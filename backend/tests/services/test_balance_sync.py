"""Criteria 3 and 4: per-chain failure isolation, and the row every run writes.

This is the module the issue's opening paragraph is about. "Per-provider failure isolation
is the point" means two separate claims, and each one has its own way of being got wrong:

| Claim | The implementation that breaks it |
|---|---|
| one chain's failure costs no other chain its data | one `try` around the `gather`, or one |
|  | transaction rolled back at the end |
| a failure is attributed to whoever caused it | one `except Exception` that records |
|  | everything as `unavailable` |

The second is the quiet one. A `TypeError` in our own parser reported as "Kaspa is
unavailable" is a defect that hides for months behind a vendor's name, and
`test_an_internal_error_is_recorded_as_internal_and_not_as_a_vendor_outage` is written to
fail the moment the two catch clauses become one.

## Two sessions, and that is the whole design of this module

`migrated_sessionmaker` hands out a factory, so the service runs on one session and every
assertion is made through another. That distinction is not fussiness:

* a row read back through the session that wrote it can be answered out of the identity
  map, so "the snapshot is there" would be true of a transaction that never committed;
* criterion 4 says the run row exists **before** any provider is called, which is only
  observable from a connection that is not the one doing the work.

`test_a_run_row_exists_before_any_provider_is_called` therefore reads the table from inside
the provider stub, through the second factory, while the first session is still in the
middle of the run.

## Nothing here opens a socket and nothing here reads a real clock

The wall clock and the monotonic clock are both injected, and they are injected
*separately* -- which is the only way `test_duration_comes_from_the_monotonic_clock` can
step one backwards while the other moves forward.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest
from structlog.testing import capture_logs

from portfolio.domain.addresses import AddressInvalidError, AddressRejection
from portfolio.domain.chains import ChainKey
from portfolio.providers.errors import (
    DuplicateProviderError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
    UnknownChainError,
)
from portfolio.repositories.sync_runs import (
    SyncErrorKind,
    SyncRunRepository,
    SyncRunStatus,
    SyncTrigger,
)
from portfolio.services.balance_sync import BalanceSyncService, build_balance_sync_service
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP350_TESTNET_V1,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V1_KEY,
)
from tests.balance_harness import (
    StubChainProvider,
    insert_user,
    insert_wallet,
    plant_wallets,
    rows_of,
    snapshots,
    sync_run_chains,
    sync_runs,
)
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.base import AddressBalance, ChainProvider
    from portfolio.repositories.sync_runs import ChainOutcome, SyncRunSummary

STARTED_AT: Final = datetime(2026, 9, 24, 0, 0, 0, tzinfo=UTC)
FINISHED_AT: Final = STARTED_AT + timedelta(seconds=3)

#: The two monotonic reads a run makes, and their difference. Integer milliseconds, which
#: is what `providers.http.monotonic_ms` returns and what `duration_ms` stores.
MONOTONIC_START: Final = 1_000_000
MONOTONIC_END: Final = MONOTONIC_START + 3128
EXPECTED_DURATION_MS: Final = MONOTONIC_END - MONOTONIC_START

BTC_UNITS: Final = 123_456_789
KAS_UNITS: Final = 1_000_000_000

#: The largest value `db.types.BaseUnits` will store, plus one. `align_balances` passes it
#: through -- it refuses a negative count, not a large one -- so it is a balance that reads
#: cleanly and then cannot be written, which is the "snapshot write fails after a
#: successful read" case the spec carries and nothing else in the suite produces.
OVER_SIXTY_FOUR_BITS: Final = 2**63


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """One migrated file, and a factory that can hand out more than one connection to it."""
    async with migrated_sessionmaker(tmp_path) as factory:
        yield factory


def service_over(
    session: AsyncSession,
    providers: Mapping[ChainKey, StubChainProvider],
    *,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], int] | None = None,
    missing: Mapping[ChainKey, BaseException] | None = None,
) -> BalanceSyncService:
    """The sync service with its three injected collaborators and nothing else.

    `missing` is how a chain with **no provider at all** is produced: `provider_for` raises
    rather than returning, exactly as `get_chain_provider` does for an unregistered key.
    That is a different failure from a provider that was built and then refused, and the
    two must not land in the same bucket.
    """
    refusals = dict(missing or {})

    def provider_for(chain_key: str) -> ChainProvider:
        key = ChainKey(chain_key)
        refusal = refusals.get(key)
        if refusal is not None:
            raise refusal
        return providers[key]

    return build_balance_sync_service(
        session,
        provider_for=provider_for,
        clock=clock if clock is not None else _fixed_clock(),
        monotonic=monotonic if monotonic is not None else _stepping_monotonic(),
    )


def _fixed_clock() -> Callable[[], datetime]:
    """A wall clock that answers `STARTED_AT` and then `FINISHED_AT`, then repeats the last.

    Two reads is what a run makes. Repeating the last value rather than raising on a third
    keeps a test that adds an assertion from failing for a reason that is not about time.
    """
    readings = iter((STARTED_AT, FINISHED_AT))
    last = [STARTED_AT]

    def clock() -> datetime:
        last[0] = next(readings, last[0])
        return last[0]

    return clock


def _stepping_monotonic() -> Callable[[], int]:
    """The monotonic counterpart, with the same two-reads-then-hold behaviour."""
    readings = iter((MONOTONIC_START, MONOTONIC_END))
    last = [MONOTONIC_START]

    def monotonic() -> int:
        last[0] = next(readings, last[0])
        return last[0]

    return monotonic


async def run_sync(
    factory: async_sessionmaker[AsyncSession],
    providers: Mapping[ChainKey, StubChainProvider],
    *,
    trigger: SyncTrigger = SyncTrigger.MANUAL,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], int] | None = None,
    missing: Mapping[ChainKey, BaseException] | None = None,
) -> SyncRunSummary:
    """One run, on its own session, closed before anything is read back."""
    async with factory() as session:
        service = service_over(
            session, providers, clock=clock, monotonic=monotonic, missing=missing
        )
        return await service.sync(trigger)


def outcomes(summary: SyncRunSummary) -> dict[str, ChainOutcome]:
    """The summary's chain lines keyed by chain, so an assertion names a chain not an index."""
    return {outcome.chain_key: outcome for outcome in summary.chains}


