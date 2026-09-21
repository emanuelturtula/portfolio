"""Criteria 2, 5 and 6: what the `wallets` table itself guarantees.

Every database here is a real file under `tmp_path`, built by running the **migrations**,
never by `metadata.create_all`. That distinction is the whole value of this module: the
migration is the only description of the schema that production ever executes, so a
constraint asserted against a `create_all` schema is a constraint asserted against a file
the Raspberry Pi has never seen.

## Why criterion 5 cannot be tested through the router

`POST /api/wallets` returning `409` proves that *something* refused a duplicate. It passes
whether the `UNIQUE` constraint exists or the service merely looked first, and a service
check alone loses the race between two concurrent writes and, worse, leaves the invariant
resting on a branch that a later refactor can delete without any test noticing.

So the decisive test here goes around the service *and* the repository and issues the
second `INSERT` as raw SQL. The only thing left that can refuse it is SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from portfolio.db.engine import create_session_factory
from portfolio.db.models import _WALLET_CHAIN_KEY_CHECK, Wallet
from portfolio.domain.chains import ChainKey
from portfolio.repositories.wallets import WalletRepository
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WPKH_UPPERCASE,
    BIP350_TESTNET_V1,
    CORE_SIGNET_P2PKH,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

CREATED_AT: Final = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
LATER: Final = datetime(2026, 9, 22, 8, 30, tzinfo=UTC)

OWNER_ID: Final = 1
SECOND_OWNER_ID: Final = 2

INSERT_USER: Final = text(
    "INSERT INTO users (id, username, password_hash, created_at) "
    "VALUES (:id, :username, 'not-a-real-hash', '2026-09-21 00:00:00')"
)

# Bound parameters, and deliberately not built through the ORM: this is the statement that
# has to reach SQLite with nothing of ours in between.
INSERT_WALLET: Final = text(
    "INSERT INTO wallets "
    "(user_id, chain_key, address_canonical, address_display, label, "
    " archived_at, created_at, updated_at) "
    "VALUES (:user_id, :chain_key, :canonical, :display, :label, "
    " :archived_at, '2026-09-21 12:00:00', '2026-09-21 12:00:00')"
)


@pytest.fixture
async def wallets_engine(migrated_engine: AsyncEngine) -> AsyncEngine:
    """A migrated database with two accounts in it, for the foreign key to land on.

    Returns rather than yields: `migrated_engine` owns the disposal, so there is nothing
    for this fixture to tear down and a `yield` would only imply otherwise.
    """
    async with migrated_engine.begin() as connection:
        await connection.execute(INSERT_USER, {"id": OWNER_ID, "username": "owner"})
        await connection.execute(INSERT_USER, {"id": SECOND_OWNER_ID, "username": "second"})
    return migrated_engine


@pytest.fixture
async def session(wallets_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session from the application's own factory, over the migrated file."""
    factory = create_session_factory(wallets_engine)
    async with factory() as opened:
        yield opened


@pytest.fixture
def repository(session: AsyncSession) -> WalletRepository:
    return WalletRepository(session)


async def add_wallet(
    repository: WalletRepository,
    *,
    canonical: str = BIP173_TESTNET_P2WPKH,
    display: str = BIP173_TESTNET_P2WPKH,
    chain_key: str = ChainKey.BITCOIN.value,
    user_id: int = OWNER_ID,
    label: str | None = "Cold storage",
) -> Wallet:
    """Insert one wallet through the repository, as the service would."""
    return await repository.add(
        user_id=user_id,
        chain_key=chain_key,
        address_canonical=canonical,
        address_display=display,
        label=label,
        created_at=CREATED_AT,
    )


# --------------------------------------------------------------------------------------
# Criterion 2: both forms reach the database
# --------------------------------------------------------------------------------------


async def test_stores_canonical_and_display_separately(
    repository: WalletRepository,
    session: AsyncSession,
) -> None:
    """Criterion 2: two columns, two different strings, both persisted.

    The pair used is the one that makes the columns distinguishable at all -- an address
    a wallet rendered in capitals. If the schema stored one column and lower-cased at
    query time, the display form would come back wrong here and nowhere else.
    """
    await add_wallet(
        repository,
        canonical=BIP173_TESTNET_P2WPKH,
        display=BIP173_TESTNET_P2WPKH_UPPERCASE,
    )
    await session.commit()
    session.expunge_all()

    stored = (await session.scalars(select(Wallet))).one()

    assert stored.address_canonical == BIP173_TESTNET_P2WPKH
    assert stored.address_display == BIP173_TESTNET_P2WPKH_UPPERCASE
    assert stored.address_canonical != stored.address_display
    assert stored.chain_key == "bitcoin"
    assert stored.label == "Cold storage"
    assert stored.archived_at is None


