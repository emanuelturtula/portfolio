"""Migration `0011_extended_keys`: `wallets.kind`, `derived_addresses`, and the reversal (#24).

Synchronous, like `test_migrations.py`, for the reason that module gives: Alembic's async
`env.py` calls `asyncio.run`. The schema is read back through a second, unconfigured engine,
so what is asserted is what is on disk.

## What is pinned, and why each one (spec 031, *Data model*, criterion 10)

* **`wallets` gains `kind`**, `TEXT NOT NULL DEFAULT 'address'`, and keeps every column,
  constraint and index it had. The migration rebuilds the table, and a rebuild written from a
  stale copy silently loses whatever the copy lacks -- `ix_wallets_user_id` most of all.
* **The two `CHECK`s on `kind`**, compared with the model's constants and the migration's
  copies, and exercised with real inserts: an unknown kind and an extended key on Kaspa are
  refused, and an extended key on Bitcoin is stored.
* **`derived_addresses`**: its columns, its unique key, its three `CHECK`s at their exact
  boundaries, the cascade from the wallet, and no index beside the unique key.
* **The upgrade over data.** Every existing wallet becomes `address`, keeps its id, and keeps
  the snapshots that reference it.
* **The downgrade's refusal (R12)** while an extended-key wallet exists -- archived ones
  included -- leaves the database at this revision with every row intact, and says a count
  and nothing else. Offline (`--sql`) it refuses outright.
* **The reversal** otherwise: the table and the column go, everything else stays, and the
  upgrade runs again.

Every key and address here is a test-network form (R11).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from portfolio.db.alembic_config import MIGRATIONS_DIR, build_alembic_config, upgrade_to_head
from portfolio.db.migration_guards import MigrationIntegrityError
from portfolio.db.migrations.versions import v0011_extended_keys
from portfolio.db.models import (
    _DERIVED_ADDRESS_BRANCH_CHECK,
    _DERIVED_ADDRESS_CHILD_INDEX_CHECK,
    _DERIVED_ADDRESS_USED_CHECK,
    _WALLET_KIND_CHAIN_CHECK,
    _WALLET_KIND_CHECK,
    DerivedAddress,
    Wallet,
)
from portfolio.domain.chains import WalletKind
from tests.address_vectors import BIP173_TESTNET_P2WPKH, KASPA_TESTNET_V0
from tests.extended_key_vectors import BIP32_TV1_M, BIP84_ACCOUNT_VPUB, BIP84_CHILDREN

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection

REVISION: Final = "0011_extended_keys"
PARENT: Final = "0010_exchange_balances"
TABLE: Final = "derived_addresses"
AT: Final = "2026-10-03 10:00:00.000000"

#: The largest index BIP32 allows without hardening, and the first it does not.
LAST_NON_HARDENED: Final = 2**31 - 1
FIRST_HARDENED: Final = 2**31

#: Spec 031's table: every column, its SQLite type, and whether it may be null.
EXPECTED_COLUMNS: Final[dict[str, tuple[str, bool]]] = {
    "id": ("INTEGER", False),
    "wallet_id": ("INTEGER", False),
    "branch": ("INTEGER", False),
    "child_index": ("INTEGER", False),
    "address_canonical": ("TEXT", False),
    "used": ("BOOLEAN", False),
    "created_at": ("DATETIME", False),
}

#: `wallets` as `0003_wallets` left it, which every later revision kept until this one.
WALLET_COLUMNS_BEFORE: Final[dict[str, tuple[str, bool]]] = {
    "id": ("INTEGER", False),
    "user_id": ("INTEGER", False),
    "chain_key": ("TEXT", False),
    "address_canonical": ("TEXT", False),
    "address_display": ("TEXT", False),
    "label": ("TEXT", True),
    "archived_at": ("DATETIME", True),
    "created_at": ("DATETIME", False),
    "updated_at": ("DATETIME", False),
}

#: ... and as this revision leaves it: the same nine, then `kind`.
WALLET_COLUMNS_AFTER: Final[dict[str, tuple[str, bool]]] = {
    **WALLET_COLUMNS_BEFORE,
    "kind": ("TEXT", False),
}

WALLET_CHECKS_BEFORE: Final = frozenset({"ck_wallets_chain_key"})
WALLET_CHECKS_AFTER: Final = WALLET_CHECKS_BEFORE | {"ck_wallets_kind", "ck_wallets_kind_chain"}

INSERT_WALLET: Final = text(
    "INSERT INTO wallets (user_id, chain_key, address_canonical, address_display, label, "
    "archived_at, created_at, updated_at) "
    "VALUES (:user, :chain, :address, :address, :label, :archived_at, :at, :at) RETURNING id"
)

INSERT_WALLET_OF_KIND: Final = text(
    "INSERT INTO wallets (user_id, chain_key, address_canonical, address_display, label, "
    "archived_at, created_at, updated_at, kind) "
    "VALUES (:user, :chain, :address, :address, NULL, :archived_at, :at, :at, :kind) "
    "RETURNING id"
)

INSERT_DERIVED: Final = text(
    "INSERT INTO derived_addresses (wallet_id, branch, child_index, address_canonical, used, "
    "created_at) VALUES (:wallet, :branch, :child_index, :address, :used, :at)"
)


def normalise_sql(expression: str) -> str:
    """Collapse whitespace and nothing else, as `test_migrations.normalise_sql` does."""
    return " ".join(expression.split())


def insert_user(connection: Connection, username: str = "owner") -> int:
    user: int = connection.execute(
        text(
            "INSERT INTO users (username, password_hash, created_at) "
            "VALUES (:name, 'not-a-hash', :at) RETURNING id"
        ),
        {"name": username, "at": AT},
    ).scalar_one()
    return int(user)


def insert_wallet(
    connection: Connection,
    user: int,
    *,
    chain: str = "bitcoin",
    address: str = BIP173_TESTNET_P2WPKH,
    label: str | None = None,
    archived_at: str | None = None,
) -> int:
    """A wallet written the way every revision before this one wrote it: with no `kind`."""
    wallet: int = connection.execute(
        INSERT_WALLET,
        {
            "user": user,
            "chain": chain,
            "address": address,
            "label": label,
            "archived_at": archived_at,
            "at": AT,
        },
    ).scalar_one()
    return int(wallet)


def insert_wallet_of_kind(
    connection: Connection,
    user: int,
    *,
    kind: str,
    chain: str = "bitcoin",
    address: str = BIP84_ACCOUNT_VPUB,
    archived_at: str | None = None,
) -> int:
    wallet: int = connection.execute(
        INSERT_WALLET_OF_KIND,
        {
            "user": user,
            "chain": chain,
            "address": address,
            "archived_at": archived_at,
            "at": AT,
            "kind": kind,
        },
    ).scalar_one()
    return int(wallet)


def insert_derived(
    connection: Connection,
    wallet: int,
    *,
    branch: int = 0,
    child_index: int = 0,
    used: object = 0,
    address: str | None = None,
) -> None:
    connection.execute(
        INSERT_DERIVED,
        {
            "wallet": wallet,
            "branch": branch,
            "child_index": child_index,
            "address": address if address is not None else BIP84_CHILDREN[0].address,
            "used": used,
            "at": AT,
        },
    )


def all_rows(engine: Engine, table: str) -> list[dict[str, Any]]:
    """Every row of `table`, by id. `table` is one of this module's literals, never input."""
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY id"))  # noqa: S608
            .mappings()
            .all()
        ]