# --------------------------------------------------------------------------------------
# Criterion 3: one chain's failure is the other chain's business, and nobody else's
# --------------------------------------------------------------------------------------


async def test_one_chain_failing_leaves_the_other_chains_snapshots_written(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The criterion, stated as the rows on disk rather than as the summary in memory.

    Read back through a **second** session, which is the assertion that matters: a service
    that staged the Bitcoin snapshots and then rolled the transaction back when Kaspa
    raised would satisfy every check made through its own session and would leave the
    database exactly as it found it. That rollback is what a single transaction around the
    whole run produces, and it is the failure this criterion exists to name.
    """
    planted = await plant_wallets(
        sessions,
        bitcoin=(BIP173_TESTNET_P2WPKH, BIP350_TESTNET_V1),
        kaspa=(KASPA_TESTNET_V0, KASPA_TESTNET_V1_KEY),
    )
    bitcoin = StubChainProvider(
        ChainKey.BITCOIN,
        {BIP173_TESTNET_P2WPKH: BTC_UNITS, BIP350_TESTNET_V1: 0},
    )
    kaspa = StubChainProvider(
        ChainKey.KASPA,
        raises=ProviderUnavailableError("no configured endpoint answered"),
    )

    summary = await run_sync(sessions, {ChainKey.BITCOIN: bitcoin, ChainKey.KASPA: kaspa})

    stored = await snapshots(sessions)
    assert {row["wallet_id"] for row in stored} == set(planted.bitcoin)
    assert {row["confirmed"] for row in stored} == {BTC_UNITS, 0}
    assert summary.status == SyncRunStatus.PARTIAL
    assert (summary.wallets_total, summary.wallets_succeeded, summary.wallets_failed) == (4, 2, 2)
    lines = outcomes(summary)
    assert lines["bitcoin"].status == "success"
    assert lines["bitcoin"].wallets_read == 2
    assert lines["kaspa"].status == "failed"
    assert lines["kaspa"].wallets_read == 0
    # The failing chain was really asked, so "no snapshots" is not "never called".
    assert kaspa.calls != []


async def test_the_chain_rows_on_disk_say_the_same_thing_the_summary_does(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A summary is built by the call that wrote the rows, so it cannot vouch for them.

    Reading `sync_run_chains` back through a second identity is what makes the summary
    trustworthy -- the same argument `test_every_refreshed_pair_reaches_the_table` makes
    about the price report, one issue earlier.
    """
    await plant_wallets(sessions)
    kaspa = StubChainProvider(ChainKey.KASPA, raises=ProviderResponseError("unusable document"))
    providers = {
        ChainKey.BITCOIN: StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS}),
        ChainKey.KASPA: kaspa,
    }

    summary = await run_sync(sessions, providers)

    rows = {row["chain_key"]: row for row in await sync_run_chains(sessions)}
    assert set(rows) == {"bitcoin", "kaspa"}
    assert all(row["sync_run_id"] == summary.run_id for row in rows.values())
    assert rows["bitcoin"]["status"] == "success"
    assert rows["bitcoin"]["error_kind"] is None
    assert rows["bitcoin"]["detail"] is None
    assert rows["bitcoin"]["wallets_read"] == 1
    assert rows["kaspa"]["status"] == "failed"
    assert rows["kaspa"]["error_kind"] == SyncErrorKind.RESPONSE
    assert rows["kaspa"]["detail"] == "unusable document"
    assert rows["kaspa"]["wallets_read"] == 0


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ProviderUnavailableError("nothing answered"), SyncErrorKind.UNAVAILABLE),
        (ProviderRateLimitedError("slow down"), SyncErrorKind.RATE_LIMITED),
        (ProviderResponseError("that is not a balance"), SyncErrorKind.RESPONSE),
    ],
    ids=["unavailable", "rate limited", "response"],
)
async def test_a_provider_error_is_recorded_with_its_kind(
    sessions: async_sessionmaker[AsyncSession],
    failure: ProviderError,
    expected: SyncErrorKind,
) -> None:
    """#6's vocabulary survives the trip to the table, member for member.

    **The rate-limited row is the one that can fail.** `ProviderRateLimitedError` is a
    *subclass* of `ProviderUnavailableError` -- deliberately, so that a caller which only
    cares about "try again later" need not enumerate both -- so a mapping written as a
    chain of `isinstance` checks with the base class first records a 429 as `unavailable`
    and loses the only actionable fact in it: the interval is too short and the fix is a
    configuration change rather than patience.

    The three are parametrized rather than asserted in one body so that a failure names
    which kind was lost instead of failing on whichever came first.
    """
    await plant_wallets(sessions, bitcoin=(), kaspa=(KASPA_TESTNET_V0,))
    kaspa = StubChainProvider(ChainKey.KASPA, raises=failure)

    summary = await run_sync(sessions, {ChainKey.KASPA: kaspa})

    rows = await sync_run_chains(sessions)
    assert [row["error_kind"] for row in rows] == [expected]
    assert outcomes(summary)["kaspa"].error_kind == expected
    assert summary.status == SyncRunStatus.FAILED