async def test_the_stored_strings_are_read_back_byte_for_byte(
    repository: WalletRepository,
    session: AsyncSession,
) -> None:
    """A `TEXT` column that normalised case or trimmed would corrupt a legacy address."""
    await add_wallet(repository, canonical=CORE_SIGNET_P2PKH, display=CORE_SIGNET_P2PKH)
    await session.commit()

    raw = await session.execute(text("SELECT address_canonical, address_display FROM wallets"))

    assert raw.one() == (CORE_SIGNET_P2PKH, CORE_SIGNET_P2PKH)


# --------------------------------------------------------------------------------------
# Criterion 5: the constraint is in the database
# --------------------------------------------------------------------------------------


async def test_unique_constraint_rejects_duplicate_insert(
    wallets_engine: AsyncEngine,
) -> None:
    """Criterion 5, proven by SQLite and by nothing else.

    No service, no repository, no ORM: two raw `INSERT`s with the same
    `(user_id, chain_key, address_canonical)`. Whatever the service does or stops doing,
    the second one has to fail here -- and if `UNIQUE` is missing from the migration, this
    is the test that goes red while `POST` still answers 409 from its own check.
    """
    row = {
        "user_id": OWNER_ID,
        "chain_key": ChainKey.BITCOIN.value,
        "canonical": BIP173_TESTNET_P2WPKH,
        "display": BIP173_TESTNET_P2WPKH,
        "label": "first",
        "archived_at": None,
    }
    async with wallets_engine.begin() as connection:
        await connection.execute(INSERT_WALLET, row)

    with pytest.raises(IntegrityError) as caught:
        async with wallets_engine.begin() as connection:
            # A different label and a different display form: the constraint is on the
            # three columns it names, not on the whole row.
            await connection.execute(INSERT_WALLET, {**row, "label": "second"})

    assert "UNIQUE" in str(caught.value).upper()

    async with wallets_engine.connect() as connection:
        remaining = await connection.scalar(text("SELECT COUNT(*) FROM wallets"))
    assert remaining == 1, "the refused insert must not have landed"


async def test_the_constraint_holds_for_an_archived_row_too(
    wallets_engine: AsyncEngine,
) -> None:
    """An archived wallet still occupies its slot, which is why a re-add is 409.

    The rejected alternative was a partial index over unarchived rows. That would let a
    re-add silently resurrect a retired wallet -- with its old label and, once #10 lands,
    its old balance history -- while looking to the user like a brand new one.
    """
    async with wallets_engine.begin() as connection:
        await connection.execute(
            INSERT_WALLET,
            {
                "user_id": OWNER_ID,
                "chain_key": ChainKey.BITCOIN.value,
                "canonical": BIP173_TESTNET_P2WPKH,
                "display": BIP173_TESTNET_P2WPKH,
                "label": "retired",
                "archived_at": "2026-09-22 08:30:00",
            },
        )

    with pytest.raises(IntegrityError):
        async with wallets_engine.begin() as connection:
            await connection.execute(
                INSERT_WALLET,
                {
                    "user_id": OWNER_ID,
                    "chain_key": ChainKey.BITCOIN.value,
                    "canonical": BIP173_TESTNET_P2WPKH,
                    "display": BIP173_TESTNET_P2WPKH,
                    "label": "re-added",
                    "archived_at": None,
                },
            )


async def test_the_same_address_is_allowed_on_another_chain(
    wallets_engine: AsyncEngine,
) -> None:
    """The constraint spans three columns; two of them must be able to differ."""
    base = {
        "canonical": BIP173_TESTNET_P2WPKH,
        "display": BIP173_TESTNET_P2WPKH,
        "label": None,
        "archived_at": None,
    }
    async with wallets_engine.begin() as connection:
        await connection.execute(
            INSERT_WALLET, {**base, "user_id": OWNER_ID, "chain_key": ChainKey.BITCOIN.value}
        )
        await connection.execute(
            INSERT_WALLET, {**base, "user_id": OWNER_ID, "chain_key": ChainKey.KASPA.value}
        )
        await connection.execute(
            INSERT_WALLET,
            {**base, "user_id": SECOND_OWNER_ID, "chain_key": ChainKey.BITCOIN.value},
        )
        total = await connection.scalar(text("SELECT COUNT(*) FROM wallets"))

    assert total == 3


