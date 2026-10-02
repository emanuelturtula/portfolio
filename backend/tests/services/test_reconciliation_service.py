"""Criterion 6 of #104 at the service: the three stored sides, summed and compared (spec 025).

`ReconciliationService.reconciliation(user_id)` reads what the syncs and the recompute
**stored** -- the cost-basis snapshot, each active wallet's latest balance snapshot, and each
exchange account's last balance reading -- sums each side per asset, and hands the three
mappings to `domain.accounting.reconcile`. It asks no chain and no venue.

Everything here runs against a real SQLite file migrated to head. The history is planted as
fills through the application's own insert and replayed by the real recompute; the wallet
readings are rows written the way the balance sync leaves them; the venue readings go through
`ExchangeBalanceRepository.replace`, the write the exchange sync makes. The service's clock is
injected and stands at `NOW`, five minutes after the snapshot.

## The scenario most tests share, worked by hand

| Asset | History (fills) | Wallets | Exchanges | Held | Difference | Status |
|---|---|---|---|---|---|---|
| BTC | 0.5 | 0.4 + 0.3 = 0.7 | 0.2 + 0.1 = 0.3 | 1.0 | +0.5 | `history_short` |
| ETH | 2 | -- | 0.5 | 0.5 | -1.5 | `history_over` |
| KAS | 1000 | 600 | 395 | 995 | -5 | `match` (0.5% of 1000) |
| SOL | -- | -- | 3 | 3 | +3 | `history_short` |
| DOGE | bought 10, sold 10 = 0 | -- | -- | 0 | -- | omitted: both zero |
| USDT | cash: no position | -- | 5000 + 250 | -- | -- | omitted: cash |

BTC is the spec's own example row. The wallets hold satoshis and sompi at eight decimals;
`0.4 BTC` is `40_000_000` base units. Bitget holds 0.2 BTC, 0.5 ETH, 395 KAS and 5000 USDT;
BingX holds 0.1 BTC, 3 SOL and 250 USDT.

## R9: a reading is compared only while it is current

The held side is a lower bound on what the owner holds, and that is what makes "more is held
than the history accounts for" a finding. **A reading that is out of date is not a lower
bound**: coins moved since it was taken are counted where they were and where they are now.
So a venue's rows are summed only when its last balance read succeeded, its fill sync is `ok`
and the reading is at most twenty-four hours old; a wallet's only when its reading is at most
that old. Anything else contributes **nothing**, and its source says why. The second half of
this module is that rule: each reason, their precedence, the age boundary to the microsecond
on both sides, and the scenario the rule was written for -- a withdrawal after a reading
that a failed read then kept -- which used to be reported as coins missing from the history.

## No chain fails in this module

Spec 028 added a second condition for a wallet: its chain did not fail in the latest finished
balance run. `plant_reading` writes each reading under a `success` run of its own with no
chain rows, and a chain with no row did not fail, so every wallet here is judged by the age
limit alone, as it was before that rule. A failed chain is
`tests/services/test_reconciliation_chain_failed.py`'s.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import event, text

from portfolio.domain.accounting import ReconciliationStatus
from portfolio.domain.chains import ChainKey
from portfolio.domain.exchanges import AccountSyncStatus, ExchangeKey, FillSide
from portfolio.repositories.exchange_balances import (
    AccountBalances,
    ExchangeBalanceRepository,
    StoredBalance,
)
from portfolio.repositories.exchange_sync_runs import ExchangeSyncErrorKind
from portfolio.services import reconciliation as reconciliation_module
from portfolio.services.accounting import SnapshotReadError, build_accounting_service
from portfolio.services.reconciliation import (
    MAX_READING_AGE,
    MAX_READING_AGE_HOURS,
    ExchangeBalanceSource,
    NotComparedReason,
    ReconciliationService,
    ReconciliationView,
    WalletSources,
    build_reconciliation_service,
    not_compared_reason,
)
from tests.accounting_harness import at, plant_account, plant_fills, plant_owner
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WSH,
    BIP350_TESTNET_V1,
    KASPA_TESTNET_V0,
)
from tests.balance_harness import insert_wallet, sqlite_timestamp
from tests.exchange_sync_harness import SettableClock, held, make_fill
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.domain.accounting import AssetReconciliation
    from portfolio.providers.exchanges.base import AssetBalance, NormalizedFill

#: The instant the recompute's clock reads, a whole second.
COMPUTED_AT: Final = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
#: The instant the reconciliation's clock reads: five minutes after the snapshot.
NOW: Final = datetime(2026, 10, 1, 10, 5, tzinfo=UTC)
BITGET_READ: Final = datetime(2026, 10, 1, 9, 59, tzinfo=UTC)
BINGX_READ: Final = datetime(2026, 10, 1, 9, 58, 30, tzinfo=UTC)
OBSERVED_OLDEST: Final = datetime(2026, 10, 1, 9, 45, tzinfo=UTC)
OBSERVED_NEWER: Final = datetime(2026, 10, 1, 9, 50, tzinfo=UTC)
OBSERVED_NEWEST: Final = datetime(2026, 10, 1, 9, 55, tzinfo=UTC)

#: R9's age limit, written out rather than read off the service.
A_DAY: Final = timedelta(hours=24)
ONE_MICROSECOND: Final = timedelta(microseconds=1)

SATS: Final = 100_000_000
"""Base units in one BTC and in one KAS: eight decimals each."""


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


# --------------------------------------------------------------------------------------
# Planting each side
# --------------------------------------------------------------------------------------


def buy(trade_id: int, minute: int, asset: str, quantity: str, cost: str) -> NormalizedFill:
    """A fee-less buy of `quantity` of `asset` for `cost` USDT."""
    return make_fill(
        trade_id,
        at(minute),
        symbol=f"{asset}USDT",
        base_asset=asset,
        quantity=quantity,
        price="1",
        quote_quantity=cost,
        fee_amount="0",
        fee_asset=None,
    )


def sell(trade_id: int, minute: int, asset: str, quantity: str, proceeds: str) -> NormalizedFill:
    return make_fill(
        trade_id,
        at(minute),
        symbol=f"{asset}USDT",
        base_asset=asset,
        side=FillSide.SELL,
        quantity=quantity,
        price="1",
        quote_quantity=proceeds,
        fee_amount="0",
        fee_asset=None,
    )


def history_fills() -> list[NormalizedFill]:
    """BTC 0.5, ETH 2, KAS 1000 held; DOGE bought and sold out."""
    return [
        buy(1001, 0, "BTC", "0.5", "30000"),
        buy(1002, 10, "ETH", "2", "5000"),
        buy(1003, 20, "KAS", "1000", "100"),
        buy(1004, 30, "DOGE", "10", "1"),
        sell(1005, 40, "DOGE", "10", "2"),
    ]


class Planted:
    """The ids a test planted, so an assertion or a later write can name one."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.accounts: dict[ExchangeKey, int] = {}
        self.wallets: dict[str, int] = {}