async def test_a_chain_with_no_provider_at_all_is_recorded_as_unknown_chain(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A wiring mistake, isolated like any other failure rather than aborting the run.

    A `chain_key` in the table with nothing registered for it is not a vendor outage and
    not a bug in a parser -- it is a chain module that was never imported, and it has its
    own member for that reason. The Bitcoin half proves it is isolated: a wiring mistake
    in one chain must not cost the other its balances.
    """
    planted = await plant_wallets(sessions)
    bitcoin = StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS})

    summary = await run_sync(
        sessions,
        {ChainKey.BITCOIN: bitcoin},
        missing={ChainKey.KASPA: UnknownChainError("kaspa", ("bitcoin",))},
    )

    rows = {row["chain_key"]: row for row in await sync_run_chains(sessions)}
    assert rows["kaspa"]["error_kind"] == SyncErrorKind.UNKNOWN_CHAIN
    assert rows["bitcoin"]["status"] == "success"
    assert {row["wallet_id"] for row in await snapshots(sessions)} == set(planted.bitcoin)
    assert summary.status == SyncRunStatus.PARTIAL


async def test_an_internal_error_is_recorded_as_internal_and_not_as_a_vendor_outage(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The distinction the whole two-clause design exists for, and the one that hides.

    A `TypeError` raised where a provider's parser runs is *our* bug. Recording it as
    `unavailable` files "the code is wrong" under "the vendor is having a bad day", and it
    stays there for as long as nobody happens to check the vendor's status page against
    our own log. The assertion is written twice on purpose: once for the member it must
    be, and once for the member it must **not** be, so a mapping that collapsed the two
    catch clauses into one fails on the second line with the reason spelled out.

    The Bitcoin chain in the same run is the isolation half: our bug in one parser must
    not take another chain's data with it, which is the outcome the spec says it rejected
    the simpler design for.
    """
    planted = await plant_wallets(sessions)
    bitcoin = StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS})
    kaspa = StubChainProvider(ChainKey.KASPA, raises=TypeError("'NoneType' is not subscriptable"))

    summary = await run_sync(sessions, {ChainKey.BITCOIN: bitcoin, ChainKey.KASPA: kaspa})

    rows = {row["chain_key"]: row for row in await sync_run_chains(sessions)}
    assert rows["kaspa"]["error_kind"] == SyncErrorKind.INTERNAL
    assert rows["kaspa"]["error_kind"] != SyncErrorKind.UNAVAILABLE, (
        "a TypeError in our own code must never be reported as the chain being unavailable"
    )
    assert outcomes(summary)["kaspa"].error_kind == SyncErrorKind.INTERNAL
    assert {row["wallet_id"] for row in await snapshots(sessions)} == set(planted.bitcoin)
    assert summary.status == SyncRunStatus.PARTIAL


async def test_the_two_catch_clauses_really_are_two(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The falsification control for the test above, and it is not redundant.

    `test_an_internal_error_is_recorded_as_internal` passes for an implementation that
    records **everything** as `internal` -- which is the opposite mistake and equally
    wrong, because it would file a real vendor outage as our bug. Driving the two failures
    through the same service in one run, and asserting the two rows differ, is what pins
    the distinction rather than one side of it.
    """
    await plant_wallets(sessions)
    providers = {
        ChainKey.BITCOIN: StubChainProvider(ChainKey.BITCOIN, raises=ValueError("our own bug")),
        ChainKey.KASPA: StubChainProvider(
            ChainKey.KASPA, raises=ProviderUnavailableError("their outage")
        ),
    }

    summary = await run_sync(sessions, providers)

    rows = {row["chain_key"]: row for row in await sync_run_chains(sessions)}
    assert rows["bitcoin"]["error_kind"] == SyncErrorKind.INTERNAL
    assert rows["kaspa"]["error_kind"] == SyncErrorKind.UNAVAILABLE
    assert rows["bitcoin"]["error_kind"] != rows["kaspa"]["error_kind"]
    assert summary.status == SyncRunStatus.FAILED


async def test_both_chains_failing_is_still_exactly_one_run_row(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 4 at its hardest: the run that produced nothing is still a run.

    A `sync_runs` table that only records the runs that worked is a table an operator
    cannot use to answer "has this thing been running at all", which is the question #23
    will be built on.
    """
    await plant_wallets(sessions)
    providers = {
        ChainKey.BITCOIN: StubChainProvider(
            ChainKey.BITCOIN, raises=ProviderUnavailableError("down")
        ),
        ChainKey.KASPA: StubChainProvider(ChainKey.KASPA, raises=ProviderUnavailableError("down")),
    }

    summary = await run_sync(sessions, providers)

    runs = await sync_runs(sessions)
    assert len(runs) == 1
    assert runs[0]["status"] == SyncRunStatus.FAILED
    assert (runs[0]["wallets_succeeded"], runs[0]["wallets_failed"]) == (0, 2)
    assert await snapshots(sessions) == []
    assert len(await sync_run_chains(sessions)) == 2
    assert summary.run_id == runs[0]["id"]


async def test_a_snapshot_that_cannot_be_written_is_isolated_like_any_other_failure(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The spec's fourth carried case: the read succeeded and the write did not.

    Driven through `BaseUnits`, which refuses a count outside the signed 64-bit range. The
    balance parses cleanly -- `align_balances` refuses a *negative* count, not a large one
    -- so the failure happens where the spec says it happens, at the write, after a
    provider has already answered.

    It is `internal` rather than a vendor kind, because a value our column cannot hold is
    our problem. And the other chain still has to keep its rows, which is what makes this a
    test about isolation rather than about a type decorator: an implementation that flushed
    every chain's snapshots in one transaction at the end loses Bitcoin's data to Kaspa's
    unwritable number.
    """
    planted = await plant_wallets(sessions)
    providers = {
        ChainKey.BITCOIN: StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS}),
        ChainKey.KASPA: StubChainProvider(ChainKey.KASPA, {KASPA_TESTNET_V0: OVER_SIXTY_FOUR_BITS}),
    }

    summary = await run_sync(sessions, providers)

    stored = await snapshots(sessions)
    assert {row["wallet_id"] for row in stored} == set(planted.bitcoin)
    rows = {row["chain_key"]: row for row in await sync_run_chains(sessions)}
    assert rows["kaspa"]["status"] == "failed"
    assert rows["kaspa"]["error_kind"] == SyncErrorKind.INTERNAL
    assert summary.status == SyncRunStatus.PARTIAL


async def test_a_cancellation_is_not_absorbed_into_a_chain_failure(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The spec's stated reason for not passing `return_exceptions=True`, made checkable.

    `gather(..., return_exceptions=True)` hands a `CancelledError` back as an ordinary
    result, so a shutdown arriving mid-run would be written to the table as "Kaspa
    failed" -- a permanent record of a vendor problem that never happened. `CancelledError`
    inherits from `BaseException` rather than `Exception` precisely so that an `except
    Exception` cannot swallow it, and this asserts the service leaves it that way.

    What happens to the run row afterwards is the lifespan's business, not this service's:
    the spec gives that to the shutdown sweep, so nothing is asserted about it here beyond
    the absence of a chain failure attributed to the cancellation.
    """
    await plant_wallets(sessions)
    providers = {
        ChainKey.BITCOIN: StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS}),
        ChainKey.KASPA: StubChainProvider(ChainKey.KASPA, raises=asyncio.CancelledError()),
    }

    with pytest.raises(asyncio.CancelledError):
        await run_sync(sessions, providers)

    rows = await sync_run_chains(sessions)
    assert [
        row for row in rows if row["chain_key"] == "kaspa" and row["status"] == "failed"
    ] == [], "a cancellation is a shutdown, not a chain that failed"