def column_shapes(engine: Engine, table: str) -> dict[str, tuple[str, bool]]:
    return {
        str(column["name"]): (str(column["type"]), bool(column["nullable"]))
        for column in inspect(engine).get_columns(table)
    }


def check_names(engine: Engine, table: str) -> set[str]:
    return {str(found["name"]) for found in inspect(engine).get_check_constraints(table)}


def wallet_indexes(engine: Engine) -> list[tuple[str, tuple[str, ...]]]:
    return [
        (str(index["name"]), tuple(str(column) for column in index["column_names"]))
        for index in inspect(engine).get_indexes("wallets")
    ]


def stamped_revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(text("SELECT version_num FROM alembic_version")))


def seed_history(connection: Connection) -> dict[str, int]:
    """Two owners' wallets, active and archived, on both chains, and a snapshot of one."""
    owner = insert_user(connection)
    second = insert_user(connection, "second")
    bitcoin = insert_wallet(connection, owner, label="cold storage")
    kaspa = insert_wallet(connection, owner, chain="kaspa", address=KASPA_TESTNET_V0)
    archived = insert_wallet(connection, second, address=BIP84_CHILDREN[1].address, archived_at=AT)
    run: int = connection.execute(
        text(
            "INSERT INTO sync_runs (trigger, status, started_at, finished_at, duration_ms, "
            "wallets_total, wallets_succeeded, wallets_failed) "
            "VALUES ('scheduled', 'success', :at, :at, 1, 2, 2, 0) RETURNING id"
        ),
        {"at": AT},
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO balance_snapshots (wallet_id, sync_run_id, confirmed, pending, "
            "decimals, observed_at) VALUES (:wallet, :run, 123456789, 42, 8, :at)"
        ),
        {"wallet": bitcoin, "run": run, "at": AT},
    )
    return {"owner": owner, "bitcoin": bitcoin, "kaspa": kaspa, "archived": archived}