async def plant_run(factory: async_sessionmaker[AsyncSession], started_at: datetime) -> int:
    """A finished `sync_runs` row, so a balance snapshot has a run to point at."""
    async with factory() as session:
        result = await session.execute(
            text(
                "INSERT INTO sync_runs (trigger, status, started_at, finished_at, duration_ms, "
                "wallets_total, wallets_succeeded, wallets_failed) "
                "VALUES ('scheduled', 'success', :at, :at, 1, 1, 1, 0) RETURNING id"
            ),
            {"at": sqlite_timestamp(started_at)},
        )
        run_id: int = result.scalar_one()
        await session.commit()
    return run_id


async def plant_reading(
    factory: async_sessionmaker[AsyncSession],
    *,
    wallet_id: int,
    confirmed: int,
    observed_at: datetime,
    pending: int | None = None,
    decimals: int = 8,
) -> None:
    """One `balance_snapshots` row under a run of its own, as the balance sync leaves it."""
    run_id = await plant_run(factory, observed_at)
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO balance_snapshots "
                "(wallet_id, sync_run_id, confirmed, pending, decimals, observed_at) "
                "VALUES (:wallet, :run, :confirmed, :pending, :decimals, :at)"
            ),
            {
                "wallet": wallet_id,
                "run": run_id,
                "confirmed": confirmed,
                "pending": pending,
                "decimals": decimals,
                "at": sqlite_timestamp(observed_at),
            },
        )
        await session.commit()


async def plant_wallet(
    factory: async_sessionmaker[AsyncSession],
    planted: Planted,
    name: str,
    chain: ChainKey,
    address: str,
    *,
    archived: bool = False,
) -> int:
    async with factory() as session:
        wallet = await insert_wallet(
            session, user_id=planted.user_id, chain_key=chain, address=address, archived=archived
        )
    planted.wallets[name] = wallet
    return wallet


async def store_balances(
    factory: async_sessionmaker[AsyncSession],
    account_id: int,
    balances: Sequence[AssetBalance],
    read_at: datetime,
) -> None:
    """A balance reading, through the write the exchange sync makes."""
    async with factory() as session:
        await ExchangeBalanceRepository(session).replace(account_id, balances, read_at)
        await session.commit()


async def fail_balances(
    factory: async_sessionmaker[AsyncSession], account_id: int, kind: ExchangeSyncErrorKind
) -> None:
    async with factory() as session:
        await ExchangeBalanceRepository(session).record_failure(account_id, kind)
        await session.commit()


async def set_sync_status(
    factory: async_sessionmaker[AsyncSession], account_id: int, status: str
) -> None:
    """The account's fill-sync status, as the exchange sync leaves it."""
    async with factory() as session:
        await session.execute(
            text("UPDATE exchange_accounts SET sync_status = :status WHERE id = :id"),
            {"status": status, "id": account_id},
        )
        await session.commit()


async def recompute(factory: async_sessionmaker[AsyncSession], user_id: int) -> None:
    """One real recompute at `COMPUTED_AT`, over a session of its own."""
    async with factory() as session:
        service = build_accounting_service(session, clock=SettableClock(COMPUTED_AT))
        await service.recompute(user_id)


async def plant_owner_with_history(
    factory: async_sessionmaker[AsyncSession],
    fills: Sequence[NormalizedFill] | None = None,
    *,
    username: str = "owner",
    snapshot: bool = True,
) -> Planted:
    """An owner, a Bitget and a BingX account whose fills are synced, and the snapshot.

    Both accounts are `ok`, as a successful fill sync leaves them: balances are only ever
    read after one, and R9 compares a venue's reading only while its fill sync is `ok`.
    """
    async with factory() as session:
        planted = Planted(await plant_owner(session, username))
        for key in (ExchangeKey.BITGET, ExchangeKey.BINGX):
            planted.accounts[key] = await plant_account(session, planted.user_id, key)
        await plant_fills(
            session,
            planted.accounts[ExchangeKey.BITGET],
            history_fills() if fills is None else fills,
        )
    for account_id in planted.accounts.values():
        await set_sync_status(factory, account_id, "ok")
    if snapshot:
        await recompute(factory, planted.user_id)
    return planted