async def test_the_chains_are_read_concurrently_rather_than_one_after_the_other(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`asyncio.gather`, proven by a rendezvous rather than by timing.

    Each chain's stub waits for the other to have arrived before it answers. Under a
    sequential implementation the first one waits forever and this fails on the timeout
    with that message; under `gather` both arrive and both proceed. No assertion here is a
    measurement of how fast the host is, which is the property `tests/providers/harness.py`
    insists on for the same reason.

    It matters beyond tidiness: the sync runs on a Raspberry Pi against two public APIs
    whose slow path is seconds, and a scheduler whose tick takes the sum rather than the
    maximum is one that starts overlapping itself.
    """
    await plant_wallets(sessions)
    arrived = {ChainKey.BITCOIN: asyncio.Event(), ChainKey.KASPA: asyncio.Event()}

    def rendezvous(mine: ChainKey, theirs: ChainKey) -> Callable[[Sequence[str]], Awaitable[None]]:
        async def wait(addresses: Sequence[str]) -> None:
            del addresses
            arrived[mine].set()
            await asyncio.wait_for(arrived[theirs].wait(), timeout=5)

        return wait

    providers = {
        ChainKey.BITCOIN: StubChainProvider(
            ChainKey.BITCOIN,
            {BIP173_TESTNET_P2WPKH: BTC_UNITS},
            on_fetch=rendezvous(ChainKey.BITCOIN, ChainKey.KASPA),
        ),
        ChainKey.KASPA: StubChainProvider(
            ChainKey.KASPA,
            {KASPA_TESTNET_V0: KAS_UNITS},
            on_fetch=rendezvous(ChainKey.KASPA, ChainKey.BITCOIN),
        ),
    }

    summary = await run_sync(sessions, providers)

    assert summary.status == SyncRunStatus.SUCCESS
    assert all(event.is_set() for event in arrived.values())


# --------------------------------------------------------------------------------------
# Criterion 4: the row, when it is written, and what is in it
# --------------------------------------------------------------------------------------


async def test_a_run_row_exists_before_any_provider_is_called(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The row is written first, and that is asserted from *inside* the provider call.

    Asserting it afterwards would prove only that a row exists by the end, which is what a
    single `INSERT` at the finish also produces -- and a run the process died in the middle
    of would then have left no trace at all. The observation is made through a second
    session, so what it sees is what was committed rather than what was staged.

    `seen` is checked at the end because a stub that was never called would make every
    assertion inside it vacuous, and that is exactly the shape a broken harness takes.
    """
    await plant_wallets(sessions, kaspa=())
    seen: list[dict[str, object]] = []

    async def read_the_run_row(addresses: Sequence[str]) -> None:
        del addresses
        async with sessions() as observer:
            seen.extend(
                await rows_of(
                    observer,
                    "SELECT id, status, started_at, finished_at, duration_ms, wallets_total "
                    "FROM sync_runs ORDER BY id",
                )
            )

    bitcoin = StubChainProvider(
        ChainKey.BITCOIN,
        {BIP173_TESTNET_P2WPKH: BTC_UNITS},
        on_fetch=read_the_run_row,
    )

    summary = await run_sync(sessions, {ChainKey.BITCOIN: bitcoin})

    assert len(seen) == 1, "the provider must have run, and exactly one run row must exist"
    assert seen[0]["status"] == SyncRunStatus.RUNNING
    assert seen[0]["finished_at"] is None
    assert seen[0]["duration_ms"] is None
    assert seen[0]["wallets_total"] == 1
    assert seen[0]["id"] == summary.run_id
    assert bitcoin.calls == [(BIP173_TESTNET_P2WPKH,)]


async def test_counts_and_timing_are_written_when_the_run_ends(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 4's second half: the row is completed, not merely created.

    Every field the criterion names -- status, counts, timing -- is read off the row rather
    than off the summary, and the timestamps come from the injected clock so the assertion
    is an equality rather than a range.
    """
    await plant_wallets(
        sessions,
        bitcoin=(BIP173_TESTNET_P2WPKH, BIP350_TESTNET_V1),
        kaspa=(KASPA_TESTNET_V0,),
    )
    providers = {
        ChainKey.BITCOIN: StubChainProvider(
            ChainKey.BITCOIN,
            {BIP173_TESTNET_P2WPKH: BTC_UNITS, BIP350_TESTNET_V1: BTC_UNITS},
        ),
        ChainKey.KASPA: StubChainProvider(ChainKey.KASPA, raises=ProviderUnavailableError("down")),
    }

    summary = await run_sync(sessions, providers, trigger=SyncTrigger.SCHEDULED)

    runs = await sync_runs(sessions)
    assert len(runs) == 1
    row = runs[0]
    assert row["trigger"] == SyncTrigger.SCHEDULED
    assert row["status"] == SyncRunStatus.PARTIAL
    assert (row["wallets_total"], row["wallets_succeeded"], row["wallets_failed"]) == (3, 2, 1)
    assert row["duration_ms"] == EXPECTED_DURATION_MS
    assert summary.started_at == STARTED_AT
    assert summary.finished_at == FINISHED_AT
    assert summary.duration_ms == EXPECTED_DURATION_MS


async def test_duration_comes_from_the_monotonic_clock_not_the_wall_clock(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The one test that can fail. Step the wall clock **backwards** and watch the duration.

    A Raspberry Pi with no battery-backed clock boots at the epoch and jumps forward when
    NTP answers; the same mechanism can step it back. `finished_at - started_at` is then
    negative and a `duration_ms` computed from it is a number nobody can read. Two reads of
    a monotonic counter cannot do that, and that is the whole reason the spec measures the
    time twice.

    The wall clock's two readings are asserted as well, in the order they came out, so that
    the test is visibly about a clock that moved backwards rather than about an arbitrary
    pair of numbers.
    """
    await plant_wallets(sessions, kaspa=())
    stepped_back = iter((FINISHED_AT, STARTED_AT))
    last = [FINISHED_AT]

    def backwards() -> datetime:
        last[0] = next(stepped_back, last[0])
        return last[0]

    bitcoin = StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS})

    summary = await run_sync(sessions, {ChainKey.BITCOIN: bitcoin}, clock=backwards)

    assert summary.started_at == FINISHED_AT
    assert summary.finished_at == STARTED_AT
    assert summary.finished_at < summary.started_at, "the fixture has to move time backwards"
    assert summary.duration_ms == EXPECTED_DURATION_MS
    assert summary.duration_ms > 0
    assert (await sync_runs(sessions))[0]["duration_ms"] == EXPECTED_DURATION_MS