async def test_the_repository_also_hits_the_constraint(
    repository: WalletRepository,
    session: AsyncSession,
) -> None:
    """The realistic path: the same refusal arrives through the code the service uses."""
    await add_wallet(repository)
    await session.commit()

    with pytest.raises(IntegrityError):
        await add_wallet(repository, label="again")


async def test_two_different_addresses_coexist_for_one_user(
    repository: WalletRepository,
    session: AsyncSession,
) -> None:
    """The constraint must not be so wide that a second wallet is impossible."""
    await add_wallet(repository, canonical=BIP173_TESTNET_P2WPKH, display=BIP173_TESTNET_P2WPKH)
    await add_wallet(repository, canonical=BIP350_TESTNET_V1, display=BIP350_TESTNET_V1)
    await session.commit()

    stored = await repository.list_for_user(OWNER_ID)

    assert {wallet.address_canonical for wallet in stored} == {
        BIP173_TESTNET_P2WPKH,
        BIP350_TESTNET_V1,
    }


# --------------------------------------------------------------------------------------
# Criterion 6: archiving keeps the row
# --------------------------------------------------------------------------------------


async def test_archived_row_is_retained_with_its_id(
    repository: WalletRepository,
    session: AsyncSession,
) -> None:
    """Criterion 6: the row survives, keeps its primary key, and stays unique.

    `balance_snapshots` does not exist yet -- it is #10 -- so what is provable today is
    that a later `wallet_id` foreign key will still have something to point at. A hard
    delete would take the id with it and orphan every snapshot ever taken of that address.
    """
    wallet = await add_wallet(repository)
    await session.commit()
    original_id = wallet.id
    assert original_id is not None

    await repository.set_archived_at(wallet, LATER, LATER)
    await session.commit()
    session.expunge_all()

    stored = (await session.scalars(select(Wallet))).all()

    assert len(stored) == 1
    assert stored[0].id == original_id
    assert stored[0].archived_at == LATER
    assert stored[0].address_canonical == BIP173_TESTNET_P2WPKH
    assert stored[0].updated_at == LATER
    assert stored[0].created_at == CREATED_AT


async def test_unarchiving_clears_the_timestamp_without_moving_the_row(
    repository: WalletRepository,
    session: AsyncSession,
) -> None:
    """`PATCH {"archived": false}` is an edit to a row the user can already see."""
    wallet = await add_wallet(repository)
    await session.commit()
    original_id = wallet.id

    await repository.set_archived_at(wallet, LATER, LATER)
    await session.commit()
    await repository.set_archived_at(wallet, None, LATER)
    await session.commit()
    session.expunge_all()

    stored = (await session.scalars(select(Wallet))).one()

    assert stored.id == original_id
    assert stored.archived_at is None


async def test_listing_hides_archived_rows_unless_asked(
    repository: WalletRepository,
    session: AsyncSession,
) -> None:
    """The repository is where `include_archived` is answered, not the router."""
    live = await add_wallet(repository, canonical=BIP173_TESTNET_P2WPKH)
    retired = await add_wallet(repository, canonical=BIP350_TESTNET_V1)
    await session.commit()
    await repository.set_archived_at(retired, LATER, LATER)
    await session.commit()

    default = await repository.list_for_user(OWNER_ID)
    everything = await repository.list_for_user(OWNER_ID, include_archived=True)

    assert [wallet.id for wallet in default] == [live.id]
    assert {wallet.id for wallet in everything} == {live.id, retired.id}


async def test_one_users_wallets_are_not_another_users(
    repository: WalletRepository,
    session: AsyncSession,
) -> None:
    """`user_id` is in the constraint, so it has to be in the read path as well."""
    mine = await add_wallet(repository, user_id=OWNER_ID)
    await add_wallet(repository, user_id=SECOND_OWNER_ID)
    await session.commit()

    assert [wallet.id for wallet in await repository.list_for_user(OWNER_ID)] == [mine.id]
    assert await repository.get_for_user(SECOND_OWNER_ID, mine.id) is None
    assert (await repository.get_for_user(OWNER_ID, mine.id)) is not None


