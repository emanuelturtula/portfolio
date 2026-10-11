"""Migration `0018_portfolios`: the table, the two columns, filing in `Main`, and the reversal.

Synchronous, like `test_migrations.py`, for the reason that module gives. What is pinned
(spec 044, *Data model* and criteria 1 and 8):

* **Every existing wallet and operation lands in its owner's `Main`**, each owner their own,
  and every account gets one even with nothing to file.
* **The rebuilds keep everything else**: the copies written out in the migration are compared
  with what the parent revision leaves on disk, so a constraint or an index missing from them
  would show here rather than vanish.
* **The columns are required and point at a portfolio**, and a portfolio that holds rows
  cannot be deleted under the database's own enforcement.
* **The downgrade keeps every row** and drops only the grouping.

Every address here is a test-network form.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest
from alembic import command
from sqlalchemy import Table, inspect, text
from sqlalchemy.exc import IntegrityError

from portfolio.db.alembic_config import build_alembic_config, upgrade_to_head
from portfolio.db.migrations.versions import v0018_portfolios
from tests.address_vectors import BIP173_TESTNET_P2WPKH, KASPA_TESTNET_V0

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection

REVISION: Final = "0018_portfolios"
PARENT: Final = "0017_manual_rewards_and_fees"
AT: Final = "2026-10-11 10:00:00.000000"


def insert_user(connection: Connection, username: str, created_at: str = AT) -> int:
    return int(
        connection.execute(
            text(
                "INSERT INTO users (username, password_hash, created_at) "
                "VALUES (:name, 'not-a-hash', :at) RETURNING id"
            ),
            {"name": username, "at": created_at},
        ).scalar_one()
    )


def insert_wallet(connection: Connection, user: int, chain: str, address: str) -> int:
    return int(
        connection.execute(
            text(
                "INSERT INTO wallets (user_id, chain_key, address_canonical, address_display, "
                "label, archived_at, created_at, updated_at) "
                "VALUES (:user, :chain, :address, :address, NULL, NULL, :at, :at) RETURNING id"
            ),
            {"user": user, "chain": chain, "address": address, "at": AT},
        ).scalar_one()
    )


def insert_operation(connection: Connection, user: int, external_id: str) -> int:
    return int(
        connection.execute(
            text(
                "INSERT INTO exchange_operations (user_id, source, venue, external_id, "
                "executed_at, kind, asset, quantity, description, created_at) VALUES "
                "(:user, 'bitget', 'Bitget', :id, :at, 'buy', 'KAS', '1', '', :at) RETURNING id"
            ),
            {"user": user, "id": external_id, "at": AT},
        ).scalar_one()
    )


def shape(engine: Engine, table: str) -> tuple[dict[str, bool], set[str], set[str]]:
    """A table's columns with nullability, its named constraints, and its indexes."""
    inspector = inspect(engine)
    columns = {
        str(column["name"]): bool(column["nullable"]) for column in inspector.get_columns(table)
    }
    constraints = {
        str(found["name"])
        for found in (
            *inspector.get_check_constraints(table),
            *inspector.get_unique_constraints(table),
            *inspector.get_foreign_keys(table),
            inspector.get_pk_constraint(table),
        )
    }
    indexes = {str(index["name"]) for index in inspector.get_indexes(table)}
    return columns, constraints, indexes


def declared_shape(table_definition: Table) -> tuple[dict[str, bool], set[str], set[str]]:
    """The same, for a table the migration writes out."""
    return (
        {column.name: bool(column.nullable) for column in table_definition.columns},
        {str(constraint.name) for constraint in table_definition.constraints},
        {str(index.name) for index in table_definition.indexes},
    )


def before(table: str) -> Table:
    """The migration's hand-written copy of `table` as the parent revision leaves it."""
    if table == "wallets":
        return v0018_portfolios._wallets(with_portfolio=False)
    return v0018_portfolios._operations(with_portfolio=False)


@pytest.mark.parametrize("table", ["wallets", "exchange_operations"])
def test_the_migrations_copies_are_the_tables_the_parent_left(
    database_url: str, sync_engine: Engine, table: str
) -> None:
    """The rebuild trusts the copies written out by hand; compare them with the disk."""
    command.upgrade(build_alembic_config(database_url), PARENT)

    assert declared_shape(before(table)) == shape(sync_engine, table)