async def test_the_trigger_is_recorded_as_the_caller_gave_it(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Three triggers, three rows. `startup` is the one nothing else in the suite produces.

    The column exists so an operator can tell a run somebody asked for from one the
    schedule made, which is the first question asked when a vendor complains about traffic.
    """
    await plant_wallets(sessions, bitcoin=(), kaspa=())

    for trigger in (SyncTrigger.SCHEDULED, SyncTrigger.MANUAL, SyncTrigger.STARTUP):
        await run_sync(sessions, {}, trigger=trigger)

    assert [row["trigger"] for row in await sync_runs(sessions)] == [
        SyncTrigger.SCHEDULED,
        SyncTrigger.MANUAL,
        SyncTrigger.STARTUP,
    ]


async def test_a_run_over_no_wallets_is_a_success_with_no_chain_rows(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The empty page: a fresh deployment's first run, which must not be a crash.

    `success` rather than `failed`, because nothing failed -- there was nothing to ask. A
    `failed` here would send whoever installed it looking for a vendor outage on day one.
    """
    await plant_wallets(sessions, bitcoin=(), kaspa=())

    summary = await run_sync(sessions, {})

    assert summary.status == SyncRunStatus.SUCCESS
    assert summary.chains == ()
    assert (summary.wallets_total, summary.wallets_succeeded, summary.wallets_failed) == (0, 0, 0)
    assert len(await sync_runs(sessions)) == 1
    assert await sync_run_chains(sessions) == []


async def test_an_archived_wallet_is_neither_read_nor_counted(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Archiving is what stops a retired address costing a vendor a request forever.

    Counted as well as read: a `wallets_total` that included archived rows would make every
    run look partial, for wallets nobody asked about.
    """
    await plant_wallets(
        sessions,
        bitcoin=(BIP173_TESTNET_P2WPKH, BIP350_TESTNET_V1),
        kaspa=(),
        archived=(BIP350_TESTNET_V1,),
    )
    bitcoin = StubChainProvider(
        ChainKey.BITCOIN,
        {BIP173_TESTNET_P2WPKH: BTC_UNITS, BIP350_TESTNET_V1: BTC_UNITS},
    )

    summary = await run_sync(sessions, {ChainKey.BITCOIN: bitcoin})

    assert bitcoin.calls == [(BIP173_TESTNET_P2WPKH,)]
    assert summary.wallets_total == 1
    assert len(await snapshots(sessions)) == 1


# --------------------------------------------------------------------------------------
# What reaches the snapshot row
# --------------------------------------------------------------------------------------


async def test_a_snapshot_carries_the_run_the_decimals_and_the_observation_time(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`decimals` is stored rather than read from `assets` at query time, and this is why.

    A snapshot that resolved its exponent at read time would reinterpret history the moment
    somebody edited an asset row -- ten years of Bitcoin balances silently rescaled by a
    one-character change. The column is on the snapshot for that reason and the value comes
    from the provider's own declared capabilities.
    """
    planted = await plant_wallets(sessions, kaspa=())
    bitcoin = StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS})

    summary = await run_sync(sessions, {ChainKey.BITCOIN: bitcoin})

    row = (await snapshots(sessions))[0]
    assert row["wallet_id"] == planted.bitcoin[0]
    assert row["sync_run_id"] == summary.run_id
    assert row["confirmed"] == BTC_UNITS
    assert row["decimals"] == 8
    assert row["observed_at"] is not None


