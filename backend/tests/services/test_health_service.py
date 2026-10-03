"""Spec 030 (#23), criterion 8: `HealthService.detail`, section by section, over a real file.

The service is built with `build_health_service`, exactly as the dependency builds it, over a
migrated SQLite file under `tmp_path` and a clock this test names. Rows are planted through
the application's own repositories where one exists. What is pinned:

* **each section says what its last recorded attempt says** -- chains, exchanges, prices and
  the holdings check -- and the timers are served in a fixed order, `disabled` when never
  built;
* **a section that raises is `unavailable` and the others answer**, with
  `health_section_failed` naming the section and the exception's class, never its message;
  the session is rolled back after a failure, a failure of the rollback is swallowed, and a
  cancellation is not a section's outcome;
* **the clock is read once**, so the timers and the prices are judged at one instant;
* **the chain's `detail`, the provider's text, is never served**.

The rules each state comes from are `tests/domain/test_health.py`'s; this module is about
what the service reads and how it composes them.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import text
from structlog.testing import capture_logs

from portfolio.domain.chains import ChainKey
from portfolio.domain.exchanges import AccountSyncStatus, ExchangeKey
from portfolio.domain.health import (
    PriceHealthState,
    ReconciliationHealthState,
    SchedulerName,
    SchedulerState,
    SectionState,
    SourceState,
)
from portfolio.repositories.exchange_balances import ExchangeBalanceRepository
from portfolio.repositories.exchange_sync_runs import ExchangeSyncErrorKind
from portfolio.repositories.exchanges import ExchangeAccountRepository
from portfolio.repositories.prices import PriceRepository
from portfolio.repositories.sync_runs import (
    ChainOutcome,
    SyncErrorKind,
    SyncRunRepository,
    SyncRunStatus,
    SyncTrigger,
)
from portfolio.repositories.wallets import WalletRepository
from portfolio.services.backup import BackupService
from portfolio.services.health import (
    SCHEDULER_ORDER,
    ChainHealth,
    ChainsHealth,
    ExchangeHealth,
    ExchangesHealth,
    HealthDetail,
    HealthSection,
    HealthService,
    PricesHealth,
    ReconciliationHealth,
    TimerLike,
    build_health_service,
    utc_now,
)
from portfolio.services.prices import STALE_AFTER
from portfolio.services.reconciliation import ReconciliationService, build_reconciliation_service
from portfolio.services.scheduler import SchedulerStatus
from tests.accounting_harness import plant_price
from tests.address_vectors import BIP173_TESTNET_P2WPKH, KASPA_TESTNET_V0
from tests.balance_harness import insert_user, insert_wallet
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW: Final = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
FAILED_DETAIL: Final = "provider text that must stay in the run log"

#: The message every exploding read raises. The log must name the class and never this.
EXPLOSION: Final = "free text that may quote a row: do not log me"


class ExplosionError(RuntimeError):
    """What a section's read raises in these tests."""


async def explode(*_arguments: object, **_keywords: object) -> Any:
    raise ExplosionError(EXPLOSION)


class World:
    """A migrated file, its owner, and the backup service the detail composes."""

    def __init__(
        self, factory: async_sessionmaker[AsyncSession], user_id: int, backup: BackupService
    ) -> None:
        self.factory = factory
        self.user_id = user_id
        self.backup = backup


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    async with migrated_sessionmaker(tmp_path) as factory:
        async with factory() as session:
            user_id = await insert_user(session)
        backup = BackupService(
            database_url="sqlite+aiosqlite:///:memory:",
            directory=tmp_path / "backups",
            enabled=True,
            interval_minutes=1440,
            keep_daily=7,
            keep_weekly=4,
            clock=lambda: NOW,
        )
        yield World(factory, user_id, backup)