# --------------------------------------------------------------------------------------
# The revision
# --------------------------------------------------------------------------------------


def test_the_revision_sits_directly_on_top_of_the_exchange_balances_one() -> None:
    """Adjacency, not the head, for the reason `test_migrations.py` gives."""
    revisions = [
        script.revision for script in ScriptDirectory(str(MIGRATIONS_DIR)).walk_revisions()
    ]

    assert REVISION in revisions
    assert revisions.index(REVISION) == revisions.index(PARENT) - 1
    assert v0011_extended_keys.revision == REVISION
    assert v0011_extended_keys.down_revision == PARENT


def test_the_model_names_the_table_and_the_column() -> None:
    assert DerivedAddress.__tablename__ == TABLE
    kind = Wallet.__table__.c.kind
    assert not kind.nullable
    assert kind.server_default is not None


def test_the_migrations_check_texts_are_the_models() -> None:
    """Two copies of each text, and nothing mechanical holds them together but this."""
    assert v0011_extended_keys._KIND_CHECK == _WALLET_KIND_CHECK
    assert v0011_extended_keys._KIND_CHAIN_CHECK == _WALLET_KIND_CHAIN_CHECK
    assert v0011_extended_keys._BRANCH_CHECK == _DERIVED_ADDRESS_BRANCH_CHECK
    assert v0011_extended_keys._CHILD_INDEX_CHECK == _DERIVED_ADDRESS_CHILD_INDEX_CHECK
    assert v0011_extended_keys._USED_CHECK == _DERIVED_ADDRESS_USED_CHECK


def test_the_check_texts_are_the_specs() -> None:
    """Spec 031's *Data model*, written out, so an edit to a constant is an edit here too."""
    assert _WALLET_KIND_CHECK == "kind IN ('address', 'extended_key')"
    assert _WALLET_KIND_CHAIN_CHECK == "kind = 'address' OR chain_key = 'bitcoin'"
    assert _DERIVED_ADDRESS_BRANCH_CHECK == "branch IN (0, 1)"
    assert _DERIVED_ADDRESS_CHILD_INDEX_CHECK == "child_index >= 0 AND child_index < 2147483648"
    assert _DERIVED_ADDRESS_USED_CHECK == "used IN (0, 1)"


def test_the_kind_check_admits_exactly_the_domains_kinds() -> None:
    """The two lists that must never drift: `WalletKind` and the column's `CHECK`."""
    admitted = {
        literal.strip().strip("'")
        for literal in _WALLET_KIND_CHECK.split("(", 1)[1].rstrip(")").split(",")
    }
    assert admitted == {kind.value for kind in WalletKind}


# --------------------------------------------------------------------------------------
# `wallets`: one column added, nothing lost
# --------------------------------------------------------------------------------------