async def test_the_pending_tri_state_reaches_the_column_intact(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`NULL` is "this chain does not answer", and a negative value is a real mempool delta.

    The two are asserted together because collapsing them is one edit: a writer that
    defaulted `pending` to `0` would satisfy every other test in this module and would turn
    "we do not know" into "nothing is pending", which is the distinction #7 added the field
    to keep. The negative value is the reason the column has no non-negative CHECK where
    `confirmed` does.
    """
    planted = await plant_wallets(
        sessions,
        bitcoin=(BIP173_TESTNET_P2WPKH,),
        kaspa=(KASPA_TESTNET_V0,),
    )
    providers = {
        # No `pending` mapping at all: the short spelling of "this chain cannot answer".
        ChainKey.BITCOIN: StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS}),
        ChainKey.KASPA: StubChainProvider(
            ChainKey.KASPA,
            {KASPA_TESTNET_V0: KAS_UNITS},
            pending={KASPA_TESTNET_V0: -500},
        ),
    }

    await run_sync(sessions, providers)

    by_wallet = {row["wallet_id"]: row for row in await snapshots(sessions)}
    assert by_wallet[planted.bitcoin[0]]["pending"] is None
    assert by_wallet[planted.kaspa[0]]["pending"] == -500


async def test_a_second_run_appends_rather_than_replacing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Snapshots are a history, which is criterion 8's whole premise.

    A writer that upserted onto `wallet_id` would satisfy the current-balance endpoint
    perfectly and leave nothing to chart. `UNIQUE (wallet_id, sync_run_id)` is what makes
    the two runs two rows, and it is asserted as two rows rather than as a constraint.
    """
    planted = await plant_wallets(sessions, kaspa=())
    first = StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS})
    second = StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS + 1})

    one = await run_sync(sessions, {ChainKey.BITCOIN: first})
    two = await run_sync(sessions, {ChainKey.BITCOIN: second})

    rows = await snapshots(sessions)
    assert [row["confirmed"] for row in rows] == [BTC_UNITS, BTC_UNITS + 1]
    assert [row["sync_run_id"] for row in rows] == [one.run_id, two.run_id]
    assert {row["wallet_id"] for row in rows} == set(planted.bitcoin)


async def test_no_message_on_a_chain_row_contains_an_address(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Rule 3 at the one new column that holds prose an operator reads.

    `detail` is the provider's own message, and `providers/errors.py` promises no exception
    there ever carries an address. This is where that promise is cashed into a row that
    ends up in a response body and in an operations view -- and the failure would be a
    provider that started quoting the address it could not read, which is the most natural
    thing in the world to write.
    """
    await plant_wallets(sessions)
    providers = {
        ChainKey.BITCOIN: StubChainProvider(
            ChainKey.BITCOIN,
            raises=ProviderResponseError("the response named an address nobody asked about"),
        ),
        ChainKey.KASPA: StubChainProvider(ChainKey.KASPA, raises=TypeError("bad parse")),
    }

    await run_sync(sessions, providers)

    details = " ".join(str(row["detail"]) for row in await sync_run_chains(sessions))
    assert BIP173_TESTNET_P2WPKH not in details
    assert KASPA_TESTNET_V0 not in details


async def test_an_internal_failure_records_the_exception_type_and_never_its_message(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The `internal` clause records `type(error).__name__` and not `str(error)`, on purpose.

    This is the one place where the two catch clauses have to differ in more than the kind
    they write. A `ProviderError`'s message is safe to record: `providers/errors.py` is
    written so that none of them ever quotes an address, a URL or a response body, and the
    message is the most useful thing an operator can be given. **An arbitrary exception has
    no such guarantee.** The realistic one is a `KeyError` raised while correlating a
    balance to the address it belongs to, whose `str()` *is* the address -- and `detail` is
    a column served by `GET /api/balances/runs`, so recording it would publish the owner's
    holdings through an endpoint built to report an outage.

    Driven with exactly that exception, carrying exactly that address. The Kaspa half is the
    control: a provider failure in the same run keeps its whole message, so this is a
    statement about the distinction rather than about `detail` being empty.
    """
    await plant_wallets(sessions)
    providers = {
        ChainKey.BITCOIN: StubChainProvider(
            ChainKey.BITCOIN, raises=KeyError(BIP173_TESTNET_P2WPKH)
        ),
        ChainKey.KASPA: StubChainProvider(
            ChainKey.KASPA, raises=ProviderUnavailableError("no configured endpoint answered")
        ),
    }

    await run_sync(sessions, providers)

    rows = {row["chain_key"]: row for row in await sync_run_chains(sessions)}
    assert rows["bitcoin"]["error_kind"] == SyncErrorKind.INTERNAL
    assert rows["bitcoin"]["detail"] == "KeyError"
    assert BIP173_TESTNET_P2WPKH not in str(rows["bitcoin"]["detail"])
    assert rows["kaspa"]["detail"] == "no configured endpoint answered", (
        "a provider's own message is safe to record and is the useful half"
    )


async def test_the_address_really_is_in_the_exception_this_guards_against(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The falsification control: without it the test above proves nothing.

    If `str(KeyError(address))` did not contain the address, "the detail does not contain
    the address" would be true of any implementation at all, including one that recorded
    `str(error)` verbatim. It does contain it -- in quotes, which is why the comparison is
    a substring one -- so the guard has something to guard against.
    """
    leaking = KeyError(BIP173_TESTNET_P2WPKH)

    assert BIP173_TESTNET_P2WPKH in str(leaking)
    assert BIP173_TESTNET_P2WPKH not in type(leaking).__name__


# --------------------------------------------------------------------------------------
# The third catch clause: the owner's mistake is neither the vendor's nor ours
# --------------------------------------------------------------------------------------


async def test_a_rejected_address_is_its_own_kind_and_not_internal(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A wrong-network wallet is a configuration mistake, and `internal` is the wrong file for it.

    The case is real rather than contrived: the wallet registry validates an address against
    the chain's codec and **not** against the configured network, so a mainnet address under
    `PORTFOLIO_BITCOIN_NETWORK=testnet` is accepted at registration and refused by the
    provider on every tick afterwards. Filed as `internal` that is a traceback every fifteen
    minutes, forever, for a defect that does not exist -- which is the failure the `internal`
    kind exists to prevent, pointed the other way.

    Asserted against `internal` explicitly as well as for its own member, because the two
    catch clauses being three is the whole content of this change.
    """
    planted = await plant_wallets(sessions)
    bitcoin = StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS})
    kaspa = StubChainProvider(
        ChainKey.KASPA,
        raises=AddressInvalidError(AddressRejection.WRONG_NETWORK),
    )

    summary = await run_sync(sessions, {ChainKey.BITCOIN: bitcoin, ChainKey.KASPA: kaspa})

    rows = {row["chain_key"]: row for row in await sync_run_chains(sessions)}
    assert rows["kaspa"]["error_kind"] == SyncErrorKind.ADDRESS_REJECTED
    assert rows["kaspa"]["error_kind"] != SyncErrorKind.INTERNAL
    assert rows["kaspa"]["error_kind"] != SyncErrorKind.RESPONSE, (
        "the vendor answered nothing; there is no response to blame"
    )
    # Isolated like every other failure: the owner's mistake on one chain costs that chain.
    assert {row["wallet_id"] for row in await snapshots(sessions)} == set(planted.bitcoin)
    assert summary.status == SyncRunStatus.PARTIAL