async def plant_the_scenario(factory: async_sessionmaker[AsyncSession]) -> Planted:
    """The module docstring's table, every side of it, every reading current at `NOW`."""
    planted = await plant_owner_with_history(factory)
    first = await plant_wallet(factory, planted, "btc-a", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    second = await plant_wallet(factory, planted, "btc-b", ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
    kaspa = await plant_wallet(factory, planted, "kas", ChainKey.KASPA, KASPA_TESTNET_V0)
    await plant_reading(factory, wallet_id=first, confirmed=40_000_000, observed_at=OBSERVED_NEWER)
    await plant_reading(
        factory, wallet_id=second, confirmed=30_000_000, observed_at=OBSERVED_OLDEST
    )
    await plant_reading(factory, wallet_id=kaspa, confirmed=600 * SATS, observed_at=OBSERVED_NEWEST)
    await store_balances(
        factory,
        planted.accounts[ExchangeKey.BITGET],
        (held("BTC", "0.2"), held("ETH", "0.5"), held("KAS", "395"), held("USDT", "5000")),
        BITGET_READ,
    )
    await store_balances(
        factory,
        planted.accounts[ExchangeKey.BINGX],
        (held("BTC", "0.1"), held("SOL", "3"), held("USDT", "250")),
        BINGX_READ,
    )
    return planted


async def view_of(
    factory: async_sessionmaker[AsyncSession], user_id: int, *, now: datetime = NOW
) -> ReconciliationView:
    """The view as the service answers it with its clock standing at `now`."""
    async with factory() as session:
        service = build_reconciliation_service(session, clock=SettableClock(now))
        return await service.reconciliation(user_id)


def by_asset(view: ReconciliationView) -> dict[str, AssetReconciliation]:
    return {row.asset: row for row in view.assets}


def by_key(view: ReconciliationView) -> dict[ExchangeKey, ExchangeBalanceSource]:
    return {source.exchange_key: source for source in view.exchanges}


def figures(row: AssetReconciliation) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    return (
        row.history_quantity,
        row.wallet_quantity,
        row.exchange_quantity,
        row.held_quantity,
        row.difference,
    )


def decimals(*values: str) -> tuple[Decimal, ...]:
    return tuple(Decimal(value) for value in values)


def compared(key: ExchangeKey, read_at: datetime) -> ExchangeBalanceSource:
    """A source whose reading is in the comparison: read, no error, no reason."""
    return ExchangeBalanceSource(key, read_at, None, None)


# --------------------------------------------------------------------------------------
# The three sides, summed
# --------------------------------------------------------------------------------------


async def test_the_three_sides_are_summed_per_asset_and_compared(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The module docstring's table, row for row."""
    planted = await plant_the_scenario(factory)

    view = await view_of(factory, planted.user_id)

    assert [row.asset for row in view.assets] == ["BTC", "ETH", "KAS", "SOL"]
    rows = by_asset(view)
    assert figures(rows["BTC"]) == decimals("0.5", "0.7", "0.3", "1.0", "0.5")
    assert rows["BTC"].status is ReconciliationStatus.HISTORY_SHORT
    assert figures(rows["ETH"]) == decimals("2", "0", "0.5", "0.5", "-1.5")
    assert rows["ETH"].status is ReconciliationStatus.HISTORY_OVER
    assert figures(rows["KAS"]) == decimals("1000", "600", "395", "995", "-5")
    assert rows["KAS"].status is ReconciliationStatus.MATCH
    assert figures(rows["SOL"]) == decimals("0", "0", "3", "3", "3")
    assert rows["SOL"].status is ReconciliationStatus.HISTORY_SHORT
    for row in view.assets:
        for figure in figures(row):
            assert type(figure) is Decimal
            assert figure.as_tuple().exponent == -18, (row.asset, figure)


async def test_the_view_carries_the_snapshots_instant_the_tolerance_and_the_age_limit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant_the_scenario(factory)

    view = await view_of(factory, planted.user_id)

    assert isinstance(view, ReconciliationView)
    assert view.computed_at == COMPUTED_AT, "the snapshot's own instant, not the request's"
    assert type(view.tolerance_pct) is Decimal
    assert view.tolerance_pct == Decimal(1)
    assert view.max_reading_age_hours == 24
    assert isinstance(view.assets, tuple)


async def test_cash_assets_and_sold_out_positions_are_not_rows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """5250 USDT sits on the venues and DOGE was sold out: neither is a finding."""
    planted = await plant_the_scenario(factory)

    rows = by_asset(await view_of(factory, planted.user_id))

    assert "USDT" not in rows
    assert "USDC" not in rows
    assert "DOGE" not in rows


async def test_a_sold_out_position_that_is_still_held_is_a_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """R2's case: the history says nothing is left and a venue still holds some."""
    planted = await plant_owner_with_history(factory)
    await store_balances(
        factory, planted.accounts[ExchangeKey.BITGET], (held("DOGE", "4"),), BITGET_READ
    )

    rows = by_asset(await view_of(factory, planted.user_id))

    assert figures(rows["DOGE"]) == decimals("0", "0", "4", "4", "4")
    assert rows["DOGE"].status is ReconciliationStatus.HISTORY_SHORT


# --------------------------------------------------------------------------------------
# The wallet side
# --------------------------------------------------------------------------------------


async def test_the_wallet_sources_count_the_compared_and_report_the_oldest_reading(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The comparison is as old as its oldest input: 09:45, not the newest 09:55."""
    planted = await plant_the_scenario(factory)

    view = await view_of(factory, planted.user_id)

    assert view.wallets == WalletSources(
        compared=3,
        stale=0,
        unread=0,
        chain_failed=0,
        failed_chains=(),
        oldest_observed_at=OBSERVED_OLDEST,
    )


async def test_an_unread_wallet_adds_nothing_and_is_counted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A wallet no sync has read is not a wallet holding zero.

    It contributes no quantity, it is counted in `unread`, and it does not make
    `oldest_observed_at` null or older.
    """
    planted = await plant_the_scenario(factory)
    await plant_wallet(factory, planted, "btc-c", ChainKey.BITCOIN, BIP350_TESTNET_V1)

    view = await view_of(factory, planted.user_id)

    assert view.wallets == WalletSources(
        compared=3,
        stale=0,
        unread=1,
        chain_failed=0,
        failed_chains=(),
        oldest_observed_at=OBSERVED_OLDEST,
    )
    assert figures(by_asset(view)["BTC"]) == decimals("0.5", "0.7", "0.3", "1.0", "0.5")


async def test_with_no_wallet_read_the_oldest_reading_is_null_and_the_side_is_empty(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant_owner_with_history(factory)
    await plant_wallet(factory, planted, "btc-a", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await plant_wallet(factory, planted, "kas", ChainKey.KASPA, KASPA_TESTNET_V0)

    view = await view_of(factory, planted.user_id)

    assert view.wallets == WalletSources(
        compared=0, stale=0, unread=2, chain_failed=0, failed_chains=(), oldest_observed_at=None
    )
    rows = by_asset(view)
    assert figures(rows["BTC"]) == decimals("0.5", "0", "0", "0", "-0.5")
    assert rows["BTC"].status is ReconciliationStatus.HISTORY_OVER, (
        "a source that contributes nothing can hide a finding, and never produces a false one"
    )


async def test_an_owner_with_no_wallets_has_nothing_compared_stale_or_unread(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant_owner_with_history(factory)

    view = await view_of(factory, planted.user_id)

    assert view.wallets == WalletSources(
        compared=0, stale=0, unread=0, chain_failed=0, failed_chains=(), oldest_observed_at=None
    )


async def test_a_wallets_latest_reading_is_used_not_an_older_one_and_not_their_sum(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three readings of one wallet: 0.9, then 0.4, then 0.25. It holds 0.25."""
    planted = await plant_owner_with_history(factory)
    wallet = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    for confirmed, observed in (
        (90_000_000, OBSERVED_OLDEST),
        (40_000_000, OBSERVED_NEWER),
        (25_000_000, OBSERVED_NEWEST),
    ):
        await plant_reading(factory, wallet_id=wallet, confirmed=confirmed, observed_at=observed)

    view = await view_of(factory, planted.user_id)

    assert by_asset(view)["BTC"].wallet_quantity == Decimal("0.25")
    assert view.wallets == WalletSources(
        compared=1,
        stale=0,
        unread=0,
        chain_failed=0,
        failed_chains=(),
        oldest_observed_at=OBSERVED_NEWEST,
    )


async def test_only_confirmed_units_are_held(factory: async_sessionmaker[AsyncSession]) -> None:
    """0.4 BTC confirmed and 5 BTC in the mempool: 0.4 is held."""
    planted = await plant_owner_with_history(factory)
    wallet = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await plant_reading(
        factory,
        wallet_id=wallet,
        confirmed=40_000_000,
        pending=5 * SATS,
        observed_at=OBSERVED_NEWER,
    )

    view = await view_of(factory, planted.user_id)

    assert by_asset(view)["BTC"].wallet_quantity == Decimal("0.4")


async def test_base_units_are_converted_with_the_decimals_the_reading_recorded(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """One base unit is 1E-8 at eight decimals and 1E-2 at two. The reading says which.

    123456789 base units at eight decimals is 1.23456789; the same count read at two
    decimals is 1234567.89. A conversion with a constant exponent gets one of them wrong.
    """
    planted = await plant_owner_with_history(factory)
    bitcoin = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    kaspa = await plant_wallet(factory, planted, "kas", ChainKey.KASPA, KASPA_TESTNET_V0)
    await plant_reading(
        factory, wallet_id=bitcoin, confirmed=123_456_789, observed_at=OBSERVED_NEWER
    )
    await plant_reading(
        factory, wallet_id=kaspa, confirmed=123_456_789, decimals=2, observed_at=OBSERVED_NEWER
    )

    rows = by_asset(await view_of(factory, planted.user_id))

    assert rows["BTC"].wallet_quantity == Decimal("1.23456789")
    assert rows["KAS"].wallet_quantity == Decimal("1234567.89")


async def test_a_wallet_is_summed_under_its_chains_asset_symbol(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`bitcoin` holds BTC and `kaspa` holds KAS: the names the positions carry."""
    planted = await plant_owner_with_history(factory, [])
    bitcoin = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    kaspa = await plant_wallet(factory, planted, "kas", ChainKey.KASPA, KASPA_TESTNET_V0)
    await plant_reading(factory, wallet_id=bitcoin, confirmed=1, observed_at=OBSERVED_NEWER)
    await plant_reading(factory, wallet_id=kaspa, confirmed=2, observed_at=OBSERVED_NEWER)

    rows = by_asset(await view_of(factory, planted.user_id))

    assert set(rows) == {"BTC", "KAS"}
    assert rows["BTC"].wallet_quantity == Decimal("0.00000001")
    assert rows["KAS"].wallet_quantity == Decimal("0.00000002")


async def test_an_archived_wallet_is_neither_summed_nor_counted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An address the owner retired, with a reading from before: not held, not unread, and
    not stale either -- it is no longer a source at all."""
    planted = await plant_the_scenario(factory)
    retired = await plant_wallet(
        factory, planted, "btc-old", ChainKey.BITCOIN, BIP350_TESTNET_V1, archived=True
    )
    await plant_reading(
        factory,
        wallet_id=retired,
        confirmed=9 * SATS,
        observed_at=OBSERVED_NEWEST,
    )

    view = await view_of(factory, planted.user_id)

    assert view.wallets == WalletSources(
        compared=3,
        stale=0,
        unread=0,
        chain_failed=0,
        failed_chains=(),
        oldest_observed_at=OBSERVED_OLDEST,
    )
    assert by_asset(view)["BTC"].wallet_quantity == Decimal("0.7")


# --------------------------------------------------------------------------------------
# The exchange side
# --------------------------------------------------------------------------------------


async def test_every_account_is_listed_by_key_with_when_it_was_read(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant_the_scenario(factory)

    view = await view_of(factory, planted.user_id)

    assert view.exchanges == (
        compared(ExchangeKey.BINGX, BINGX_READ),
        compared(ExchangeKey.BITGET, BITGET_READ),
    )


async def test_several_accounts_are_summed_per_asset(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """BTC is at both venues, 0.2 and 0.1; each other asset at one. Nothing is counted twice."""
    planted = await plant_the_scenario(factory)

    rows = by_asset(await view_of(factory, planted.user_id))

    assert rows["BTC"].exchange_quantity == Decimal("0.3")
    assert rows["ETH"].exchange_quantity == Decimal("0.5")
    assert rows["KAS"].exchange_quantity == Decimal("395")
    assert rows["SOL"].exchange_quantity == Decimal("3")


async def test_an_owner_with_no_exchange_account_has_no_exchanges_listed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        user_id = await plant_owner(session)
    await recompute(factory, user_id)

    view = await view_of(factory, user_id)

    assert view.exchanges == ()
    assert view.assets == ()
    assert view.computed_at == COMPUTED_AT


async def test_the_sums_are_exact_past_38_digits(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Each venue's balance fits the column; their sum needs 39 digits and loses none.

    60000000000000000000.000000000000000001 twice is
    120000000000000000000.000000000000000002. A `SUM()` in SQL would have made it a float.
    """
    planted = await plant_owner_with_history(factory, [])
    amount = "60000000000000000000.000000000000000001"
    for key, read_at in ((ExchangeKey.BITGET, BITGET_READ), (ExchangeKey.BINGX, BINGX_READ)):
        await store_balances(factory, planted.accounts[key], (held("KAS", amount),), read_at)

    (row,) = (await view_of(factory, planted.user_id)).assets

    assert row.exchange_quantity == Decimal("120000000000000000000.000000000000000002")
    assert row.held_quantity == Decimal("120000000000000000000.000000000000000002")
    assert row.status is ReconciliationStatus.HISTORY_SHORT


# --------------------------------------------------------------------------------------
# R9: a venue's reading is compared only while it is current
# --------------------------------------------------------------------------------------


def test_the_age_limit_is_twenty_four_hours() -> None:
    assert timedelta(hours=24) == MAX_READING_AGE
    assert MAX_READING_AGE_HOURS == 24
    assert timedelta(hours=MAX_READING_AGE_HOURS) == MAX_READING_AGE


def test_the_four_reasons_and_their_wire_forms_in_the_order_they_are_tested() -> None:
    assert [(reason.name, reason.value) for reason in NotComparedReason] == [
        ("READ_FAILED", "read_failed"),
        ("NEVER_READ", "never_read"),
        ("SYNC_FAILED", "sync_failed"),
        ("OUT_OF_DATE", "out_of_date"),
    ]


def account(
    *,
    read_at: datetime | None = BITGET_READ,
    error: ExchangeSyncErrorKind | None = None,
    sync_status: AccountSyncStatus = AccountSyncStatus.OK,
) -> AccountBalances:
    """One account as the repository lists it, for the pure rule."""
    return AccountBalances(
        exchange_key=ExchangeKey.BITGET,
        sync_status=sync_status,
        balances_read_at=read_at,
        balances_error=error,
        balances=(StoredBalance("BTC", Decimal("0.2")),) if read_at is not None else (),
    )


LONG_AGO: Final = NOW - timedelta(days=30)

REASONS: Final = [
    pytest.param(account(), None, id="read, ok and recent: compared"),
    pytest.param(
        account(error=ExchangeSyncErrorKind.UNAVAILABLE), "read_failed", id="the last read failed"
    ),
    pytest.param(account(read_at=None), "never_read", id="never read"),
    pytest.param(
        account(sync_status=AccountSyncStatus.ERROR), "sync_failed", id="the fill sync failed"
    ),
    pytest.param(
        account(sync_status=AccountSyncStatus.AUTH_FAILED), "sync_failed", id="the key was refused"
    ),
    pytest.param(
        account(sync_status=AccountSyncStatus.NEVER_SYNCED),
        "sync_failed",
        id="a reading on an account that is not synced",
    ),
    pytest.param(account(read_at=LONG_AGO), "out_of_date", id="read a month ago"),
    # Precedence: the first that applies, in the order the reasons are declared.
    pytest.param(
        account(read_at=None, error=ExchangeSyncErrorKind.AUTH),
        "read_failed",
        id="failed and never read: read_failed",
    ),
    pytest.param(
        account(
            read_at=LONG_AGO,
            error=ExchangeSyncErrorKind.SCHEMA,
            sync_status=AccountSyncStatus.ERROR,
        ),
        "read_failed",
        id="everything wrong at once: read_failed",
    ),
    pytest.param(
        account(read_at=None, sync_status=AccountSyncStatus.AUTH_FAILED),
        "never_read",
        id="never read and not synced: never_read",
    ),
    pytest.param(
        account(read_at=LONG_AGO, sync_status=AccountSyncStatus.ERROR),
        "sync_failed",
        id="sync failed and old: sync_failed",
    ),
]


@pytest.mark.parametrize(("listed", "reason"), REASONS)
def test_the_reason_a_reading_is_not_compared_is_the_first_that_applies(
    listed: AccountBalances, reason: str | None
) -> None:
    """The pure rule, by itself: a venue's reading is current when its last balance read
    succeeded, a reading exists, its fill sync is `ok` and the reading is at most a day old."""
    found = not_compared_reason(listed, NOW)

    assert (None if found is None else found.value) == reason


@pytest.mark.parametrize(
    ("age", "reason"),
    [
        pytest.param(timedelta(0), None, id="read this instant"),
        pytest.param(A_DAY - ONE_MICROSECOND, None, id="one microsecond inside"),
        pytest.param(A_DAY, None, id="exactly twenty-four hours: at most, so current"),
        pytest.param(A_DAY + ONE_MICROSECOND, "out_of_date", id="one microsecond past"),
        pytest.param(A_DAY * 2, "out_of_date", id="two days"),
        pytest.param(-timedelta(hours=3), None, id="dated after the clock: current"),
        pytest.param(-A_DAY * 400, None, id="dated far after the clock: current"),
    ],
)
def test_a_venues_reading_is_current_for_exactly_twenty_four_hours(
    age: timedelta, reason: str | None
) -> None:
    """ "At most" twenty-four hours: the boundary itself is current, a microsecond past is not.

    A reading dated after the clock -- the clock stepped back since -- has no positive age
    and is current: refusing it would discard the newest reading there is.
    """
    found = not_compared_reason(account(read_at=NOW - age), NOW)

    assert (None if found is None else found.value) == reason


async def test_an_account_never_read_is_listed_and_adds_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant_owner_with_history(factory)
    await store_balances(
        factory, planted.accounts[ExchangeKey.BITGET], (held("BTC", "0.2"),), BITGET_READ
    )

    view = await view_of(factory, planted.user_id)

    assert view.exchanges == (
        ExchangeBalanceSource(ExchangeKey.BINGX, None, None, NotComparedReason.NEVER_READ),
        compared(ExchangeKey.BITGET, BITGET_READ),
    )
    assert by_asset(view)["BTC"].exchange_quantity == Decimal("0.2")


async def test_a_failed_read_reports_its_kind_and_its_last_reading_is_left_out(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bitget's last read failed. Its rows are still stored, and none of them is summed.

    BTC is then 0.1, BingX's alone; ETH and KAS, held only at Bitget, have nothing on the
    exchange side. The reading is kept on disk and its age is still reported -- it is the
    comparison that no longer trusts it.
    """
    planted = await plant_the_scenario(factory)
    await fail_balances(
        factory, planted.accounts[ExchangeKey.BITGET], ExchangeSyncErrorKind.UNAVAILABLE
    )

    view = await view_of(factory, planted.user_id)

    assert view.exchanges == (
        compared(ExchangeKey.BINGX, BINGX_READ),
        ExchangeBalanceSource(
            ExchangeKey.BITGET,
            BITGET_READ,
            ExchangeSyncErrorKind.UNAVAILABLE,
            NotComparedReason.READ_FAILED,
        ),
    )
    rows = by_asset(view)
    assert figures(rows["BTC"]) == decimals("0.5", "0.7", "0.1", "0.8", "0.3")
    assert figures(rows["ETH"]) == decimals("2", "0", "0", "0", "-2")
    assert figures(rows["KAS"]) == decimals("1000", "600", "0", "600", "-400")
    assert rows["SOL"].exchange_quantity == Decimal(3), "BingX is still compared"


async def test_a_read_that_never_succeeded_reports_its_kind_and_no_reading(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant_owner_with_history(factory)
    await fail_balances(
        factory, planted.accounts[ExchangeKey.BINGX], ExchangeSyncErrorKind.INSUFFICIENT_SCOPE
    )

    view = await view_of(factory, planted.user_id)

    assert view.exchanges == (
        ExchangeBalanceSource(
            ExchangeKey.BINGX,
            None,
            ExchangeSyncErrorKind.INSUFFICIENT_SCOPE,
            NotComparedReason.READ_FAILED,
        ),
        ExchangeBalanceSource(ExchangeKey.BITGET, None, None, NotComparedReason.NEVER_READ),
    )
    assert by_asset(view)["BTC"].exchange_quantity == 0


@pytest.mark.parametrize("status", ["error", "auth_failed", "never_synced"])
async def test_a_venue_whose_fill_sync_is_not_ok_is_left_out_with_its_reading_intact(
    factory: async_sessionmaker[AsyncSession], status: str
) -> None:
    """The balance read itself last succeeded, and has stopped being refreshed.

    Balances are read only after a successful fill sync. An account whose fill sync has since
    failed, or whose key was refused so that timers skip it, keeps a reading with no error on
    it and nothing saying it is ageing -- until this rule names it.
    """
    planted = await plant_the_scenario(factory)
    await set_sync_status(factory, planted.accounts[ExchangeKey.BITGET], status)

    view = await view_of(factory, planted.user_id)

    assert by_key(view)[ExchangeKey.BITGET] == ExchangeBalanceSource(
        ExchangeKey.BITGET, BITGET_READ, None, NotComparedReason.SYNC_FAILED
    )
    assert by_key(view)[ExchangeKey.BINGX] == compared(ExchangeKey.BINGX, BINGX_READ)
    rows = by_asset(view)
    assert rows["BTC"].exchange_quantity == Decimal("0.1")
    assert rows["KAS"].exchange_quantity == 0
    assert rows["ETH"].exchange_quantity == 0


@pytest.mark.parametrize(
    ("age", "is_compared"),
    [
        pytest.param(A_DAY, True, id="exactly twenty-four hours old"),
        pytest.param(A_DAY + ONE_MICROSECOND, False, id="one microsecond older"),
        pytest.param(-timedelta(minutes=5), True, id="dated after the clock"),
        pytest.param(-A_DAY * 3, True, id="dated three days after the clock"),
    ],
)
async def test_a_venues_reading_is_summed_until_it_is_more_than_a_day_old(
    factory: async_sessionmaker[AsyncSession], age: timedelta, is_compared: bool
) -> None:
    """Through the stored rows: the timer was switched off, or the credentials removed."""
    planted = await plant_owner_with_history(factory)
    read_at = NOW - age
    await store_balances(
        factory, planted.accounts[ExchangeKey.BITGET], (held("BTC", "0.2"),), read_at
    )

    view = await view_of(factory, planted.user_id)

    source = by_key(view)[ExchangeKey.BITGET]
    assert source.balances_read_at == read_at
    if is_compared:
        assert source.not_compared_reason is None
        assert by_asset(view)["BTC"].exchange_quantity == Decimal("0.2")
    else:
        assert source.not_compared_reason is NotComparedReason.OUT_OF_DATE
        assert by_asset(view)["BTC"].exchange_quantity == 0


async def test_coins_withdrawn_after_a_reading_that_was_then_kept_are_not_counted_twice(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The case R9 was ruled on, start to finish.

    The owner holds exactly the 0.5 BTC the history accounts for. It sat on Bitget when the
    venue was last read. Then it was withdrawn to a wallet, the wallet was read, and Bitget's
    next balance read failed -- so Bitget's stored reading still says 0.5 BTC.

    Summed, that reading and the wallet's make 1 BTC held against 0.5 in the history: a
    `history_short` for coins that do not exist, with advice to record an opening balance.
    With the kept reading left out, 0.5 is held, 0.5 is accounted for, and it is a `match`.
    """
    planted = await plant_owner_with_history(factory, [buy(1001, 0, "BTC", "0.5", "30000")])
    bitget = planted.accounts[ExchangeKey.BITGET]
    await store_balances(factory, bitget, (held("BTC", "0.5"),), BITGET_READ - timedelta(hours=2))
    wallet = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await plant_reading(factory, wallet_id=wallet, confirmed=50_000_000, observed_at=OBSERVED_NEWER)
    await fail_balances(factory, bitget, ExchangeSyncErrorKind.UNAVAILABLE)

    view = await view_of(factory, planted.user_id)

    (row,) = view.assets
    assert figures(row) == decimals("0.5", "0.5", "0", "0.5", "0")
    assert row.status is ReconciliationStatus.MATCH
    assert by_key(view)[ExchangeKey.BITGET].not_compared_reason is NotComparedReason.READ_FAILED


async def test_a_venue_that_stopped_syncing_before_a_withdrawal_is_not_counted_twice(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The same double count with no failed balance read at all, which is why the fill sync
    is part of the rule: the venue's fill sync began failing, so its balances stopped being
    read, and the coins left for a wallet in the meantime."""
    planted = await plant_owner_with_history(factory, [buy(1001, 0, "KAS", "1000", "100")])
    bitget = planted.accounts[ExchangeKey.BITGET]
    await store_balances(factory, bitget, (held("KAS", "1000"),), BITGET_READ)
    await set_sync_status(factory, bitget, "error")
    wallet = await plant_wallet(factory, planted, "kas", ChainKey.KASPA, KASPA_TESTNET_V0)
    await plant_reading(
        factory, wallet_id=wallet, confirmed=1000 * SATS, observed_at=OBSERVED_NEWEST
    )

    view = await view_of(factory, planted.user_id)

    (row,) = view.assets
    assert figures(row) == decimals("1000", "1000", "0", "1000", "0")
    assert row.status is ReconciliationStatus.MATCH
    assert by_key(view)[ExchangeKey.BITGET].not_compared_reason is NotComparedReason.SYNC_FAILED


async def test_a_reading_that_becomes_current_again_is_compared_again(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A failure is not for ever: the next successful read puts the venue back."""
    planted = await plant_the_scenario(factory)
    bitget = planted.accounts[ExchangeKey.BITGET]
    await fail_balances(factory, bitget, ExchangeSyncErrorKind.RATE_LIMITED)
    assert by_asset(await view_of(factory, planted.user_id))["KAS"].exchange_quantity == 0

    await store_balances(factory, bitget, (held("KAS", "400"),), NOW - timedelta(minutes=1))

    view = await view_of(factory, planted.user_id)
    assert by_key(view)[ExchangeKey.BITGET].not_compared_reason is None
    assert by_asset(view)["KAS"].exchange_quantity == Decimal(400)


# --------------------------------------------------------------------------------------
# R9: a wallet's reading is compared only while it is current
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "is_compared"),
    [
        pytest.param(timedelta(0), True, id="read this instant"),
        pytest.param(A_DAY - ONE_MICROSECOND, True, id="one microsecond inside"),
        pytest.param(A_DAY, True, id="exactly twenty-four hours old"),
        pytest.param(A_DAY + ONE_MICROSECOND, False, id="one microsecond older"),
        pytest.param(A_DAY * 30, False, id="a month old"),
        pytest.param(-timedelta(minutes=5), True, id="dated after the clock"),
        pytest.param(-A_DAY * 3, True, id="dated three days after the clock"),
    ],
)
async def test_a_wallets_reading_is_summed_until_it_is_more_than_a_day_old(
    factory: async_sessionmaker[AsyncSession], age: timedelta, is_compared: bool
) -> None:
    """A wallet no balance sync reads any more keeps its last reading where it was.

    No chain is recorded as failed here: the age limit decides alone, as it does when the
    balance timer is off or no run finishes.

    A reading dated after the clock -- the clock stepped back since -- is current however far
    after: its age is not positive, and it is the newest reading there is.
    """
    planted = await plant_owner_with_history(factory)
    wallet = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    observed_at = NOW - age
    await plant_reading(factory, wallet_id=wallet, confirmed=40_000_000, observed_at=observed_at)

    view = await view_of(factory, planted.user_id)

    if is_compared:
        assert view.wallets == WalletSources(
            compared=1,
            stale=0,
            unread=0,
            chain_failed=0,
            failed_chains=(),
            oldest_observed_at=observed_at,
        )
        assert by_asset(view)["BTC"].wallet_quantity == Decimal("0.4")
    else:
        assert view.wallets == WalletSources(
            compared=0, stale=1, unread=0, chain_failed=0, failed_chains=(), oldest_observed_at=None
        )
        assert by_asset(view)["BTC"].wallet_quantity == 0


async def test_the_three_wallet_counts_add_up_and_the_oldest_is_among_the_compared(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """One wallet compared, one stale, one unread. The stale one's reading is the oldest
    there is, and it is not the `oldest_observed_at`: it bounds nothing that is compared."""
    planted = await plant_owner_with_history(factory)
    current = await plant_wallet(factory, planted, "a", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    stale = await plant_wallet(factory, planted, "b", ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
    await plant_wallet(factory, planted, "c", ChainKey.BITCOIN, BIP350_TESTNET_V1)
    await plant_reading(
        factory, wallet_id=current, confirmed=40_000_000, observed_at=OBSERVED_NEWER
    )
    await plant_reading(
        factory, wallet_id=stale, confirmed=900_000_000, observed_at=NOW - timedelta(days=3)
    )

    view = await view_of(factory, planted.user_id)

    assert view.wallets == WalletSources(
        compared=1,
        stale=1,
        unread=1,
        chain_failed=0,
        failed_chains=(),
        oldest_observed_at=OBSERVED_NEWER,
    )
    assert by_asset(view)["BTC"].wallet_quantity == Decimal("0.4"), "9 BTC of a stale reading"


async def test_a_wallet_is_judged_by_its_latest_reading_not_by_an_older_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Read three days ago and again ten minutes ago: current. Staleness is the latest
    reading's age, and a history of old readings does not make a wallet stale."""
    planted = await plant_owner_with_history(factory)
    wallet = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await plant_reading(
        factory, wallet_id=wallet, confirmed=90_000_000, observed_at=NOW - timedelta(days=3)
    )
    await plant_reading(
        factory, wallet_id=wallet, confirmed=25_000_000, observed_at=OBSERVED_NEWEST
    )

    view = await view_of(factory, planted.user_id)

    assert view.wallets == WalletSources(
        compared=1,
        stale=0,
        unread=0,
        chain_failed=0,
        failed_chains=(),
        oldest_observed_at=OBSERVED_NEWEST,
    )
    assert by_asset(view)["BTC"].wallet_quantity == Decimal("0.25")


async def test_coins_that_left_a_wallet_no_sync_has_read_since_are_not_counted_twice(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The wallet half of the case R9 was ruled on.

    1000 KAS sat in a wallet when it was last read, three days ago, and no balance sync has
    finished since: the balance timer is off. The coins were deposited to Bitget, whose
    reading is ten minutes old. The wallet's old reading and the venue's new one would make
    2000 held against 1000.

    This is the residual the age limit still covers. A chain recorded as failed leaves the
    wallet out at once, whatever the reading's age (spec 028).
    """
    planted = await plant_owner_with_history(factory, [buy(1001, 0, "KAS", "1000", "100")])
    wallet = await plant_wallet(factory, planted, "kas", ChainKey.KASPA, KASPA_TESTNET_V0)
    await plant_reading(
        factory, wallet_id=wallet, confirmed=1000 * SATS, observed_at=NOW - timedelta(days=3)
    )
    await store_balances(
        factory, planted.accounts[ExchangeKey.BITGET], (held("KAS", "1000"),), BITGET_READ
    )

    view = await view_of(factory, planted.user_id)

    (row,) = view.assets
    assert figures(row) == decimals("1000", "0", "1000", "1000", "0")
    assert row.status is ReconciliationStatus.MATCH
    assert view.wallets == WalletSources(
        compared=0, stale=1, unread=0, chain_failed=0, failed_chains=(), oldest_observed_at=None
    )


async def test_every_reading_is_held_to_one_instant(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The clock is read once. A clock that jumped two days between two reads would judge
    the venues by one instant and the wallets by another."""
    planted = await plant_the_scenario(factory)

    class JumpingClock:
        def __init__(self) -> None:
            self.reads = 0

        def __call__(self) -> datetime:
            self.reads += 1
            return NOW + timedelta(days=2) * (self.reads - 1)

    clock = JumpingClock()
    async with factory() as session:
        view = await build_reconciliation_service(session, clock=clock).reconciliation(
            planted.user_id
        )

    assert clock.reads == 1
    assert view.wallets.compared == 3
    assert all(source.not_compared_reason is None for source in view.exchanges)


# --------------------------------------------------------------------------------------
# No snapshot, and an empty one
# --------------------------------------------------------------------------------------


async def test_with_no_snapshot_nothing_is_compared_and_the_sources_are_still_answered(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """ "Not computed" is never "every balance is unaccounted for".

    The venues and the wallets hold plenty, and with no snapshot every unit of it would be
    reported as missing from the history. `assets` is empty instead, and `computed_at` is
    `None`, which is how a reader tells the two apart. Each source still says how it stands.
    """
    planted = await plant_owner_with_history(factory, snapshot=False)
    wallet = await plant_wallet(factory, planted, "btc", ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await plant_wallet(factory, planted, "kas", ChainKey.KASPA, KASPA_TESTNET_V0)
    await plant_reading(factory, wallet_id=wallet, confirmed=40_000_000, observed_at=OBSERVED_NEWER)
    await store_balances(
        factory, planted.accounts[ExchangeKey.BITGET], (held("BTC", "0.2"),), BITGET_READ
    )
    await fail_balances(factory, planted.accounts[ExchangeKey.BINGX], ExchangeSyncErrorKind.AUTH)

    view = await view_of(factory, planted.user_id)

    assert view == ReconciliationView(
        computed_at=None,
        tolerance_pct=Decimal(1),
        max_reading_age_hours=24,
        assets=(),
        exchanges=(
            ExchangeBalanceSource(
                ExchangeKey.BINGX,
                None,
                ExchangeSyncErrorKind.AUTH,
                NotComparedReason.READ_FAILED,
            ),
            compared(ExchangeKey.BITGET, BITGET_READ),
        ),
        wallets=WalletSources(
            compared=1,
            stale=0,
            unread=1,
            chain_failed=0,
            failed_chains=(),
            oldest_observed_at=OBSERVED_NEWER,
        ),
    )


async def test_a_snapshot_of_an_empty_history_is_computed_and_reports_what_is_held(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The recompute ran and found no event. That is a history, of nothing: every coin held
    is then missing from it, which is exactly the case the check exists for."""
    planted = await plant_owner_with_history(factory, [])
    await store_balances(
        factory, planted.accounts[ExchangeKey.BITGET], (held("BTC", "0.2"),), BITGET_READ
    )

    view = await view_of(factory, planted.user_id)

    assert view.computed_at == COMPUTED_AT
    (row,) = view.assets
    assert figures(row) == decimals("0", "0", "0.2", "0.2", "0.2")
    assert row.status is ReconciliationStatus.HISTORY_SHORT


# --------------------------------------------------------------------------------------
# Scoping, and what the service does not do
# --------------------------------------------------------------------------------------


async def test_another_owners_history_wallets_and_balances_are_not_in_the_view(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    planted = await plant_the_scenario(factory)
    other = await plant_owner_with_history(
        factory, [buy(9001, 0, "ZZOTHER", "7", "7")], username="second"
    )
    wallet = await plant_wallet(factory, other, "kas", ChainKey.KASPA, BIP350_TESTNET_V1)
    await plant_reading(
        factory, wallet_id=wallet, confirmed=4242 * SATS, observed_at=OBSERVED_OLDEST
    )
    await store_balances(
        factory, other.accounts[ExchangeKey.BITGET], (held("BTC", "4242"),), BITGET_READ
    )

    mine = await view_of(factory, planted.user_id)
    theirs = await view_of(factory, other.user_id)

    assert [row.asset for row in mine.assets] == ["BTC", "ETH", "KAS", "SOL"]
    assert figures(by_asset(mine)["BTC"]) == decimals("0.5", "0.7", "0.3", "1.0", "0.5")
    assert figures(by_asset(mine)["KAS"]) == decimals("1000", "600", "395", "995", "-5")
    assert mine.wallets.compared == 3
    assert [row.asset for row in theirs.assets] == ["BTC", "KAS", "ZZOTHER"]
    assert by_asset(theirs)["BTC"].exchange_quantity == Decimal(4242)
    assert theirs.wallets == WalletSources(
        compared=1,
        stale=0,
        unread=0,
        chain_failed=0,
        failed_chains=(),
        oldest_observed_at=OBSERVED_OLDEST,
    )


async def test_the_service_only_reads_and_never_aggregates_a_quantity_in_sql(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every statement of one request: a `SELECT`, and none that sums or orders money.

    The exchange balances are `TEXT` in SQLite; `SUM(quantity)` there is a float. The sums
    are `money.add`'s, in Python, over rows loaded whole. Nor is a reading's age decided in
    SQL: `balances_read_at` and `observed_at` are text there, and compared in Python.
    """
    planted = await plant_the_scenario(factory)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        del conn, cursor, rest
        statements.append(statement)

    async with factory() as session:
        engine = session.bind
        assert engine is not None
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            service = build_reconciliation_service(session, clock=SettableClock(NOW))
            view = await service.reconciliation(planted.user_id)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert len(view.assets) == 4, "the control: the read returned the comparison"
    assert statements, "nothing was recorded"
    assert [s for s in statements if not s.lstrip().upper().startswith("SELECT")] == []
    about_balances = [s for s in statements if "exchange_balances" in s]
    assert about_balances, statements
    for statement in about_balances:
        upper = statement.upper()
        assert "SUM(" not in upper, statement
        assert "TOTAL(" not in upper, statement
        assert "AVG(" not in upper, statement
        tail = upper.split("FROM", 1)[1]
        assert "QUANTITY" not in tail, f"quantity is compared or ordered in SQL: {statement}"
    for statement in statements:
        upper = statement.upper()
        if " WHERE " in upper:
            where = upper.split(" WHERE ", 1)[1]
            assert "BALANCES_READ_AT" not in where, statement
            assert "OBSERVED_AT" not in where, statement


def test_the_service_module_imports_no_provider() -> None:
    """Criterion 7: a router imports this module, so nothing here may reach a venue.

    The import contracts are the enforcement; this is the same fact read off the module
    itself, so that it fails beside the code that broke it.
    """
    source = reconciliation_module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:  # noqa: PTH123
        imports = [
            line.strip() for line in handle if line.lstrip().startswith(("import ", "from "))
        ]

    assert imports, "the scan read nothing"
    assert [line for line in imports if "providers" in line] == []
    assert [line for line in imports if "httpx" in line] == []
    assert [line for line in imports if "fastapi" in line] == []


async def test_a_snapshot_that_changes_under_every_read_is_an_error_not_a_guess(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The history side goes through `AccountingService.read_snapshot`, the one consistent
    read: when it gives up, so does the comparison, rather than pairing one snapshot's
    header with another's positions."""
    planted = await plant_the_scenario(factory)

    async def never_consistent(self: object, user_id: int) -> None:
        del self, user_id
        raise SnapshotReadError

    async with factory() as session:
        service = build_reconciliation_service(session, clock=SettableClock(NOW))
        assert isinstance(service, ReconciliationService)
        monkeypatch.setattr(
            "portfolio.services.accounting.AccountingService.read_snapshot", never_consistent
        )
        with pytest.raises(SnapshotReadError):
            await service.reconciliation(planted.user_id)