def test_the_wallet_gains_kind_and_keeps_the_rest(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)

    assert column_shapes(sync_engine, "wallets") == WALLET_COLUMNS_AFTER
    (kind,) = [
        column for column in inspect(sync_engine).get_columns("wallets") if column["name"] == "kind"
    ]
    assert normalise_sql(str(kind["default"])) == "'address'"


def test_the_rebuild_keeps_every_constraint_and_the_index(
    database_url: str, sync_engine: Engine
) -> None:
    """`ix_wallets_user_id` is in the `copy_from` table; a rebuild without it drops it."""
    upgrade_to_head(database_url)
    inspector = inspect(sync_engine)

    assert check_names(sync_engine, "wallets") == WALLET_CHECKS_AFTER
    assert [
        (unique["name"], tuple(unique["column_names"]))
        for unique in inspector.get_unique_constraints("wallets")
    ] == [("uq_wallets_user_chain_address", ("user_id", "chain_key", "address_canonical"))]
    assert {
        (fk["name"], fk["referred_table"], fk["options"]["ondelete"])
        for fk in inspector.get_foreign_keys("wallets")
    } == {("fk_wallets_user_id_users", "users", "CASCADE")}
    assert inspector.get_pk_constraint("wallets")["name"] == "pk_wallets"
    assert wallet_indexes(sync_engine) == [("ix_wallets_user_id", ("user_id",))]


def test_the_wallet_checks_match_the_model(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)
    reflected = {
        str(found["name"]): normalise_sql(str(found["sqltext"]))
        for found in inspect(sync_engine).get_check_constraints("wallets")
    }

    assert reflected["ck_wallets_kind"] == normalise_sql(_WALLET_KIND_CHECK)
    assert reflected["ck_wallets_kind_chain"] == normalise_sql(_WALLET_KIND_CHAIN_CHECK)


def test_a_wallet_written_without_a_kind_is_an_address(
    database_url: str, sync_engine: Engine
) -> None:
    """The server default: every writer before spec 031 wrote no `kind`, and meant this."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        insert_wallet(connection, insert_user(connection))

    (wallet,) = all_rows(sync_engine, "wallets")
    assert wallet["kind"] == "address"


def test_an_extended_key_on_bitcoin_is_stored(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        insert_wallet_of_kind(connection, insert_user(connection), kind="extended_key")

    (wallet,) = all_rows(sync_engine, "wallets")
    assert (wallet["chain_key"], wallet["kind"]) == ("bitcoin", "extended_key")
    assert wallet["address_canonical"] == BIP84_ACCOUNT_VPUB


def test_an_unknown_kind_is_refused_by_the_table(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        user = insert_user(connection)

    for kind in ("descriptor", "Address", "EXTENDED_KEY", ""):
        with (
            pytest.raises(IntegrityError, match="ck_wallets_kind"),
            sync_engine.begin() as connection,
        ):
            insert_wallet_of_kind(connection, user, kind=kind, address=BIP173_TESTNET_P2WPKH)

    assert all_rows(sync_engine, "wallets") == []


def test_an_extended_key_on_kaspa_is_refused_by_the_table(
    database_url: str, sync_engine: Engine
) -> None:
    """The domain refuses it first (R2a); this is what holds if the domain ever did not."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        user = insert_user(connection)

    with (
        pytest.raises(IntegrityError, match="ck_wallets_kind_chain"),
        sync_engine.begin() as connection,
    ):
        insert_wallet_of_kind(connection, user, kind="extended_key", chain="kaspa")

    # The control: an address on Kaspa is still an address.
    with sync_engine.begin() as connection:
        insert_wallet_of_kind(
            connection, user, kind="address", chain="kaspa", address=KASPA_TESTNET_V0
        )
    assert [(row["chain_key"], row["kind"]) for row in all_rows(sync_engine, "wallets")] == [
        ("kaspa", "address")
    ]