async def test_a_rejected_address_never_reaches_the_detail_column(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`detail` is served by an endpoint, so the reason may travel and the address may not.

    `AddressRejection` is a closed set of fixed words and each member's message is a fixed
    sentence that interpolates nothing, which is what makes the reason safe to record. The
    address is in scope in the frame that raised and must stay there: it is the owner's
    holdings, and this column ends up in a response body and in an operations view.
    """
    await plant_wallets(sessions, bitcoin=(), kaspa=(KASPA_TESTNET_V0,))
    kaspa = StubChainProvider(
        ChainKey.KASPA,
        raises=AddressInvalidError(AddressRejection.WRONG_NETWORK),
    )

    await run_sync(sessions, {ChainKey.KASPA: kaspa})

    detail = str((await sync_run_chains(sessions))[0]["detail"])
    assert KASPA_TESTNET_V0 not in detail
    assert detail != ""
    assert AddressRejection.WRONG_NETWORK.value in detail or "network" in detail.lower(), (
        "the reason has to survive, or an operator has nothing to act on"
    )


async def test_the_three_clauses_are_three(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """One run, three failures, three different kinds. The control for all of the above.

    Each of the three tests that names one kind passes for an implementation that records
    *everything* as that kind. Only driving all three through one service and asserting the
    rows differ pins the distinction rather than one side of it. A third chain is not
    available, so the vendor and the rejection are driven together here and the internal
    clause is proven distinct from both by the pair of assertions at the end.
    """
    await plant_wallets(sessions)
    vendor_down = {
        ChainKey.BITCOIN: StubChainProvider(
            ChainKey.BITCOIN, raises=ProviderUnavailableError("their outage")
        ),
        ChainKey.KASPA: StubChainProvider(
            ChainKey.KASPA, raises=AddressInvalidError(AddressRejection.WRONG_NETWORK)
        ),
    }
    our_bug = {
        ChainKey.BITCOIN: StubChainProvider(ChainKey.BITCOIN, raises=ValueError("our own bug")),
        ChainKey.KASPA: StubChainProvider(
            ChainKey.KASPA, raises=AddressInvalidError(AddressRejection.WRONG_NETWORK)
        ),
    }

    await run_sync(sessions, vendor_down)
    await run_sync(sessions, our_bug)

    kinds = {
        (row["sync_run_id"], row["chain_key"]): row["error_kind"]
        for row in await sync_run_chains(sessions)
    }
    recorded = sorted({str(kind) for kind in kinds.values()})
    assert recorded == sorted(
        {
            SyncErrorKind.UNAVAILABLE.value,
            SyncErrorKind.ADDRESS_REJECTED.value,
            SyncErrorKind.INTERNAL.value,
        }
    )


# --------------------------------------------------------------------------------------
# The two provider failures the vocabulary does not name
# --------------------------------------------------------------------------------------


class UnmappedVendorError(ProviderError):
    """A `ProviderError` subclass added after the mapping was written, as one will be."""


@pytest.mark.parametrize(
    "failure",
    [
        UnmappedVendorError("a new failure a provider learned to raise"),
        DuplicateProviderError("kaspa"),
    ],
    ids=["a subclass added later", "duplicate registration"],
)
async def test_a_provider_error_nobody_mapped_is_recorded_as_internal(
    sessions: async_sessionmaker[AsyncSession],
    failure: ProviderError,
) -> None:
    """The fall-through in the mapping points at us, and it has to point somewhere.

    The `CHECK` on `error_kind` admits a closed set, so a provider error with no entry in
    the mapping still has to become one of them. It becomes `internal`, because the missing
    entry is our omission -- a subclass somebody added to `providers/errors.py` without
    teaching the sync what it means. Filing it under a vendor kind would be a guess, and a
    guess is how "Kaspa is unavailable" comes to describe a mapping bug.

    `DuplicateProviderError` is the real, shipping instance of the case: a `ProviderError`
    the mapping deliberately omits, because it is raised at import and never by a read.
    """
    await plant_wallets(sessions, bitcoin=(), kaspa=(KASPA_TESTNET_V0,))
    kaspa = StubChainProvider(ChainKey.KASPA, raises=failure)

    await run_sync(sessions, {ChainKey.KASPA: kaspa})

    rows = await sync_run_chains(sessions)
    assert [row["error_kind"] for row in rows] == [SyncErrorKind.INTERNAL]
    assert kaspa.calls != [], "the provider was really reached, so this is its error"


class ShortChangingProvider(StubChainProvider):
    """A provider that breaks its contract: it answers about fewer addresses than it was asked.

    It bypasses `align_balances`, which is the only way to produce this -- every real
    provider builds its answer through that function precisely so it cannot. What is under
    test is the sync's own refusal to trust a result it cannot match to its request, which
    is the second line of defence and the one a future provider written carelessly would
    meet first.
    """

    async def fetch_balances(self, addresses: Sequence[str]) -> Sequence[AddressBalance]:
        answered = await super().fetch_balances(addresses)
        return tuple(answered)[:-1]


async def test_a_provider_that_answers_about_fewer_addresses_fails_its_chain_as_a_response(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """An answer that cannot be matched to the request writes nothing for that chain.

    Not a partial write of the addresses that *did* come back: a response that dropped one
    address is a response whose correlation cannot be trusted for the others either -- the
    argument `align_balances` makes about an unrequested address, one layer up. So the
    whole chain fails, as `response`, because the vendor answered and the answer was
    unusable.

    The detail says how many were missing and never which: the addresses are the owner's
    holdings and the column is served by an endpoint. Bitcoin in the same run is the
    isolation half, as everywhere else in this module.
    """
    planted = await plant_wallets(
        sessions,
        bitcoin=(BIP173_TESTNET_P2WPKH,),
        kaspa=(KASPA_TESTNET_V0, KASPA_TESTNET_V1_KEY),
    )
    providers = {
        ChainKey.BITCOIN: StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS}),
        ChainKey.KASPA: ShortChangingProvider(
            ChainKey.KASPA, {KASPA_TESTNET_V0: KAS_UNITS, KASPA_TESTNET_V1_KEY: KAS_UNITS}
        ),
    }

    summary = await run_sync(sessions, providers)

    rows = {row["chain_key"]: row for row in await sync_run_chains(sessions)}
    assert rows["kaspa"]["status"] == "failed"
    assert rows["kaspa"]["error_kind"] == SyncErrorKind.RESPONSE
    detail = str(rows["kaspa"]["detail"])
    assert "1" in detail, "the detail says how many were missing"
    assert KASPA_TESTNET_V0 not in detail
    assert KASPA_TESTNET_V1_KEY not in detail
    assert {row["wallet_id"] for row in await snapshots(sessions)} == set(planted.bitcoin), (
        "neither Kaspa wallet is written, not even the one that did come back"
    )
    assert summary.status == SyncRunStatus.PARTIAL


async def test_two_wallets_on_one_address_are_one_request_and_two_snapshots(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Two accounts may watch one address; the vendor is asked about it once.

    Not a contrived case: a household where both people register the same savings address is
    the ordinary one, and uniqueness is per account for that reason. Without the
    de-duplication the provider is handed the address twice, refuses the duplicate -- every
    provider does, through `align_balances` -- and the whole chain is recorded as `internal`
    on every tick, for both accounts, forever.

    Two snapshots rather than one, because the reading belongs to each wallet: the fan-out
    after the request is the half a de-duplication that dropped a wallet would get wrong.
    """
    async with sessions() as session:
        first_owner = await insert_user(session, "first-owner")
        second_owner = await insert_user(session, "second-owner")
        mine = await insert_wallet(
            session, user_id=first_owner, chain_key=ChainKey.BITCOIN, address=BIP173_TESTNET_P2WPKH
        )
        theirs = await insert_wallet(
            session, user_id=second_owner, chain_key=ChainKey.BITCOIN, address=BIP173_TESTNET_P2WPKH
        )
    bitcoin = StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS})

    summary = await run_sync(sessions, {ChainKey.BITCOIN: bitcoin})

    assert bitcoin.calls == [(BIP173_TESTNET_P2WPKH,)], "one address, asked about once"
    stored = {(row["wallet_id"], row["confirmed"]) for row in await snapshots(sessions)}
    assert stored == {(mine, BTC_UNITS), (theirs, BTC_UNITS)}
    assert summary.status == SyncRunStatus.SUCCESS
    assert (summary.wallets_total, summary.wallets_succeeded) == (2, 2)