# --------------------------------------------------------------------------------------
# The schema the migration actually wrote
# --------------------------------------------------------------------------------------


def test_the_unique_constraint_is_in_the_migrated_schema(
    migrated_database_url: str,
    sync_engine: Engine,
) -> None:
    """Reflected off disk, through a second connection, by name and by column list.

    The raw-insert test above proves SQLite refuses a duplicate. This proves *which*
    constraint refused it, which is what makes the failure legible if the columns are ever
    reordered or one is dropped from the list.
    """
    del migrated_database_url  # Ordering only: the fixture migrates the file.

    unique = {
        str(constraint["name"]): list(constraint["column_names"])
        for constraint in inspect(sync_engine).get_unique_constraints("wallets")
    }

    assert unique == {
        "uq_wallets_user_chain_address": ["user_id", "chain_key", "address_canonical"]
    }


def test_the_chain_key_check_constraint_matches_the_model(
    migrated_database_url: str,
    sync_engine: Engine,
) -> None:
    """Alembic has no check-constraint comparator, so this is the only thing looking.

    `assets.kind` already carries this exact problem and this exact test. Editing
    `_WALLET_CHAIN_KEY_CHECK` without writing the matching migration passes ruff, mypy,
    the layering contract, the drift check and every other test in the repository, and
    then rejects inserts in production with `CHECK constraint failed: ck_wallets_chain_key`.
    """
    del migrated_database_url

    reflected = {
        str(constraint["name"]): str(constraint["sqltext"])
        for constraint in inspect(sync_engine).get_check_constraints("wallets")
    }

    assert set(reflected) == {"ck_wallets_chain_key"}
    assert " ".join(reflected["ck_wallets_chain_key"].split()) == " ".join(
        _WALLET_CHAIN_KEY_CHECK.split()
    )


def test_the_check_constraint_admits_exactly_the_chain_keys_the_domain_knows(
    migrated_database_url: str,
    sync_engine: Engine,
) -> None:
    """The two lists that must never drift: the registry's keys and the column's `CHECK`.

    A `ChainKey` the constraint does not admit is a 500 on a request the API accepted; a
    value the constraint admits that the registry cannot validate is an address nothing
    can ever read a balance for. Asserted by *inserting* each key rather than by parsing
    the constraint text, so it is the database's own opinion that is recorded.
    """
    del migrated_database_url

    with sync_engine.begin() as connection:
        connection.execute(INSERT_USER, {"id": OWNER_ID, "username": "owner"})
        for index, key in enumerate(ChainKey):
            connection.execute(
                INSERT_WALLET,
                {
                    "user_id": OWNER_ID,
                    "chain_key": key.value,
                    "canonical": f"{BIP173_TESTNET_P2WPKH}{index}",
                    "display": f"{BIP173_TESTNET_P2WPKH}{index}",
                    "label": None,
                    "archived_at": None,
                },
            )
        accepted = connection.scalar(text("SELECT COUNT(*) FROM wallets"))

    assert accepted == len(ChainKey)

    with pytest.raises(IntegrityError) as caught, sync_engine.begin() as connection:
        connection.execute(
            INSERT_WALLET,
            {
                "user_id": OWNER_ID,
                "chain_key": "ethereum",
                "canonical": BIP173_TESTNET_P2WPKH,
                "display": BIP173_TESTNET_P2WPKH,
                "label": None,
                "archived_at": None,
            },
        )

    assert "ck_wallets_chain_key" in str(caught.value)


def test_the_user_index_exists(migrated_database_url: str, sync_engine: Engine) -> None:
    """Every read is scoped by `user_id`; without the index every read is a table scan."""
    del migrated_database_url

    indexes = {index["name"] for index in inspect(sync_engine).get_indexes("wallets")}

    assert "ix_wallets_user_id" in indexes


def test_the_foreign_key_cascades_from_users(
    migrated_database_url: str,
    sync_engine: Engine,
) -> None:
    """Deleting the account has to take its wallets, or `create-user --replace` orphans."""
    del migrated_database_url

    foreign_keys = inspect(sync_engine).get_foreign_keys("wallets")

    assert len(foreign_keys) == 1
    assert foreign_keys[0]["referred_table"] == "users"
    assert foreign_keys[0]["constrained_columns"] == ["user_id"]
    assert foreign_keys[0]["options"].get("ondelete") == "CASCADE"