def test_a_null_kind_is_refused(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        user = insert_user(connection)

    with (
        pytest.raises(IntegrityError, match="NOT NULL constraint failed"),
        sync_engine.begin() as connection,
    ):
        connection.execute(
            INSERT_WALLET_OF_KIND,
            {
                "user": user,
                "chain": "bitcoin",
                "address": BIP173_TESTNET_P2WPKH,
                "archived_at": None,
                "at": AT,
                "kind": None,
            },
        )


# --------------------------------------------------------------------------------------
# `derived_addresses`
# --------------------------------------------------------------------------------------


def test_the_table_has_the_specs_columns(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)

    assert column_shapes(sync_engine, TABLE) == EXPECTED_COLUMNS


def test_the_keys_and_the_absent_index_are_the_specs(
    database_url: str, sync_engine: Engine
) -> None:
    upgrade_to_head(database_url)
    inspector = inspect(sync_engine)

    assert {
        (fk["name"], fk["constrained_columns"][0], fk["referred_table"], fk["options"]["ondelete"])
        for fk in inspector.get_foreign_keys(TABLE)
    } == {("fk_derived_addresses_wallet_id_wallets", "wallet_id", "wallets", "CASCADE")}
    assert [
        (unique["name"], tuple(unique["column_names"]))
        for unique in inspector.get_unique_constraints(TABLE)
    ] == [("uq_derived_addresses_wallet_branch_index", ("wallet_id", "branch", "child_index"))]
    assert inspector.get_pk_constraint(TABLE)["name"] == "pk_derived_addresses"
    assert inspector.get_indexes(TABLE) == [], "the unique key leads with the wallet"


def test_the_table_checks_match_the_model(database_url: str, sync_engine: Engine) -> None:
    upgrade_to_head(database_url)
    reflected = {
        str(found["name"]): normalise_sql(str(found["sqltext"]))
        for found in inspect(sync_engine).get_check_constraints(TABLE)
    }

    assert reflected == {
        "ck_derived_addresses_branch": normalise_sql(_DERIVED_ADDRESS_BRANCH_CHECK),
        "ck_derived_addresses_child_index": normalise_sql(_DERIVED_ADDRESS_CHILD_INDEX_CHECK),
        "ck_derived_addresses_used": normalise_sql(_DERIVED_ADDRESS_USED_CHECK),
    }


@pytest.fixture
def key_wallet(database_url: str, sync_engine: Engine) -> int:
    """A migrated database with one extended-key wallet in it."""
    upgrade_to_head(database_url)
    with sync_engine.begin() as connection:
        return insert_wallet_of_kind(connection, insert_user(connection), kind="extended_key")


def test_both_branches_and_both_ends_of_the_index_range_are_stored(
    sync_engine: Engine, key_wallet: int
) -> None:
    with sync_engine.begin() as connection:
        for branch in (0, 1):
            for child_index in (0, LAST_NON_HARDENED):
                insert_derived(
                    connection, key_wallet, branch=branch, child_index=child_index, used=branch
                )

    assert [
        (row["branch"], row["child_index"], row["used"]) for row in all_rows(sync_engine, TABLE)
    ] == [(0, 0, 0), (0, LAST_NON_HARDENED, 0), (1, 0, 1), (1, LAST_NON_HARDENED, 1)]


@pytest.mark.parametrize("branch", [-1, 2, 3])
def test_a_branch_other_than_receive_or_change_is_refused(
    sync_engine: Engine, key_wallet: int, branch: int
) -> None:
    with (
        pytest.raises(IntegrityError, match="ck_derived_addresses_branch"),
        sync_engine.begin() as connection,
    ):
        insert_derived(connection, key_wallet, branch=branch)

    assert all_rows(sync_engine, TABLE) == []


@pytest.mark.parametrize("child_index", [-1, FIRST_HARDENED, FIRST_HARDENED + 1, 2**32], ids=str)
def test_a_negative_or_hardened_index_is_refused(
    sync_engine: Engine, key_wallet: int, child_index: int
) -> None:
    with (
        pytest.raises(IntegrityError, match="ck_derived_addresses_child_index"),
        sync_engine.begin() as connection,
    ):
        insert_derived(connection, key_wallet, child_index=child_index)

    assert all_rows(sync_engine, TABLE) == []


@pytest.mark.parametrize("used", [2, -1, "yes"])
def test_used_is_a_boolean_and_nothing_else(
    sync_engine: Engine, key_wallet: int, used: object
) -> None:
    with (
        pytest.raises(IntegrityError, match="ck_derived_addresses_used"),
        sync_engine.begin() as connection,
    ):
        insert_derived(connection, key_wallet, used=used)

    assert all_rows(sync_engine, TABLE) == []


@pytest.mark.parametrize(
    "column", ["wallet_id", "branch", "child_index", "address_canonical", "used", "created_at"]
)
def test_no_column_of_a_derived_address_may_be_null(
    sync_engine: Engine, key_wallet: int, column: str
) -> None:
    values: dict[str, object] = {
        "wallet": key_wallet,
        "branch": 0,
        "child_index": 0,
        "address": BIP84_CHILDREN[0].address,
        "used": 0,
        "at": AT,
    }
    parameter = {
        "wallet_id": "wallet",
        "address_canonical": "address",
        "created_at": "at",
    }.get(column, column)
    values[parameter] = None

    with (
        pytest.raises(IntegrityError, match="NOT NULL constraint failed"),
        sync_engine.begin() as connection,
    ):
        connection.execute(INSERT_DERIVED, values)

    assert all_rows(sync_engine, TABLE) == []


def test_one_address_per_position_per_wallet(sync_engine: Engine, key_wallet: int) -> None:
    """The unique key, by insert. Another branch, index or wallet is a different position."""
    with sync_engine.begin() as connection:
        other = insert_wallet_of_kind(
            connection, insert_user(connection, "second"), kind="extended_key"
        )
        insert_derived(connection, key_wallet, branch=0, child_index=0)
        insert_derived(connection, key_wallet, branch=1, child_index=0)
        insert_derived(connection, key_wallet, branch=0, child_index=1)
        insert_derived(connection, other, branch=0, child_index=0)

    with (
        pytest.raises(IntegrityError, match="UNIQUE constraint failed"),
        sync_engine.begin() as connection,
    ):
        insert_derived(
            connection, key_wallet, branch=0, child_index=0, address=BIP84_CHILDREN[2].address
        )

    assert len(all_rows(sync_engine, TABLE)) == 4


def test_removing_a_wallet_takes_its_derived_addresses_and_only_its_own(
    sync_engine: Engine, key_wallet: int
) -> None:
    with sync_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        other = insert_wallet_of_kind(
            connection, insert_user(connection, "second"), kind="extended_key"
        )
        insert_derived(connection, key_wallet, child_index=0)
        insert_derived(connection, key_wallet, child_index=1)
        insert_derived(connection, other, child_index=0)
        connection.execute(text("DELETE FROM wallets WHERE id = :id"), {"id": key_wallet})
        connection.commit()

    assert [(row["wallet_id"], row["child_index"]) for row in all_rows(sync_engine, TABLE)] == [
        (other, 0)
    ]


def test_a_derived_address_cannot_reference_a_wallet_that_does_not_exist(
    sync_engine: Engine, key_wallet: int
) -> None:
    with sync_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            insert_derived(connection, key_wallet + 4242)

    assert all_rows(sync_engine, TABLE) == []


# --------------------------------------------------------------------------------------
# The upgrade over data
# --------------------------------------------------------------------------------------


def test_the_upgrade_keeps_every_wallet_as_an_address_and_what_references_it(
    database_url: str, sync_engine: Engine
) -> None:
    """Ids, labels, archive marks and snapshots all survive the rebuild of `wallets`."""
    command.upgrade(build_alembic_config(database_url), PARENT)
    with sync_engine.begin() as connection:
        ids = seed_history(connection)
    wallets_before = all_rows(sync_engine, "wallets")
    snapshots_before = all_rows(sync_engine, "balance_snapshots")
    assert "kind" not in wallets_before[0]

    upgrade_to_head(database_url)

    wallets_after = all_rows(sync_engine, "wallets")
    assert wallets_after == [{**row, "kind": "address"} for row in wallets_before]
    assert [row["id"] for row in wallets_after] == [ids["bitcoin"], ids["kaspa"], ids["archived"]]
    assert all_rows(sync_engine, "balance_snapshots") == snapshots_before
    assert all_rows(sync_engine, TABLE) == []
    assert wallet_indexes(sync_engine) == [("ix_wallets_user_id", ("user_id",))]
    with sync_engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []


def test_the_migrations_copy_of_the_table_is_the_table_the_parent_left(
    database_url: str, sync_engine: Engine
) -> None:
    """`_wallets_before()` is written out by hand, and the rebuild trusts it.

    A column, a constraint or the index missing from it would be dropped by the upgrade
    without a word. Compared with what `0010_exchange_balances` actually leaves on disk.
    """
    command.upgrade(build_alembic_config(database_url), PARENT)
    inspector = inspect(sync_engine)
    declared = v0011_extended_keys._wallets_before()

    assert {column.name: bool(column.nullable) for column in declared.columns} == {
        str(column["name"]): bool(column["nullable"]) for column in inspector.get_columns("wallets")
    }
    on_disk = {
        str(found["name"])
        for found in (
            *inspector.get_check_constraints("wallets"),
            *inspector.get_unique_constraints("wallets"),
            *inspector.get_foreign_keys("wallets"),
            inspector.get_pk_constraint("wallets"),
        )
    }
    assert {str(constraint.name) for constraint in declared.constraints} == on_disk
    assert {str(index.name) for index in declared.indexes} == {
        str(index["name"]) for index in inspector.get_indexes("wallets")
    }


def test_the_downgrades_copy_of_the_table_is_the_table_this_revision_leaves(
    database_url: str, sync_engine: Engine
) -> None:
    upgrade_to_head(database_url)
    inspector = inspect(sync_engine)
    declared = v0011_extended_keys._wallets_after()

    assert {column.name: bool(column.nullable) for column in declared.columns} == {
        str(column["name"]): bool(column["nullable"]) for column in inspector.get_columns("wallets")
    }
    on_disk = {
        str(found["name"])
        for found in (
            *inspector.get_check_constraints("wallets"),
            *inspector.get_unique_constraints("wallets"),
            *inspector.get_foreign_keys("wallets"),
            inspector.get_pk_constraint("wallets"),
        )
    }
    assert {str(constraint.name) for constraint in declared.constraints} == on_disk


def at_this_revision(database_url: str) -> None:
    """Migrate to this revision and no further.

    A downgrade from head would first run `0012_drop_exchanges_accounting`'s, which rebuilds
    tables this revision has nothing to do with; the tests below are about this one step.
    """
    command.upgrade(build_alembic_config(database_url), REVISION)


# --------------------------------------------------------------------------------------
# The downgrade (R12)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("archived", "expected_count"),
    [((None,), 1), (("2026-10-03 11:00:00.000000",), 1), ((None, "2026-10-03 11:00:00.000000"), 2)],
    ids=["active", "archived", "one of each"],
)
def test_the_downgrade_refuses_while_an_extended_key_wallet_exists(
    database_url: str,
    sync_engine: Engine,
    archived: tuple[str | None, ...],
    expected_count: int,
) -> None:
    """Archived ones included: an archived wallet is one un-archive away from the sync."""
    at_this_revision(database_url)
    keys = (BIP84_ACCOUNT_VPUB, BIP32_TV1_M)
    with sync_engine.begin() as connection:
        ids = seed_history(connection)
        for key, archived_at in zip(keys, archived, strict=False):
            wallet = insert_wallet_of_kind(
                connection, ids["owner"], kind="extended_key", address=key, archived_at=archived_at
            )
            insert_derived(connection, wallet, branch=0, child_index=0, used=1)
    wallets_before = all_rows(sync_engine, "wallets")
    derived_before = all_rows(sync_engine, TABLE)
    snapshots_before = all_rows(sync_engine, "balance_snapshots")

    with pytest.raises(MigrationIntegrityError) as refused:
        command.downgrade(build_alembic_config(database_url), PARENT)

    message = str(refused.value)
    assert message == v0011_extended_keys.DOWNGRADE_REFUSED.format(count=expected_count)
    for key in keys:
        assert key not in message
    assert BIP84_CHILDREN[0].address not in message
    assert stamped_revision(sync_engine) == REVISION
    assert all_rows(sync_engine, "wallets") == wallets_before
    assert all_rows(sync_engine, TABLE) == derived_before
    assert all_rows(sync_engine, "balance_snapshots") == snapshots_before
    assert check_names(sync_engine, "wallets") == WALLET_CHECKS_AFTER