async def test_a_running_row_left_behind_is_swept_when_the_next_run_opens(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """No restart needed: the next run clears what a failed close-out left at `running`.

    The lifespan sweeps at startup and at shutdown, and review found the gap between them:
    a run whose close-out failed -- the database locked at the wrong moment -- stays
    `running` until the process restarts, which on a healthy Pi is weeks. For all that time
    the table says a sync is in progress that is not. The sync now sweeps before it opens
    its own row, so the new row is never swept by itself.
    """
    await plant_wallets(sessions, kaspa=())
    async with sessions() as session:
        stale = await SyncRunRepository(session).open_run(
            trigger=SyncTrigger.SCHEDULED,
            started_at=STARTED_AT - timedelta(hours=1),
            wallets_total=1,
        )
        await session.commit()
        stale_id = stale.id
    bitcoin = StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS})

    with capture_logs() as captured:
        summary = await run_sync(sessions, {ChainKey.BITCOIN: bitcoin})

    statuses = {row["id"]: row["status"] for row in await sync_runs(sessions)}
    assert statuses == {stale_id: "interrupted", summary.run_id: "success"}
    swept = [
        entry for entry in captured if entry["event"] == "balance_sync_runs_marked_interrupted"
    ]
    assert [entry["runs"] for entry in swept] == [1]