class FakeTimer:
    """A `TimerLike` answering a chosen status, recording the instant it was asked about."""

    def __init__(self, status: SchedulerStatus) -> None:
        self.answer = status
        self.asked: list[datetime] = []

    def status(self, now: datetime) -> SchedulerStatus:
        self.asked.append(now)
        return self.answer


async def detail(
    world: World,
    *,
    timers: dict[SchedulerName, TimerLike | None] | None = None,
    clock: Callable[[], datetime] = lambda: NOW,
) -> HealthDetail:
    async with world.factory() as session:
        service = build_health_service(
            session, backup=world.backup, timers=timers or {}, clock=clock
        )
        return await service.detail(world.user_id)


async def finished_run(
    world: World, *, finished_at: datetime, chains: Sequence[ChainOutcome]
) -> None:
    async with world.factory() as session:
        repository = SyncRunRepository(session)
        opened = await repository.open_run(
            trigger=SyncTrigger.SCHEDULED, started_at=finished_at, wallets_total=1
        )
        run_id = opened.id
        await session.commit()
        await repository.finish_run(
            run_id,
            status=SyncRunStatus.SUCCESS,
            finished_at=finished_at,
            duration_ms=1,
            wallets_succeeded=1,
            wallets_failed=0,
            chains=chains,
        )
        await session.commit()


def ok(chain_key: str) -> ChainOutcome:
    return ChainOutcome(chain_key=chain_key, status=SyncRunStatus.SUCCESS, wallets_read=1)


def failed(chain_key: str, kind: SyncErrorKind = SyncErrorKind.RATE_LIMITED) -> ChainOutcome:
    return ChainOutcome(
        chain_key=chain_key,
        status=SyncRunStatus.FAILED,
        wallets_read=0,
        error_kind=kind,
        detail=FAILED_DETAIL,
    )


async def add_wallet(
    world: World, chain: ChainKey, address: str, *, archived: bool = False
) -> None:
    async with world.factory() as session:
        await insert_wallet(
            session, user_id=world.user_id, chain_key=chain, address=address, archived=archived
        )


async def add_account(world: World, key: ExchangeKey) -> int:
    async with world.factory() as session:
        account = await ExchangeAccountRepository(session).ensure(
            user_id=world.user_id, exchange_key=key, created_at=NOW - timedelta(days=30)
        )
        await session.commit()
        return account.id


# --------------------------------------------------------------------------------------
# An empty installation
# --------------------------------------------------------------------------------------


async def test_a_fresh_installation_answers_every_section(world: World) -> None:
    """No timer built, no run, no account, no price, no snapshot: every section still answers."""
    served = await detail(world)

    assert served.backup.state.value == "pending"
    assert [timer.name for timer in served.schedulers] == list(SCHEDULER_ORDER)
    assert {timer.state for timer in served.schedulers} == {SchedulerState.DISABLED}
    assert all(timer.last_tick_at is None for timer in served.schedulers)
    assert all(timer.last_tick_succeeded is None for timer in served.schedulers)
    assert served.chains == ChainsHealth(state=SectionState.OK, items=())
    assert served.exchanges == ExchangesHealth(state=SectionState.OK, items=())
    assert served.prices == PricesHealth(state=PriceHealthState.NEVER, latest_fetched_at=None)
    assert served.reconciliation == ReconciliationHealth(
        state=ReconciliationHealthState.NOT_COMPUTED,
        computed_at=None,
        assets_compared=0,
        assets_mismatched=0,
        sources_not_compared=0,
    )


# --------------------------------------------------------------------------------------
# The timers
# --------------------------------------------------------------------------------------