def test_the_refusal_names_a_count_and_the_way_out() -> None:
    """A fixed sentence with one placeholder, the count. Never a key, never an id."""
    template = v0011_extended_keys.DOWNGRADE_REFUSED

    assert template.count("{") == 1
    assert "{count}" in template
    assert "docs/operations.md" in template
    assert "0011_extended_keys" in template


def test_the_downgrade_round_trips_with_no_extended_key_wallet(
    database_url: str, sync_engine: Engine
) -> None:
    """The table and the column go; every wallet, snapshot, constraint and index stays."""
    at_this_revision(database_url)
    with sync_engine.begin() as connection:
        seed_history(connection)
    wallets_before = all_rows(sync_engine, "wallets")
    snapshots_before = all_rows(sync_engine, "balance_snapshots")
    tables_before = set(inspect(sync_engine).get_table_names())

    command.downgrade(build_alembic_config(database_url), PARENT)

    assert stamped_revision(sync_engine) == PARENT
    assert set(inspect(sync_engine).get_table_names()) == tables_before - {TABLE}
    assert column_shapes(sync_engine, "wallets") == WALLET_COLUMNS_BEFORE
    assert check_names(sync_engine, "wallets") == WALLET_CHECKS_BEFORE
    assert wallet_indexes(sync_engine) == [("ix_wallets_user_id", ("user_id",))]
    assert all_rows(sync_engine, "wallets") == [
        {name: value for name, value in row.items() if name != "kind"} for row in wallets_before
    ]
    assert all_rows(sync_engine, "balance_snapshots") == snapshots_before
    with sync_engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

    at_this_revision(database_url)

    assert set(inspect(sync_engine).get_table_names()) == tables_before
    assert all_rows(sync_engine, "wallets") == wallets_before
    assert all_rows(sync_engine, "balance_snapshots") == snapshots_before
    assert check_names(sync_engine, "wallets") == WALLET_CHECKS_AFTER