def test_every_owner_gets_a_main_and_every_row_is_filed_in_their_own(
    database_url: str, sync_engine: Engine
) -> None:
    command.upgrade(build_alembic_config(database_url), PARENT)
    with sync_engine.begin() as connection:
        first = insert_user(connection, "first", "2026-01-01 00:00:00.000000")
        second = insert_user(connection, "second")
        idle = insert_user(connection, "idle")
        wallet_one = insert_wallet(connection, first, "bitcoin", BIP173_TESTNET_P2WPKH)
        wallet_two = insert_wallet(connection, second, "kaspa", KASPA_TESTNET_V0)
        operation_one = insert_operation(connection, first, "a")
        operation_two = insert_operation(connection, second, "a")

    command.upgrade(build_alembic_config(database_url), REVISION)

    with sync_engine.connect() as connection:
        portfolios = {
            row.user_id: (row.id, row.name, row.created_at, row.updated_at)
            for row in connection.execute(
                text("SELECT id, user_id, name, created_at, updated_at FROM portfolios")
            )
        }
        wallets: dict[int, int] = dict(
            connection.execute(text("SELECT id, portfolio_id FROM wallets")).tuples().all()
        )
        operations: dict[int, int] = dict(
            connection.execute(text("SELECT id, portfolio_id FROM exchange_operations"))
            .tuples()
            .all()
        )

    assert set(portfolios) == {first, second, idle}
    assert {entry[1] for entry in portfolios.values()} == {"Main"}
    # The account's own timestamp: the migration reads no clock.
    assert portfolios[first][2:] == ("2026-01-01 00:00:00.000000",) * 2
    assert wallets == {wallet_one: portfolios[first][0], wallet_two: portfolios[second][0]}
    assert operations == {
        operation_one: portfolios[first][0],
        operation_two: portfolios[second][0],
    }


def test_the_columns_are_required_indexed_and_keep_everything_else(
    database_url: str, sync_engine: Engine
) -> None:
    upgrade_to_head(database_url)

    for table in ("wallets", "exchange_operations"):
        columns, constraints, indexes = shape(sync_engine, table)
        before_columns, before_constraints, before_indexes = declared_shape(before(table))
        assert columns == {**before_columns, "portfolio_id": False}
        assert constraints == before_constraints | {f"fk_{table}_portfolio_id_portfolios"}
        assert indexes == before_indexes | {f"ix_{table}_portfolio_id"}


def test_a_portfolio_that_holds_rows_cannot_be_deleted(
    database_url: str, sync_engine: Engine
) -> None:
    """R10, with foreign keys enforced as the application's engine enforces them."""
    upgrade_to_head(database_url)
    with sync_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        with connection.begin():
            user = insert_user(connection, "owner")
            connection.execute(
                text(
                    "INSERT INTO portfolios (user_id, name, created_at, updated_at) "
                    "VALUES (:user, 'Main', :at, :at)"
                ),
                {"user": user, "at": AT},
            )
            connection.execute(
                text(
                    "INSERT INTO wallets (user_id, chain_key, address_canonical, "
                    "address_display, created_at, updated_at, portfolio_id) VALUES (:user, "
                    "'bitcoin', :address, :address, :at, :at, "
                    "(SELECT id FROM portfolios WHERE user_id = :user))"
                ),
                {"user": user, "address": BIP173_TESTNET_P2WPKH, "at": AT},
            )
        with pytest.raises(IntegrityError), connection.begin():
            connection.execute(text("DELETE FROM portfolios"))


def test_the_downgrade_keeps_every_row_and_drops_the_grouping(
    database_url: str, sync_engine: Engine
) -> None:
    command.upgrade(build_alembic_config(database_url), PARENT)
    with sync_engine.begin() as connection:
        user = insert_user(connection, "owner")
        insert_wallet(connection, user, "bitcoin", BIP173_TESTNET_P2WPKH)
        insert_operation(connection, user, "a")
    command.upgrade(build_alembic_config(database_url), REVISION)

    command.downgrade(build_alembic_config(database_url), PARENT)

    assert "portfolios" not in inspect(sync_engine).get_table_names()
    for table in ("wallets", "exchange_operations"):
        assert shape(sync_engine, table) == declared_shape(before(table))
    with sync_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM wallets")).scalar_one() == 1
        assert (
            connection.execute(text("SELECT COUNT(*) FROM exchange_operations")).scalar_one() == 1
        )
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

    command.upgrade(build_alembic_config(database_url), REVISION)