async def test_each_timer_is_served_as_it_reports_itself_in_a_fixed_order(world: World) -> None:
    """Handed in out of order, one `None` and one missing: served in the lifespan's order."""
    ticked = NOW - timedelta(minutes=3)
    balance = FakeTimer(SchedulerStatus(SchedulerState.OK, ticked, True))
    backup = FakeTimer(SchedulerStatus(SchedulerState.LATE, ticked - timedelta(days=3), False))
    prices = FakeTimer(SchedulerStatus(SchedulerState.STOPPED, None, None))

    served = await detail(
        world,
        timers={
            SchedulerName.BACKUP: backup,
            SchedulerName.EXCHANGE_SYNC: None,
            SchedulerName.BALANCE_SYNC: balance,
            SchedulerName.PRICE_REFRESH: prices,
        },
    )

    assert [(timer.name, timer.state) for timer in served.schedulers] == [
        (SchedulerName.BALANCE_SYNC, SchedulerState.OK),
        (SchedulerName.PRICE_REFRESH, SchedulerState.STOPPED),
        (SchedulerName.EXCHANGE_SYNC, SchedulerState.DISABLED),
        (SchedulerName.BACKUP, SchedulerState.LATE),
    ]
    assert served.schedulers[0].last_tick_at == ticked
    assert served.schedulers[0].last_tick_succeeded is True
    assert served.schedulers[3].last_tick_at == ticked - timedelta(days=3)
    assert served.schedulers[3].last_tick_succeeded is False
    assert served.schedulers[1].last_tick_at is None


async def test_a_timer_missing_from_the_mapping_is_disabled(world: World) -> None:
    only = FakeTimer(SchedulerStatus(SchedulerState.OK, NOW, True))

    served = await detail(world, timers={SchedulerName.PRICE_REFRESH: only})

    states = {timer.name: timer.state for timer in served.schedulers}
    assert states == {
        SchedulerName.BALANCE_SYNC: SchedulerState.DISABLED,
        SchedulerName.PRICE_REFRESH: SchedulerState.OK,
        SchedulerName.EXCHANGE_SYNC: SchedulerState.DISABLED,
        SchedulerName.BACKUP: SchedulerState.DISABLED,
    }


async def test_the_clock_is_read_once_for_the_timers_and_the_prices(world: World) -> None:
    """A clock that jumps an hour per reading: every timer and the prices see the first one.

    The price is fetched exactly `STALE_AFTER` before the first reading, so it is fresh at that
    instant and stale at any later one. A service that read the clock again for the prices, or
    per timer, answers `stale` or hands the timers different instants.
    """
    readings = [NOW + timedelta(hours=hour) for hour in range(10)]
    calls = iter(readings)
    async with world.factory() as session:
        await plant_price(session, symbol="BTC", amount=Decimal(1), as_of=NOW - STALE_AFTER)
    timers = {
        name: FakeTimer(SchedulerStatus(SchedulerState.OK, None, None)) for name in SCHEDULER_ORDER
    }

    async with world.factory() as session:
        service = HealthService(
            backup=world.backup,
            timers=dict(timers),
            sync_runs=SyncRunRepository(session),
            wallets=WalletRepository(session),
            exchanges=ExchangeAccountRepository(session),
            prices=PriceRepository(session),
            reconciliation=build_reconciliation_service(session, clock=lambda: NOW),
            clock=lambda: next(calls),
        )
        served = await service.detail(world.user_id)

    assert served.prices.state is PriceHealthState.FRESH
    assert all(timer.asked == [NOW] for timer in timers.values())
    assert next(calls) == readings[1], "the clock was read exactly once"


def test_the_default_clock_is_aware_utc() -> None:
    before = datetime.now(UTC)
    reading = utc_now()

    assert reading.tzinfo is UTC
    assert before <= reading <= datetime.now(UTC)


# --------------------------------------------------------------------------------------
# The chains
# --------------------------------------------------------------------------------------


async def test_a_wallets_chain_with_no_finished_run_is_never(world: World) -> None:
    await add_wallet(world, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)

    served = await detail(world)

    assert served.chains.items == (
        ChainHealth(
            chain_key="bitcoin",
            state=SourceState.NEVER,
            last_success_at=None,
            last_error_kind=None,
        ),
    )