def test_the_downgrade_proceeds_once_the_extended_key_wallet_is_removed(
    database_url: str, sync_engine: Engine
) -> None:
    """What `docs/operations.md` tells the operator to do, and that it is enough."""
    at_this_revision(database_url)
    with sync_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        user = insert_user(connection)
        wallet = insert_wallet_of_kind(connection, user, kind="extended_key")
        insert_derived(connection, wallet, child_index=0)
        insert_derived(connection, wallet, child_index=1)
        connection.commit()
    with pytest.raises(MigrationIntegrityError):
        command.downgrade(build_alembic_config(database_url), PARENT)

    with sync_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.execute(text("DELETE FROM wallets WHERE id = :id"), {"id": wallet})
        connection.commit()
    assert all_rows(sync_engine, TABLE) == [], "the cascade took the derived addresses"

    command.downgrade(build_alembic_config(database_url), PARENT)

    assert stamped_revision(sync_engine) == PARENT
    assert TABLE not in inspect(sync_engine).get_table_names()


def test_the_offline_downgrade_refuses_outright(
    database_url: str, sync_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no database to count wallets in, a rendered script could strand one."""
    at_this_revision(database_url)
    with sync_engine.begin() as connection:
        seed_history(connection)
    wallets_before = all_rows(sync_engine, "wallets")

    with pytest.raises(MigrationIntegrityError) as refused:
        command.downgrade(build_alembic_config(database_url), f"{REVISION}:{PARENT}", sql=True)

    assert str(refused.value) == v0011_extended_keys.DOWNGRADE_REFUSED_OFFLINE
    assert "DROP TABLE derived_addresses" not in capsys.readouterr().out
    assert stamped_revision(sync_engine) == REVISION
    assert all_rows(sync_engine, "wallets") == wallets_before