async def test_each_chain_is_its_newest_outcome_with_its_newest_success(world: World) -> None:
    await add_wallet(world, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await add_wallet(world, ChainKey.KASPA, KASPA_TESTNET_V0)
    first = NOW - timedelta(hours=2)
    second = NOW - timedelta(hours=1)
    await finished_run(world, finished_at=first, chains=[ok("bitcoin"), ok("kaspa")])
    await finished_run(world, finished_at=second, chains=[ok("bitcoin"), failed("kaspa")])

    served = await detail(world)

    assert served.chains == ChainsHealth(
        state=SectionState.OK,
        items=(
            ChainHealth(
                chain_key="bitcoin",
                state=SourceState.OK,
                last_success_at=second,
                last_error_kind=None,
            ),
            ChainHealth(
                chain_key="kaspa",
                state=SourceState.FAILING,
                last_success_at=first,
                last_error_kind=SyncErrorKind.RATE_LIMITED,
            ),
        ),
    )


async def test_a_chain_that_recovered_carries_no_error_kind(world: World) -> None:
    """The kind is the newest outcome's, and only while failing: a recovery clears it."""
    await add_wallet(world, ChainKey.KASPA, KASPA_TESTNET_V0)
    await finished_run(world, finished_at=NOW - timedelta(hours=2), chains=[failed("kaspa")])
    await finished_run(world, finished_at=NOW - timedelta(hours=1), chains=[ok("kaspa")])

    (kaspa,) = (await detail(world)).chains.items

    assert kaspa.state is SourceState.OK
    assert kaspa.last_error_kind is None


async def test_a_chain_in_the_latest_run_is_listed_without_a_wallet(world: World) -> None:
    """A wallet archived since the run: the run's outcome is still news until the next one."""
    await add_wallet(world, ChainKey.KASPA, KASPA_TESTNET_V0, archived=True)
    await finished_run(world, finished_at=NOW, chains=[failed("kaspa", SyncErrorKind.RESPONSE)])

    (kaspa,) = (await detail(world)).chains.items

    assert kaspa.chain_key == "kaspa"
    assert kaspa.state is SourceState.FAILING
    assert kaspa.last_error_kind is SyncErrorKind.RESPONSE


async def test_a_chain_only_in_an_older_run_with_no_active_wallet_is_not_listed(
    world: World,
) -> None:
    """R8: the latest finished run's chains plus the active wallets' chains, and no others."""
    await add_wallet(world, ChainKey.KASPA, KASPA_TESTNET_V0, archived=True)
    await add_wallet(world, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await finished_run(world, finished_at=NOW - timedelta(hours=2), chains=[ok("kaspa")])
    await finished_run(world, finished_at=NOW - timedelta(hours=1), chains=[ok("bitcoin")])

    served = await detail(world)

    assert [chain.chain_key for chain in served.chains.items] == ["bitcoin"]


async def test_the_chains_are_sorted_by_key(world: World) -> None:
    await add_wallet(world, ChainKey.KASPA, KASPA_TESTNET_V0)
    await add_wallet(world, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)

    served = await detail(world)

    assert [chain.chain_key for chain in served.chains.items] == ["bitcoin", "kaspa"]


async def test_the_providers_detail_is_never_part_of_a_chains_health(world: World) -> None:
    await add_wallet(world, ChainKey.KASPA, KASPA_TESTNET_V0)
    await finished_run(world, finished_at=NOW, chains=[failed("kaspa")])

    served = await detail(world)

    assert "detail" not in ChainHealth.__slots__
    assert FAILED_DETAIL not in repr(served)


# --------------------------------------------------------------------------------------
# The exchanges
# --------------------------------------------------------------------------------------


async def test_each_account_carries_its_sync_and_its_balance_reading(world: World) -> None:
    synced_at = NOW - timedelta(minutes=20)
    read_at = NOW - timedelta(minutes=19)
    bingx = await add_account(world, ExchangeKey.BINGX)
    bitget = await add_account(world, ExchangeKey.BITGET)
    async with world.factory() as session:
        await ExchangeAccountRepository(session).mark_synced(bitget, synced_at=synced_at)
        await ExchangeBalanceRepository(session).replace(bitget, [], read_at)
        await ExchangeAccountRepository(session).set_status(bingx, AccountSyncStatus.AUTH_FAILED)
        await session.commit()

    served = await detail(world)

    assert served.exchanges == ExchangesHealth(
        state=SectionState.OK,
        items=(
            ExchangeHealth(
                exchange_key=ExchangeKey.BINGX,
                sync_state=AccountSyncStatus.AUTH_FAILED,
                last_synced_at=None,
                balances_state=SourceState.NEVER,
                balances_read_at=None,
            ),
            ExchangeHealth(
                exchange_key=ExchangeKey.BITGET,
                sync_state=AccountSyncStatus.OK,
                last_synced_at=synced_at,
                balances_state=SourceState.OK,
                balances_read_at=read_at,
            ),
        ),
    )


async def test_a_failed_balance_read_is_failing_and_keeps_the_last_good_instant(
    world: World,
) -> None:
    read_at = NOW - timedelta(hours=5)
    bitget = await add_account(world, ExchangeKey.BITGET)
    async with world.factory() as session:
        await ExchangeBalanceRepository(session).replace(bitget, [], read_at)
        await ExchangeBalanceRepository(session).record_failure(
            bitget, ExchangeSyncErrorKind.UNAVAILABLE
        )
        await ExchangeAccountRepository(session).set_status(bitget, AccountSyncStatus.ERROR)
        await session.commit()

    (account,) = (await detail(world)).exchanges.items

    assert account.sync_state is AccountSyncStatus.ERROR
    assert account.balances_state is SourceState.FAILING
    assert account.balances_read_at == read_at


# --------------------------------------------------------------------------------------
# The prices
# --------------------------------------------------------------------------------------


async def test_prices_exactly_at_the_age_limit_are_fresh(world: World) -> None:
    fetched = NOW - STALE_AFTER
    async with world.factory() as session:
        await plant_price(session, symbol="BTC", amount=Decimal(1), as_of=fetched)

    assert (await detail(world)).prices == PricesHealth(
        state=PriceHealthState.FRESH, latest_fetched_at=fetched
    )


async def test_prices_past_the_age_limit_are_stale(world: World) -> None:
    fetched = NOW - STALE_AFTER - timedelta(seconds=1)
    async with world.factory() as session:
        await plant_price(session, symbol="BTC", amount=Decimal(1), as_of=fetched)

    assert (await detail(world)).prices == PricesHealth(
        state=PriceHealthState.STALE, latest_fetched_at=fetched
    )


async def test_the_newest_price_row_is_the_one_judged(world: World) -> None:
    newest = NOW - timedelta(minutes=5)
    async with world.factory() as session:
        await plant_price(session, symbol="BTC", amount=Decimal(1), as_of=NOW - timedelta(days=2))
        await plant_price(session, symbol="KAS", amount=Decimal(1), as_of=newest)

    assert (await detail(world)).prices.latest_fetched_at == newest


# --------------------------------------------------------------------------------------
# A section that fails does not fail the others
# --------------------------------------------------------------------------------------

#: Each section, and the read that builds it.
SECTION_READS: Final = {
    HealthSection.CHAINS: (SyncRunRepository, "chain_histories"),
    HealthSection.EXCHANGES: (ExchangeAccountRepository, "list_for_user"),
    HealthSection.PRICES: (PriceRepository, "latest_fetched_at"),
    HealthSection.RECONCILIATION: (ReconciliationService, "reconciliation"),
}


async def plant_every_section(world: World) -> None:
    """Something in each section, so an answering section is visibly not an empty one."""
    await add_wallet(world, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    await finished_run(world, finished_at=NOW, chains=[ok("bitcoin")])
    await add_account(world, ExchangeKey.BITGET)
    async with world.factory() as session:
        await plant_price(session, symbol="BTC", amount=Decimal(1), as_of=NOW)


def health_section_failures(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(entry) for entry in entries if entry["event"] == "health_section_failed"]


@pytest.mark.parametrize("section", list(HealthSection), ids=[s.value for s in HealthSection])
async def test_a_failing_section_is_unavailable_and_the_others_answer(
    world: World, section: HealthSection, monkeypatch: pytest.MonkeyPatch
) -> None:
    await plant_every_section(world)
    owner, method = SECTION_READS[section]
    monkeypatch.setattr(owner, method, explode)
    timer = FakeTimer(SchedulerStatus(SchedulerState.OK, NOW, True))

    with capture_logs() as entries:
        served = await detail(world, timers={SchedulerName.BALANCE_SYNC: timer})

    assert health_section_failures(entries) == [
        {
            "event": "health_section_failed",
            "section": section.value,
            "error_type": "ExplosionError",
            "log_level": "error",
        }
    ]
    assert EXPLOSION not in repr(entries)
    unavailable = {
        HealthSection.CHAINS: served.chains == ChainsHealth(SectionState.UNAVAILABLE, ()),
        HealthSection.EXCHANGES: served.exchanges == ExchangesHealth(SectionState.UNAVAILABLE, ()),
        HealthSection.PRICES: served.prices == PricesHealth(PriceHealthState.UNAVAILABLE, None),
        HealthSection.RECONCILIATION: served.reconciliation
        == ReconciliationHealth(ReconciliationHealthState.UNAVAILABLE, None, None, None, None),
    }
    assert unavailable == {name: name is section for name in HealthSection}
    # The others answered with what was planted, and the backup and the timers regardless.
    if section is not HealthSection.CHAINS:
        assert [chain.state for chain in served.chains.items] == [SourceState.OK]
    if section is not HealthSection.EXCHANGES:
        assert [account.exchange_key for account in served.exchanges.items] == [ExchangeKey.BITGET]
    if section is not HealthSection.PRICES:
        assert served.prices.state is PriceHealthState.FRESH
    if section is not HealthSection.RECONCILIATION:
        assert served.reconciliation.state is ReconciliationHealthState.NOT_COMPUTED
    assert served.schedulers[0].state is SchedulerState.OK
    assert served.backup.state.value == "pending"


async def test_every_section_failing_at_once_still_answers_the_backup_and_the_timers(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for owner, method in SECTION_READS.values():
        monkeypatch.setattr(owner, method, explode)

    with capture_logs() as entries:
        served = await detail(world)

    assert [entry["section"] for entry in health_section_failures(entries)] == [
        "chains",
        "exchanges",
        "prices",
        "reconciliation",
    ]
    assert served.chains.state is SectionState.UNAVAILABLE
    assert served.exchanges.state is SectionState.UNAVAILABLE
    assert served.prices.state is PriceHealthState.UNAVAILABLE
    assert served.reconciliation.state is ReconciliationHealthState.UNAVAILABLE
    assert len(served.schedulers) == 4
    assert served.backup.state.value == "pending"


async def test_the_chains_section_fails_on_any_of_its_three_reads(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The latest run and the wallets are read inside the section's `try` too."""
    for owner, method in (
        (SyncRunRepository, "latest_finished"),
        (WalletRepository, "list_for_user"),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(owner, method, explode)
            served = await detail(world)
        assert served.chains.state is SectionState.UNAVAILABLE, method


class Recorder:
    """An `after_failure` that counts its calls, and can be told to fail itself."""

    def __init__(self, *, raises: bool = False) -> None:
        self.calls = 0
        self.raises = raises

    async def __call__(self) -> None:
        self.calls += 1
        if self.raises:
            message = "the rollback failed too"
            raise RuntimeError(message)


def service_with(session: AsyncSession, world: World, after_failure: Recorder) -> HealthService:
    return HealthService(
        backup=world.backup,
        timers={},
        sync_runs=SyncRunRepository(session),
        wallets=WalletRepository(session),
        exchanges=ExchangeAccountRepository(session),
        prices=PriceRepository(session),
        reconciliation=build_reconciliation_service(session, clock=lambda: NOW),
        after_failure=after_failure,
        clock=lambda: NOW,
    )


async def test_the_session_is_rolled_back_after_each_failed_section_and_only_then(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    after = Recorder()
    async with world.factory() as session:
        await service_with(session, world, after).detail(world.user_id)
        assert after.calls == 0, "nothing failed, so nothing is rolled back"

        monkeypatch.setattr(SyncRunRepository, "chain_histories", explode)
        monkeypatch.setattr(PriceRepository, "latest_fetched_at", explode)
        await service_with(session, world, after).detail(world.user_id)

    assert after.calls == 2


async def test_with_no_after_failure_a_failed_section_is_still_unavailable(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`after_failure` is optional: a service built without one still answers every section."""
    await plant_every_section(world)
    monkeypatch.setattr(SyncRunRepository, "chain_histories", explode)

    async with world.factory() as session:
        service = HealthService(
            backup=world.backup,
            timers={},
            sync_runs=SyncRunRepository(session),
            wallets=WalletRepository(session),
            exchanges=ExchangeAccountRepository(session),
            prices=PriceRepository(session),
            reconciliation=build_reconciliation_service(session, clock=lambda: NOW),
            clock=lambda: NOW,
        )
        with capture_logs() as entries:
            served = await service.detail(world.user_id)

    assert served.chains.state is SectionState.UNAVAILABLE
    assert served.exchanges.state is SectionState.OK
    assert served.prices.state is PriceHealthState.FRESH
    assert [entry["section"] for entry in health_section_failures(entries)] == ["chains"]


async def test_a_rollback_that_fails_is_swallowed_and_the_next_section_still_answers(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await plant_every_section(world)
    after = Recorder(raises=True)
    monkeypatch.setattr(SyncRunRepository, "chain_histories", explode)

    async with world.factory() as session:
        served = await service_with(session, world, after).detail(world.user_id)

    assert after.calls == 1
    assert served.chains.state is SectionState.UNAVAILABLE
    assert served.exchanges.state is SectionState.OK
    assert served.prices.state is PriceHealthState.FRESH


async def test_build_health_service_rolls_back_a_session_a_failed_statement_left_open(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real `after_failure`: the session is usable by the next section after a failure."""
    rolled_back: list[bool] = []

    async def bad_statement(self: SyncRunRepository) -> object:
        await self._session.execute(text("SELECT no_such_column FROM sync_runs"))
        return {}

    await plant_every_section(world)
    monkeypatch.setattr(SyncRunRepository, "chain_histories", bad_statement)
    async with world.factory() as session:
        original = session.rollback

        async def recording_rollback() -> None:
            rolled_back.append(True)
            await original()

        monkeypatch.setattr(session, "rollback", recording_rollback)
        service = build_health_service(session, backup=world.backup, timers={}, clock=lambda: NOW)
        served = await service.detail(world.user_id)

    assert rolled_back == [True]
    assert served.chains.state is SectionState.UNAVAILABLE
    assert served.exchanges.state is SectionState.OK
    assert served.prices.state is PriceHealthState.FRESH


async def test_a_cancellation_is_not_a_sections_outcome(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def cancelled(*_arguments: object, **_keywords: object) -> object:
        raise asyncio.CancelledError

    monkeypatch.setattr(PriceRepository, "latest_fetched_at", cancelled)

    with capture_logs() as entries, pytest.raises(asyncio.CancelledError):
        await detail(world)

    assert health_section_failures(entries) == []
