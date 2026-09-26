"""#15's write side: criteria 1 to 5, and the loop that pages a venue into `exchange_fills`.

Every test drives the real `ExchangeSyncService` over a real migrated SQLite file, against
`SimulatedVenue` (`tests/exchange_sync_harness.py`): a venue that pages the fills it holds by
window and by trade id, and fails when told to. What is asserted is read back over a
**second session**, so it is what was committed -- the only state a restart ever sees.

## The window most tests use

The clock stands at `T0`, 2026-09-25 12:00 UTC, and the history start is that date, so the
first sync plans one window, `[00:00, 12:00)`. Nine fills a minute apart end at 11:59, and
the venue's page is three fills, so the window is three pages whose cursors are the trade
ids `1007` and `1004`:

| Page | Cursor sent | Fills | Next cursor |
|---|---|---|---|
| 1 | `None` | 1009, 1008, 1007 | `1007` |
| 2 | `1007` | 1006, 1005, 1004 | `1004` |
| 3 | `1004` | 1003, 1002, 1001 | `None` |

"Resumes with page 2's `next_cursor`" is then a statement about one string, `1004`, in the
venue's call log.

## A crash is tested two ways, and a third

An `Exception` out of page 3 is a failure the run records. A `BaseException` that is not an
`Exception` is the process dying: nothing is recorded, the run row stays `running`, and a
fresh service over the same file -- a new engine, as a restarted container has -- resumes.
And a crash at **every** commit of a run, one at a time, proves the fills and the cursor are
never committed apart: after each, the fills stored are exactly the pages the checkpoint
says were read.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import event, text
from structlog.testing import capture_logs

from portfolio.domain.exchanges import ExchangeKey
from portfolio.providers.exchanges.base import CursorKind, FillWindow
from portfolio.providers.exchanges.errors import (
    ExchangeAuthError,
    ExchangeInsufficientScopeError,
    ExchangeInvalidRequestError,
    ExchangeRateLimitedError,
    ExchangeRetentionWindowError,
    ExchangeSchemaError,
    ExchangeUnavailableError,
)
from portfolio.repositories.exchange_sync_runs import (
    AccountOutcomeStatus,
    ExchangeSyncErrorKind,
    SyncRunStatus,
    SyncTrigger,
)
from portfolio.repositories.exchanges import ExchangeAccountRepository, ExchangeFillRepository
from portfolio.services.exchange_sync import build_exchange_sync_service
from portfolio.services.exchange_sync_plan import HISTORY_GENESIS
from tests.balance_harness import insert_user
from tests.exchange_sync_harness import (
    ACCOUNTS_SQL,
    FILLS_SQL,
    OUTCOMES_SQL,
    RUNS_SQL,
    T0,
    WINDOWS_SQL,
    PageCall,
    RecordingSleeper,
    SettableClock,
    SimulatedPowerLoss,
    SimulatedVenue,
    TickingMonotonic,
    always,
    changed,
    faults_on,
    fills_between,
    make_fill,
    rows,
    sqlite_timestamp,
    trade_ids,
)
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.repositories.exchange_sync_runs import AccountOutcome, ExchangeSyncRunSummary
    from tests.exchange_sync_harness import Fault

TODAY: Final = date(2026, 9, 25)
MIDNIGHT: Final = datetime(2026, 9, 25, tzinfo=UTC)
#: The one window a first sync plans under `TODAY` at `T0`.
WINDOW: Final = FillWindow(since=MIDNIGHT, until=T0)
ALL_NINE: Final = [str(trade_id) for trade_id in range(1001, 1010)]
PAGE_ONE: Final = {"1009", "1008", "1007"}
PAGE_TWO: Final = {"1006", "1005", "1004"}


def nine_fills() -> list[Any]:
    return fills_between(9)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        async with built() as session:
            await insert_user(session)
        yield built


class Harness:
    """The service's collaborators, kept across runs so a test can move the clock."""

    def __init__(self, *, history_start: date | None = TODAY) -> None:
        self.clock = SettableClock()
        self.sleeper = RecordingSleeper()
        self.monotonic = TickingMonotonic()
        self.history_start = history_start

    async def run(
        self,
        factory: async_sessionmaker[AsyncSession],
        venues: Mapping[ExchangeKey, SimulatedVenue] | SimulatedVenue,
        trigger: SyncTrigger = SyncTrigger.SCHEDULED,
        *,
        on_session: Callable[[AsyncSession], None] | None = None,
    ) -> ExchangeSyncRunSummary:
        providers = (
            {venues.capabilities.exchange_key: venues}
            if isinstance(venues, SimulatedVenue)
            else dict(venues)
        )
        async with factory() as session:
            if on_session is not None:
                on_session(session)
            service = build_exchange_sync_service(
                session,
                providers=providers,
                clock=self.clock,
                monotonic=self.monotonic,
                sleep=self.sleeper,
                history_start=self.history_start,
            )
            return await service.sync(trigger)


def only(summary: ExchangeSyncRunSummary) -> AccountOutcome:
    (outcome,) = summary.accounts
    return outcome


async def account_row(factory: async_sessionmaker[AsyncSession], key: str = "bitget") -> Any:
    (row,) = [row for row in await rows(factory, ACCOUNTS_SQL) if row["exchange_key"] == key]
    return row


async def window_rows(factory: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    return await rows(factory, WINDOWS_SQL)


def assert_each_once(stored: list[str], expected: list[str]) -> None:
    assert len(stored) == len(set(stored)), "a fill is stored twice"
    assert sorted(stored) == sorted(expected), "a fill is missing or unexpected"


# --------------------------------------------------------------------------------------
# The ordinary run
# --------------------------------------------------------------------------------------


async def test_a_first_sync_reads_the_whole_window_and_records_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    venue = SimulatedVenue(nine_fills())

    summary = await harness.run(factory, venue)

    assert venue.calls == [
        PageCall(WINDOW, None, None),
        PageCall(WINDOW, "1007", None),
        PageCall(WINDOW, "1004", None),
    ]
    assert_each_once(await trade_ids(factory), ALL_NINE)
    assert summary.status is SyncRunStatus.SUCCESS
    assert summary.trigger is SyncTrigger.SCHEDULED
    outcome = only(summary)
    assert outcome.status is AccountOutcomeStatus.SUCCESS
    assert (outcome.windows_completed, outcome.pages) == (1, 3)
    assert (outcome.fills_seen, outcome.fills_inserted) == (9, 9)
    assert (summary.fills_seen, summary.fills_inserted) == (9, 9)
    assert (summary.accounts_total, summary.accounts_succeeded, summary.accounts_failed) == (
        1,
        1,
        0,
    )
    account = await account_row(factory)
    assert account["sync_status"] == "ok"
    assert account["last_synced_at"] == sqlite_timestamp(T0)
    assert account["planned_until"] == sqlite_timestamp(T0)
    assert await window_rows(factory) == [], "the queue holds only unfinished work"
    (run,) = await rows(factory, RUNS_SQL)
    assert run["status"] == "success"
    assert run["duration_ms"] is not None
    assert run["duration_ms"] > 0


async def test_the_run_row_is_committed_before_any_venue_is_called(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Evidence of a run a crash interrupts has to be on disk before the crash can happen."""
    harness = Harness()
    venue = SimulatedVenue(nine_fills())
    seen: list[list[dict[str, Any]]] = []

    async def look(call: PageCall) -> None:
        del call
        if not seen:
            seen.append(await rows(factory, RUNS_SQL))
            seen.append(await rows(factory, ACCOUNTS_SQL))
            seen.append(await window_rows(factory))

    venue.on_call = look
    await harness.run(factory, venue)

    runs, accounts, windows = seen
    assert [run["status"] for run in runs] == ["running"]
    (account,) = accounts
    assert account["planned_until"] == sqlite_timestamp(T0), "the plan was committed first"
    assert account["effective_since"] == sqlite_timestamp(MIDNIGHT)
    assert [(row["since"], row["until"]) for row in windows] == [
        (sqlite_timestamp(MIDNIGHT), sqlite_timestamp(T0))
    ]


# --------------------------------------------------------------------------------------
# Criterion 1: a crash mid-window resumes with no duplicates and no gaps
# --------------------------------------------------------------------------------------


async def test_a_failure_on_the_third_page_resumes_at_the_committed_cursor(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An `Exception` out of page 3: the run records it, the checkpoint keeps page 2's cursor."""
    harness = Harness()
    failing = SimulatedVenue(
        nine_fills(), fault=faults_on({3: ExchangeUnavailableError(status=503)})
    )

    first = await harness.run(factory, failing)

    outcome = only(first)
    assert outcome.status is AccountOutcomeStatus.FAILED
    assert outcome.error_kind is ExchangeSyncErrorKind.UNAVAILABLE
    assert (outcome.windows_completed, outcome.pages) == (0, 2)
    assert (outcome.fills_seen, outcome.fills_inserted) == (6, 6)
    assert set(await trade_ids(factory)) == PAGE_ONE | PAGE_TWO
    assert [row["cursor"] for row in await window_rows(factory)] == ["1004"]
    assert (await account_row(factory))["sync_status"] == "error"

    healthy = SimulatedVenue(nine_fills())
    second = await harness.run(factory, healthy)

    assert healthy.calls == [PageCall(WINDOW, "1004", None)], (
        "the resumed run must ask for page 3 with page 2's cursor, not start the window again"
    )
    assert_each_once(await trade_ids(factory), ALL_NINE)
    assert only(second).status is AccountOutcomeStatus.SUCCESS
    assert (only(second).fills_seen, only(second).fills_inserted) == (3, 3)
    assert await window_rows(factory) == []
    assert (await account_row(factory))["sync_status"] == "ok"


async def test_a_crash_on_the_third_page_resumes_with_no_duplicates_and_no_gaps(
    tmp_path: Path,
) -> None:
    """The process dies on page 3; a fresh process over the same file finishes the window.

    `SimulatedPowerLoss` is a `BaseException`, so nothing in the application may catch it:
    no outcome is written, the run row stays `running`, and what survives is exactly what
    was committed. The second process is a new engine over the same file -- the state a
    restarted container has -- and its venue's call log is the proof: page 3 asked for with
    page 2's cursor, and nothing asked twice.
    """
    harness = Harness()
    async with migrated_sessionmaker(tmp_path) as first_process:
        async with first_process() as session:
            await insert_user(session)
        dying = SimulatedVenue(nine_fills(), fault=faults_on({3: SimulatedPowerLoss()}))
        with pytest.raises(SimulatedPowerLoss):
            await harness.run(first_process, dying)
        assert dying.cursors() == [None, "1007", "1004"]

    async with migrated_sessionmaker(tmp_path) as second_process:
        assert [run["status"] for run in await rows(second_process, RUNS_SQL)] == ["running"]
        assert await rows(second_process, OUTCOMES_SQL) == [], "a dead process records nothing"
        assert set(await trade_ids(second_process)) == PAGE_ONE | PAGE_TWO
        assert [row["cursor"] for row in await window_rows(second_process)] == ["1004"]

        restarted = SimulatedVenue(nine_fills())
        summary = await harness.run(second_process, restarted)

        assert restarted.calls == [PageCall(WINDOW, "1004", None)]
        assert_each_once(await trade_ids(second_process), ALL_NINE)
        assert summary.status is SyncRunStatus.SUCCESS
        assert [run["status"] for run in await rows(second_process, RUNS_SQL)] == [
            "interrupted",
            "success",
        ], "the dead run is swept before the new one opens"
        interrupted = (await rows(second_process, RUNS_SQL))[0]
        assert interrupted["finished_at"] is None
        assert interrupted["duration_ms"] is None


async def test_a_commit_that_fails_leaves_neither_the_fills_nor_the_cursor(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Page 2's commit fails: its fills and its cursor are refused together.

    The commit is failed from a `before_commit` hook armed by the venue having served two
    pages, so the commit that fails is the one carrying page 2. What remains is page 1 and
    page 1's cursor -- neither the fills without the cursor (a checkpoint behind the data,
    which only costs a re-read) nor the cursor without the fills (a gap, forever).
    """
    harness = Harness()
    venue = SimulatedVenue(nine_fills())
    armed = [True]

    def fail_page_two(_session: object) -> None:
        if armed[0] and len(venue.calls) == 2:
            armed[0] = False
            message = "disk I/O error"
            raise OSError(message)

    def install(session: AsyncSession) -> None:
        event.listen(session.sync_session, "before_commit", fail_page_two)

    summary = await harness.run(factory, venue, on_session=install)

    assert armed == [False], "the hook never fired, so this proves nothing"
    assert set(await trade_ids(factory)) == PAGE_ONE
    assert [row["cursor"] for row in await window_rows(factory)] == ["1007"]
    outcome = only(summary)
    assert outcome.status is AccountOutcomeStatus.FAILED
    assert outcome.error_kind is ExchangeSyncErrorKind.INTERNAL
    assert outcome.detail == "OSError"


def dying_at_commit(target: int) -> Callable[[AsyncSession], None]:
    """An installer for a hook that kills the process at the `target`-th commit, counting from 1."""
    seen = [0]

    def hook(_session: object) -> None:
        seen[0] += 1
        if seen[0] == target:
            raise SimulatedPowerLoss

    def install(session: AsyncSession) -> None:
        event.listen(session.sync_session, "before_commit", hook)

    return install


def pages_the_checkpoint_says_were_read(windows: list[dict[str, Any]], planned: bool) -> set[str]:
    """The fills the stored checkpoint promises are on disk, for the nine-fill window."""
    if not windows:
        return set(ALL_NINE) if planned else set()
    (window,) = windows
    return {None: set(), "1007": PAGE_ONE, "1004": PAGE_ONE | PAGE_TWO}[window["cursor"]]


#: The oldest window of a backfill with no history start, under the simulated venue's
#: ninety-day retention and seven-day limit: `[T0 - 90 d + 5 min, T0 - 84 d)`.
OLDEST_UNTIL: Final = T0 - timedelta(days=84)


def oldest_nine() -> list[Any]:
    """The nine fills, placed in the oldest backfill window instead of the newest."""
    return fills_between(9, newest=T0 - timedelta(days=85))


@pytest.mark.parametrize(
    ("kind", "resumed_cursors"),
    [
        (CursorKind.TRADE_ID_BEFORE, ["1004"]),
        (CursorKind.TRADE_ID_AFTER, ["1004"]),
        (CursorKind.TIME, [None, "1007", "1004"]),
    ],
    ids=["trade id before", "trade id after", "time"],
)
async def test_an_interrupted_oldest_window_resumes_at_its_cursor_after_the_floor_moves(
    factory: async_sessionmaker[AsyncSession],
    kind: CursorKind,
    resumed_cursors: list[str | None],
) -> None:
    """Criterion 1 for the window the rolling retention floor passes on every run (F3).

    The backfill reads the twelve newer windows, then pages 1 and 2 of the oldest, and page
    3 fails. Fifteen minutes later the retention floor has moved fifteen minutes, past the
    oldest window's `since`, so the window's start moves up -- and with a trade-id cursor,
    which bounds ids rather than instants, it must still resume at page 2's cursor rather
    than restart. Otherwise an interrupted backfill re-reads its oldest window from the
    start after every interruption, forever. A time cursor is a position in the range that
    moved, so that kind restarts -- and the constraint makes the re-read free.
    """
    harness = Harness(history_start=None)

    def fail_third_oldest_page(
        call_number: int, window: FillWindow, cursor: str | None
    ) -> BaseException | None:
        del call_number
        oldest = window.until == OLDEST_UNTIL
        return ExchangeUnavailableError(status=503) if oldest and cursor == "1004" else None

    await harness.run(
        factory,
        SimulatedVenue(oldest_nine(), cursor_kind=kind, fault=fail_third_oldest_page),
    )
    assert [row["cursor"] for row in await window_rows(factory)] == ["1004"]
    harness.clock.advance(timedelta(minutes=15))
    venue = SimulatedVenue(oldest_nine(), cursor_kind=kind)

    summary = await harness.run(factory, venue)

    oldest_calls = [call for call in venue.calls if call.window.until == OLDEST_UNTIL]
    assert [call.cursor for call in oldest_calls] == resumed_cursors, (
        "the oldest window did not resume the way its cursor kind allows"
    )
    new_floor = T0 + timedelta(minutes=15) - timedelta(days=90) + timedelta(minutes=5)
    assert oldest_calls[0].window.since == new_floor
    assert summary.status is SyncRunStatus.SUCCESS
    assert_each_once(await trade_ids(factory), ALL_NINE)


async def test_a_retention_step_keeps_the_cursor_of_a_trade_id_window(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A step moves only `since`; a trade-id cursor still bounds the same pages.

    The oldest window is interrupted after page 2. In the next run the venue turns out to
    keep a little less than it declared and refuses the window; the step moves its start a
    day and the very next request carries page 2's cursor.
    """
    harness = Harness(history_start=None)

    def fail_third_oldest_page(
        call_number: int, window: FillWindow, cursor: str | None
    ) -> BaseException | None:
        del call_number
        oldest = window.until == OLDEST_UNTIL
        return ExchangeUnavailableError(status=503) if oldest and cursor == "1004" else None

    await harness.run(factory, SimulatedVenue(oldest_nine(), fault=fail_third_oldest_page))
    venue = SimulatedVenue(
        oldest_nine(), fault=refuse_older_than(T0 - timedelta(days=89, hours=12))
    )

    summary = await harness.run(factory, venue)

    oldest_calls = [call for call in venue.calls if call.window.until == OLDEST_UNTIL]
    assert [call.cursor for call in oldest_calls] == ["1004", "1004"]
    oldest_since = T0 - timedelta(days=90) + timedelta(minutes=5)
    assert [call.window.since for call in oldest_calls] == [
        oldest_since,
        oldest_since + timedelta(days=1),
    ]
    assert summary.status is SyncRunStatus.SUCCESS
    assert_each_once(await trade_ids(factory), ALL_NINE)


async def test_a_clock_that_ran_ahead_leaves_no_range_claimed_and_never_read(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """F1: a ceiling left in the future by a fast clock is pulled back, and re-covered.

    Run 1 happens with the clock a day ahead; the venue, which signs its requests against
    real time, refuses it. The clock is corrected and run 2 finds its plan ahead of `now`:
    it logs that and pulls `planned_until` back to `now`. A fill is then executed between
    run 2 and run 3, and run 3 reads it. Kept at the day-ahead ceiling, no top window would
    be planned until real time caught up, and the fill would sit forever in a range the
    account claims to have read.
    """
    harness = Harness()
    harness.clock.moment = T0 + timedelta(days=1)
    await harness.run(factory, SimulatedVenue(fault=always(ExchangeUnavailableError(status=503))))
    assert (await account_row(factory))["planned_until"] == sqlite_timestamp(T0 + timedelta(days=1))
    harness.clock.moment = T0 + timedelta(hours=1)

    with capture_logs() as captured:
        await harness.run(factory, SimulatedVenue(nine_fills()))

    assert (await account_row(factory))["planned_until"] == sqlite_timestamp(
        T0 + timedelta(hours=1)
    )
    behind = [entry for entry in captured if entry["event"] == "exchange_sync_clock_behind_plan"]
    assert [entry["log_level"] for entry in behind] == ["warning"]
    late = make_fill(2000, T0 + timedelta(hours=2))
    harness.clock.moment = T0 + timedelta(hours=3)

    await harness.run(factory, SimulatedVenue([*nine_fills(), late]))

    assert "2000" in await trade_ids(factory), "the fill after the corrected clock was never read"
    account = await account_row(factory)
    assert account["planned_until"] == sqlite_timestamp(T0 + timedelta(hours=3))
    assert account["sync_status"] == "ok"


async def test_an_edge_that_aged_during_the_run_is_re_clamped_without_spending_a_step(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """F2: the oldest window is read last, after the edge it was planned at has aged.

    The venue keeps exactly ninety days as of each request. Ten minutes pass while the newest
    window is read, so by the time the oldest window is asked for, its `since` is older than
    the venue keeps. That is elapsed time, not a short retention: the window moves to the
    edge as it stands now -- ten minutes on, not a whole day -- and no step is spent.
    """
    harness = Harness(history_start=None)

    def refuse_past_live_retention(
        call_number: int, window: FillWindow, cursor: str | None
    ) -> BaseException | None:
        del call_number, cursor
        if window.since < harness.clock.moment - timedelta(days=90):
            return ExchangeRetentionWindowError(status=400, venue_code="40704")
        return None

    venue = SimulatedVenue(nine_fills(), fault=refuse_past_live_retention)
    aged: list[bool] = []

    async def age_the_edge(call: PageCall) -> None:
        if call.window.until == T0 and not aged:
            aged.append(True)
            harness.clock.advance(timedelta(minutes=10))

    venue.on_call = age_the_edge

    with capture_logs() as captured:
        summary = await harness.run(factory, venue)

    planned = T0 - timedelta(days=90) + timedelta(minutes=5)
    fresh = T0 + timedelta(minutes=10) - timedelta(days=90) + timedelta(minutes=5)
    oldest_calls = [call for call in venue.calls if call.window.until == OLDEST_UNTIL]
    assert [call.window.since for call in oldest_calls] == [planned, fresh]
    events = [entry["event"] for entry in captured]
    assert events.count("exchange_sync_retention_reclamped") == 1
    assert events.count("exchange_sync_retention_step") == 0
    assert summary.status is SyncRunStatus.SUCCESS
    assert (await account_row(factory))["effective_since"] == sqlite_timestamp(fresh)


async def test_a_step_past_a_short_retention_never_raises_the_floor_past_the_ceiling(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """F2: a step that empties the refused window raises the floor only to that window's end.

    A venue that keeps a day, read in six-hour windows, and in fact keeps only twenty hours:
    the three newer windows are read, the oldest is refused, and a step of a whole day from
    its start lands past `now`, emptying it. The history held begins where that window
    ended, eighteen hours back -- not at the step's target, which would invert the planned
    range, and not at the ceiling, which would claim eighteen stored hours were never held.
    """
    harness = Harness(history_start=None)
    venue = SimulatedVenue(
        nine_fills(),
        retention=timedelta(days=1),
        max_query_window=timedelta(hours=6),
        fault=refuse_older_than(T0 - timedelta(hours=20)),
    )

    summary = await harness.run(factory, venue)

    account = await account_row(factory)
    assert account["planned_until"] == sqlite_timestamp(T0)
    assert account["effective_since"] <= account["planned_until"], "the planned range inverted"
    assert account["effective_since"] == sqlite_timestamp(T0 - timedelta(hours=18)), (
        "the floor is where the held history begins: the emptied window's end"
    )
    assert summary.status is SyncRunStatus.SUCCESS
    assert await window_rows(factory) == []
    assert_each_once(await trade_ids(factory), ALL_NINE)


async def test_a_window_exactly_as_long_as_the_limit_resumes_at_its_cursor(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every backfill window but the oldest is exactly `max_query_window` long.

    With no history start the first plan is thirteen seven-day windows, the newest holding
    the nine fills. Page 3 fails; the next run resumes that window at page 2's cursor. A
    queue normalisation that re-split a window of *exactly* the limit would replace it with
    an identical one and restart it from its first page.
    """
    harness = Harness(history_start=None)
    newest = FillWindow(since=T0 - timedelta(days=7), until=T0)
    await harness.run(
        factory,
        SimulatedVenue(nine_fills(), fault=faults_on({3: ExchangeUnavailableError(status=503)})),
    )
    venue = SimulatedVenue(nine_fills())

    await harness.run(factory, venue)

    assert newest.duration == venue.capabilities.max_query_window
    assert venue.calls[0] == PageCall(newest, "1004", None)
    assert PageCall(newest, None, None) not in venue.calls
    assert_each_once(await trade_ids(factory), ALL_NINE)


async def test_a_clock_stepped_back_behind_the_configured_start_plans_no_bottom(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The recorded start is compared through the same `min(..., now)` as the clamp.

    The first run read from the configured 2026-09-20. The clock then steps back to 09-10,
    behind that date: the clamp asks from `now`, and the recorded 09-20 compared raw would
    look like the owner asking for ten more days -- a bottom range nobody requested.
    """
    harness = Harness(history_start=date(2026, 9, 20))
    await harness.run(factory, SimulatedVenue(nine_fills()))
    start = sqlite_timestamp(datetime(2026, 9, 20, tzinfo=UTC))
    assert (await account_row(factory))["effective_since"] == start
    harness.clock.moment = datetime(2026, 9, 10, tzinfo=UTC)
    venue = SimulatedVenue(nine_fills())

    summary = await harness.run(factory, venue)

    assert venue.calls == [], "no range was asked for: nothing was requested"
    assert summary.status is SyncRunStatus.SUCCESS
    account = await account_row(factory)
    assert account["effective_since"] == start
    assert account["requested_since"] == start


async def test_a_crash_at_any_commit_never_separates_the_fills_from_their_checkpoint(
    tmp_path: Path,
) -> None:
    """The process dies at the first commit, then at the second, and so on to the end.

    After every crash the fills on disk are exactly the pages the checkpoint says were read,
    and a restart finishes with every fill once. A sync that committed the fills and the
    cursor in two transactions passes every other test in this module and fails this one,
    at the commit between the two.
    """
    crashed_at: list[int] = []
    for crash_at in range(1, 30):
        directory = tmp_path / f"crash-{crash_at}"
        directory.mkdir()
        harness = Harness()

        async with migrated_sessionmaker(directory) as first_process:
            async with first_process() as session:
                await insert_user(session)
            try:
                await harness.run(
                    first_process,
                    SimulatedVenue(nine_fills()),
                    on_session=dying_at_commit(crash_at),
                )
            except SimulatedPowerLoss:
                crashed_at.append(crash_at)
            else:
                break

        async with migrated_sessionmaker(directory) as second_process:
            windows = await window_rows(second_process)
            accounts = await rows(second_process, ACCOUNTS_SQL)
            planned = bool(accounts) and accounts[0]["planned_until"] is not None
            stored = await trade_ids(second_process)
            assert set(stored) == pages_the_checkpoint_says_were_read(windows, planned), (
                f"after a crash at commit {crash_at}, the fills on disk disagree with the "
                f"checkpoint: {sorted(stored)} against {windows}"
            )

            summary = await harness.run(second_process, SimulatedVenue(nine_fills()))

            assert summary.status is SyncRunStatus.SUCCESS
            assert_each_once(await trade_ids(second_process), ALL_NINE)

    assert len(crashed_at) >= 5, f"only {len(crashed_at)} commits were crashed at"


# --------------------------------------------------------------------------------------
# Criterion 2: re-running inserts nothing, and a revised fill is a conflict
# --------------------------------------------------------------------------------------


async def test_rerunning_a_completed_sync_inserts_zero_rows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A minute later the top window reaches five minutes back and re-reads five fills."""
    harness = Harness()
    await harness.run(factory, SimulatedVenue(nine_fills()))
    harness.clock.advance(timedelta(minutes=1))
    venue = SimulatedVenue(nine_fills())

    summary = await harness.run(factory, venue)

    assert {call.window for call in venue.calls} == {
        FillWindow(since=T0 - timedelta(minutes=5), until=T0 + timedelta(minutes=1))
    }
    assert summary.fills_seen == 5
    assert summary.fills_inserted == 0
    assert summary.status is SyncRunStatus.SUCCESS
    assert_each_once(await trade_ids(factory), ALL_NINE)


async def test_a_fill_the_venue_indexed_late_is_caught_by_the_overlap(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Why the overlap exists: a fill executed before the last run's ceiling, indexed after."""
    harness = Harness()
    fills = nine_fills()
    await harness.run(factory, SimulatedVenue(fills))
    late = make_fill(2000, T0 - timedelta(minutes=4, seconds=30))
    harness.clock.advance(timedelta(minutes=1))

    summary = await harness.run(factory, SimulatedVenue([*fills, late]))

    assert summary.fills_inserted == 1
    assert "2000" in await trade_ids(factory)


async def test_a_conflict_fails_the_account_without_advancing_the_cursor(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A settled fill revised under its own id stops the account at that page.

    1005 is already stored with a different quantity. Page 2 carries it, so page 2 -- and
    its new fills, 1006 and 1004 -- is rolled back, and the cursor stays at page 1's.
    """
    harness = Harness()
    fills = nine_fills()
    revised = {fill.external_trade_id: fill for fill in fills}["1005"]
    async with factory() as session:
        owner = await session.scalar(text("SELECT id FROM users"))
        account = await ExchangeAccountRepository(session).ensure(
            user_id=owner, exchange_key=ExchangeKey.BITGET, created_at=T0
        )
        await ExchangeFillRepository(session).insert_page(
            account.id, [changed(revised, quantity=Decimal("0.7"))], ingested_at=T0
        )
        await session.commit()

    summary = await harness.run(factory, SimulatedVenue(fills))

    outcome = only(summary)
    assert outcome.status is AccountOutcomeStatus.FAILED
    assert outcome.error_kind is ExchangeSyncErrorKind.CONFLICT
    assert outcome.detail is not None
    assert "1005" not in outcome.detail
    assert set(await trade_ids(factory)) == PAGE_ONE | {"1005"}
    assert [row["cursor"] for row in await window_rows(factory)] == ["1007"]
    stored = {row["external_trade_id"]: row for row in await rows(factory, FILLS_SQL)}
    assert Decimal(stored["1005"]["quantity"]) == Decimal("0.7"), "the first version is kept"
    assert (await account_row(factory))["sync_status"] == "error"


# --------------------------------------------------------------------------------------
# Criterion 3: the clamp records both starts
# --------------------------------------------------------------------------------------


async def test_the_retention_clamp_records_requested_and_effective_since(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """No history start: all of it was asked for, ninety days less a margin is what is held."""
    harness = Harness(history_start=None)
    venue = SimulatedVenue(nine_fills())

    await harness.run(factory, venue)

    account = await account_row(factory)
    assert account["requested_since"] == sqlite_timestamp(HISTORY_GENESIS)
    assert account["requested_since"] == "2009-01-03 00:00:00.000000"
    assert account["effective_since"] == sqlite_timestamp(
        T0 - timedelta(days=90) + timedelta(minutes=5)
    )
    assert min(call.window.since for call in venue.calls) == T0 - timedelta(days=90) + timedelta(
        minutes=5
    ), "nothing older than the clamp is asked for"
    assert_each_once(await trade_ids(factory), ALL_NINE)


async def test_a_history_start_inside_retention_is_both_starts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness(history_start=date(2026, 9, 1))

    await harness.run(factory, SimulatedVenue(nine_fills()))

    account = await account_row(factory)
    start = sqlite_timestamp(datetime(2026, 9, 1, tzinfo=UTC))
    assert (account["requested_since"], account["effective_since"]) == (start, start)


async def test_a_clock_behind_the_configured_start_records_the_configured_value(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A clock stepped back behind the history start must not raise; nothing is planned."""
    harness = Harness(history_start=date(2026, 9, 26))
    venue = SimulatedVenue(nine_fills())

    summary = await harness.run(factory, venue)

    assert summary.status is SyncRunStatus.SUCCESS
    assert venue.calls == []
    account = await account_row(factory)
    assert account["requested_since"] == sqlite_timestamp(datetime(2026, 9, 26, tzinfo=UTC))
    assert account["effective_since"] == sqlite_timestamp(T0)


# --------------------------------------------------------------------------------------
# Criterion 4: an auth error is terminal until the owner acts
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (ExchangeAuthError(status=401, venue_code="40006"), ExchangeSyncErrorKind.AUTH),
        (
            ExchangeInsufficientScopeError(status=400, venue_code="40014"),
            ExchangeSyncErrorKind.INSUFFICIENT_SCOPE,
        ),
    ],
    ids=["auth", "insufficient scope"],
)
async def test_an_auth_error_marks_the_account_and_stops_without_retrying(
    factory: async_sessionmaker[AsyncSession],
    error: ExchangeAuthError,
    kind: ExchangeSyncErrorKind,
) -> None:
    """One request, no sleep, `auth_failed`. The scope subclass is matched before its parent."""
    harness = Harness()
    venue = SimulatedVenue(nine_fills(), fault=always(error))

    summary = await harness.run(factory, venue)

    assert len(venue.calls) == 1, "an auth error is not retried"
    assert harness.sleeper.slept == []
    outcome = only(summary)
    assert outcome.status is AccountOutcomeStatus.FAILED
    assert outcome.error_kind is kind
    assert outcome.detail == str(error)
    assert (await account_row(factory))["sync_status"] == "auth_failed"
    assert summary.status is SyncRunStatus.FAILED


async def test_insufficient_scope_is_auth_failed_too(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Named by the spec; the parametrised test above covers it, this pins the status alone."""
    harness = Harness()

    await harness.run(
        factory,
        SimulatedVenue(fault=always(ExchangeInsufficientScopeError(status=403))),
    )

    assert (await account_row(factory))["sync_status"] == "auth_failed"


@pytest.mark.parametrize("trigger", [SyncTrigger.SCHEDULED, SyncTrigger.STARTUP])
async def test_a_scheduled_run_skips_an_auth_failed_account(
    factory: async_sessionmaker[AsyncSession], trigger: SyncTrigger
) -> None:
    """Asking a venue to refuse the same key every fifteen minutes is how an IP gets banned."""
    harness = Harness()
    await harness.run(factory, SimulatedVenue(fault=always(ExchangeAuthError(status=401))))
    harness.clock.advance(timedelta(minutes=15))
    venue = SimulatedVenue(nine_fills(), symbols=("BTCUSDT",))

    summary = await harness.run(factory, venue, trigger)

    assert venue.calls == []
    assert venue.symbol_calls == 0
    outcome = only(summary)
    assert outcome.status is AccountOutcomeStatus.SKIPPED
    assert outcome.error_kind is None
    assert summary.status is SyncRunStatus.SUCCESS, "nothing attempted is not a failure"
    assert (summary.accounts_total, summary.accounts_skipped) == (1, 1)
    assert (summary.accounts_succeeded, summary.accounts_failed) == (0, 0)
    assert (await account_row(factory))["sync_status"] == "auth_failed"
    outcomes = await rows(factory, OUTCOMES_SQL)
    assert [row["status"] for row in outcomes] == ["failed", "skipped"]


async def test_a_manual_run_retries_an_auth_failed_account(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The owner fixed the key, restarted, and pressed sync."""
    harness = Harness()
    await harness.run(factory, SimulatedVenue(fault=always(ExchangeAuthError(status=401))))
    venue = SimulatedVenue(nine_fills())

    summary = await harness.run(factory, venue, SyncTrigger.MANUAL)

    assert venue.calls, "a manual sync must call the venue"
    assert only(summary).status is AccountOutcomeStatus.SUCCESS
    assert (await account_row(factory))["sync_status"] == "ok"
    assert_each_once(await trade_ids(factory), ALL_NINE)


# --------------------------------------------------------------------------------------
# Criterion 5: a rate limit is retried and the run still completes
# --------------------------------------------------------------------------------------


def throttled(retry_after_ms: int | None = None) -> ExchangeRateLimitedError:
    return ExchangeRateLimitedError(status=429, venue_code="429", retry_after_ms=retry_after_ms)


async def test_a_rate_limit_is_retried_and_the_run_completes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """1.5 s from the venue is slept as 2 whole seconds -- rounded up -- then the page again."""
    harness = Harness()
    venue = SimulatedVenue(nine_fills(), fault=faults_on({2: throttled(1500)}))

    summary = await harness.run(factory, venue)

    assert harness.sleeper.slept == [2]
    assert venue.cursors() == [None, "1007", "1007", "1004"], "the same page, asked again"
    assert summary.status is SyncRunStatus.SUCCESS
    assert only(summary).status is AccountOutcomeStatus.SUCCESS
    assert_each_once(await trade_ids(factory), ALL_NINE)


def throttle_page_two(times: int, retry_after_ms: int | None) -> Fault:
    """Refuse the first `times` requests for page 2, then answer."""
    refused = [0]

    def fault(call_number: int, window: FillWindow, cursor: str | None) -> BaseException | None:
        del call_number, window
        if cursor == "1007" and refused[0] < times:
            refused[0] += 1
            return throttled(retry_after_ms)
        return None

    return fault


async def test_three_retries_are_allowed_and_the_fourth_attempt_can_succeed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Exactly `RATE_LIMIT_RETRIES`: three refusals, a fourth attempt, and the run completes."""
    harness = Harness()
    venue = SimulatedVenue(nine_fills(), fault=throttle_page_two(3, 1000))

    summary = await harness.run(factory, venue)

    assert harness.sleeper.slept == [1, 1, 1]
    assert venue.cursors() == [None, "1007", "1007", "1007", "1007", "1004"]
    assert summary.status is SyncRunStatus.SUCCESS
    assert_each_once(await trade_ids(factory), ALL_NINE)


async def test_exhausted_retries_fail_the_account_and_keep_the_checkpoint(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Four attempts at page 2, three sleeps, then `rate_limited` -- and page 1 is kept."""
    harness = Harness()
    venue = SimulatedVenue(nine_fills(), fault=throttle_page_two(99, 1000))

    summary = await harness.run(factory, venue)

    assert venue.cursors() == [None, "1007", "1007", "1007", "1007"]
    assert harness.sleeper.slept == [1, 1, 1]
    outcome = only(summary)
    assert outcome.status is AccountOutcomeStatus.FAILED
    assert outcome.error_kind is ExchangeSyncErrorKind.RATE_LIMITED
    assert set(await trade_ids(factory)) == PAGE_ONE
    assert [row["cursor"] for row in await window_rows(factory)] == ["1007"]
    assert (await account_row(factory))["sync_status"] == "error"


async def test_without_a_retry_after_the_waits_double(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """No `Retry-After`: `2 ** attempt` with attempts counted from one -- 2, 4, 8."""
    harness = Harness()

    await harness.run(factory, SimulatedVenue(nine_fills(), fault=throttle_page_two(99, None)))

    assert harness.sleeper.slept == [2, 4, 8]


async def test_a_wait_beyond_the_cap_is_not_slept(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sixty-one seconds is not waited: the coordinator is not held for a venue's afternoon."""
    harness = Harness()
    venue = SimulatedVenue(nine_fills(), fault=throttle_page_two(99, 61_000))

    summary = await harness.run(factory, venue)

    assert harness.sleeper.slept == []
    assert venue.cursors() == [None, "1007"]
    assert only(summary).error_kind is ExchangeSyncErrorKind.RATE_LIMITED
    assert set(await trade_ids(factory)) == PAGE_ONE


# --------------------------------------------------------------------------------------
# Every other failure, by class
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (ExchangeUnavailableError(status=503), ExchangeSyncErrorKind.UNAVAILABLE),
        (
            ExchangeInvalidRequestError(status=400, venue_code="40017"),
            ExchangeSyncErrorKind.INVALID_REQUEST,
        ),
        (ExchangeSchemaError("quantity must be greater than zero"), ExchangeSyncErrorKind.SCHEMA),
    ],
    ids=["unavailable", "invalid request", "schema"],
)
async def test_an_exchange_error_fails_the_account_with_its_kind_and_its_message(
    factory: async_sessionmaker[AsyncSession], error: Exception, kind: ExchangeSyncErrorKind
) -> None:
    harness = Harness()
    venue = SimulatedVenue(nine_fills(), fault=always(error))

    summary = await harness.run(factory, venue)

    assert len(venue.calls) == 1, "none of these is retried: the transport already did"
    outcome = only(summary)
    assert outcome.error_kind is kind
    assert outcome.detail == str(error)
    assert (await account_row(factory))["sync_status"] == "error"
    assert (await account_row(factory))["last_synced_at"] is None


async def test_an_internal_error_records_the_type_name_and_logs_the_traceback(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Our bug, not the venue's: `internal`, the type name only, the traceback in the log."""
    harness = Harness()
    venue = SimulatedVenue(nine_fills(), fault=always(KeyError("tradeId 1005 of BTCUSDT")))

    with capture_logs() as captured:
        summary = await harness.run(factory, venue)

    outcome = only(summary)
    assert outcome.error_kind is ExchangeSyncErrorKind.INTERNAL
    assert outcome.detail == "KeyError"
    logged = [
        entry for entry in captured if entry["event"] == "exchange_sync_account_internal_error"
    ]
    assert len(logged) == 1
    assert logged[0].get("exc_info"), "the traceback is logged"
    assert logged[0]["error_type"] == "KeyError"


async def test_an_error_after_a_failure_can_recover_to_ok(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    await harness.run(factory, SimulatedVenue(fault=always(ExchangeUnavailableError(status=502))))
    assert (await account_row(factory))["sync_status"] == "error"

    await harness.run(factory, SimulatedVenue(nine_fills()))

    account = await account_row(factory)
    assert account["sync_status"] == "ok"
    assert account["last_synced_at"] == sqlite_timestamp(T0)


# --------------------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------------------


def spread_across(days: int) -> list[Any]:
    """One fill every twelve hours, ending at 11:00 on `T0`'s day, ids rising with time."""
    return [
        make_fill(3000 + index, T0 - timedelta(hours=1) - timedelta(hours=12) * (days * 2 - index))
        for index in range(days * 2 + 1)
    ]


async def test_pending_and_new_windows_are_read_newest_first_across_the_queue(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A run interrupted at its second window, then a run a day later.

    The later run has a new top window and three windows left over. It reads them all
    newest first: the new top, then the leftovers from the newest down.
    """
    harness = Harness(history_start=date(2026, 9, 1))
    fills = spread_across(24)

    def fail_below_the_top(
        call_number: int, window: FillWindow, cursor: str | None
    ) -> BaseException | None:
        del call_number, cursor
        return ExchangeUnavailableError(status=503) if window.until < T0 else None

    await harness.run(factory, SimulatedVenue(fills, fault=fail_below_the_top))
    leftover = await window_rows(factory)
    assert len(leftover) == 3
    harness.clock.advance(timedelta(days=1))
    venue = SimulatedVenue(fills)

    summary = await harness.run(factory, venue)

    firsts = [call.window for call in venue.calls if call.cursor is None]
    assert firsts[0] == FillWindow(since=T0 - timedelta(minutes=5), until=T0 + timedelta(days=1))
    untils = [window.until for window in firsts]
    assert untils == sorted(untils, reverse=True)
    assert len(firsts) == 4
    assert summary.status is SyncRunStatus.SUCCESS
    assert_each_once(await trade_ids(factory), [fill.external_trade_id for fill in fills])


async def test_a_cursor_that_returns_to_one_already_sent_is_a_schema_error(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A -> B -> A: `require_cursor_advanced` sees only A -> A, the sync sees the cycle.

    Three requests and a `schema` failure, not a Raspberry Pi paging forever. The venue has
    a call budget, so a sync without the check fails this test rather than hanging it.
    """
    harness = Harness()
    venue = SimulatedVenue(next_cursors={None: "500", "500": "400", "400": "500"})

    summary = await harness.run(factory, venue)

    assert venue.cursors() == [None, "500", "400"]
    outcome = only(summary)
    assert outcome.error_kind is ExchangeSyncErrorKind.SCHEMA
    assert [row["cursor"] for row in await window_rows(factory)] == ["400"]


async def test_a_cursor_repeated_by_the_venue_is_a_schema_error(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A -> A, which the page assembly refuses before the sync sees it."""
    harness = Harness()
    venue = SimulatedVenue(next_cursors={None: "500", "500": "500"})

    summary = await harness.run(factory, venue)

    assert venue.cursors() == [None, "500"]
    assert only(summary).error_kind is ExchangeSyncErrorKind.SCHEMA


async def test_a_cycle_back_to_the_cursor_a_window_resumed_from_is_caught(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The resumed cursor counts as sent: 500 -> 400 -> 500 after a restart at 500.

    The first run commits page 1 (next cursor 500) and fails on page 2. The second resumes at
    500, is sent to 400, and is sent back to 500 -- the cursor it started from. Two requests,
    not three: a sync that forgot the cursor it resumed from would ask for 500 again before
    noticing.
    """
    harness = Harness()
    cursors = {None: "500", "500": "400", "400": "500"}
    await harness.run(
        factory,
        SimulatedVenue(
            next_cursors=cursors, fault=faults_on({2: ExchangeUnavailableError(status=503)})
        ),
    )
    assert [row["cursor"] for row in await window_rows(factory)] == ["500"]
    venue = SimulatedVenue(next_cursors=cursors)

    summary = await harness.run(factory, venue)

    assert venue.cursors() == ["500", "400"]
    assert only(summary).error_kind is ExchangeSyncErrorKind.SCHEMA


async def test_a_full_page_from_a_venue_without_a_cursor_splits_the_window(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`NONE`: a full page is a truncated window. It is not inserted; the halves are read.

    Five fills and a page of three. The whole window comes back full, so it is split and
    the newer half read first; its older half is full again and split again.
    """
    harness = Harness()
    fills = [make_fill(4000 + hour, MIDNIGHT + timedelta(hours=hour)) for hour in (1, 3, 5, 7, 9)]
    venue = SimulatedVenue(fills, cursor_kind=CursorKind.NONE)

    summary = await harness.run(factory, venue)

    six = MIDNIGHT + timedelta(hours=6)
    three = MIDNIGHT + timedelta(hours=3)
    assert [call.window for call in venue.calls] == [
        WINDOW,
        FillWindow(since=six, until=T0),
        FillWindow(since=MIDNIGHT, until=six),
        FillWindow(since=three, until=six),
        FillWindow(since=MIDNIGHT, until=three),
    ]
    assert all(call.cursor is None for call in venue.calls)
    assert_each_once(await trade_ids(factory), [fill.external_trade_id for fill in fills])
    assert summary.status is SyncRunStatus.SUCCESS
    assert await window_rows(factory) == []


async def test_a_full_page_in_a_window_too_small_to_split_is_a_schema_error(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three fills in one millisecond and a page of three: no split can ever make it fit."""
    harness = Harness()
    fills = [make_fill(5000 + index, MIDNIGHT) for index in range(3)]
    venue = SimulatedVenue(fills, cursor_kind=CursorKind.NONE)

    summary = await harness.run(factory, venue)

    assert only(summary).error_kind is ExchangeSyncErrorKind.SCHEMA
    assert await trade_ids(factory) == [], "a truncated page is never inserted"
    assert venue.calls[-1].window.duration == timedelta(milliseconds=1)


async def test_a_venue_that_requires_a_symbol_is_asked_per_window_and_symbol(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    fills = fills_between(4, symbols=("BTCUSDT", "KASUSDT"))
    venue = SimulatedVenue(fills, requires_symbol=True, symbols=("KASUSDT", "BTCUSDT", "KASUSDT"))

    summary = await harness.run(factory, venue)

    assert venue.symbol_calls == 1
    firsts = [(call.window, call.symbol) for call in venue.calls if call.cursor is None]
    assert len(firsts) == 2, "one window per symbol, and a repeated candidate asked once"
    assert set(firsts) == {(WINDOW, "BTCUSDT"), (WINDOW, "KASUSDT")}
    assert_each_once(await trade_ids(factory), [fill.external_trade_id for fill in fills])
    assert summary.status is SyncRunStatus.SUCCESS


async def test_a_venue_with_no_candidate_symbols_plans_no_rows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    venue = SimulatedVenue(nine_fills(), requires_symbol=True, symbols=())

    summary = await harness.run(factory, venue)

    assert venue.calls == []
    assert await window_rows(factory) == []
    assert only(summary).status is AccountOutcomeStatus.SUCCESS


async def test_symbol_discovery_is_only_asked_when_there_are_new_windows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    await harness.run(factory, SimulatedVenue(requires_symbol=True, symbols=("BTCUSDT",)))
    venue = SimulatedVenue(requires_symbol=True, symbols=("BTCUSDT",))

    await harness.run(factory, venue)

    assert venue.symbol_calls == 0, "the clock did not move, so nothing new was planned"


def a_writer_can_begin(database: Path, seen: list[str | None]) -> Callable[[], None]:
    """Try to take SQLite's write lock from a second connection, without waiting for it.

    Records `None` when `BEGIN IMMEDIATE` succeeds, or the refusal's message when another
    connection -- the sync's own session -- is holding a write transaction open.
    """

    def attempt() -> None:
        connection = sqlite3.connect(database, timeout=0)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
            seen.append(None)
        except sqlite3.OperationalError as error:
            seen.append(str(error))
        finally:
            connection.close()

    return attempt


async def test_no_write_transaction_is_held_open_while_symbols_are_discovered(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Discovery is a network call, and SQLite has one writer: nothing may wait behind it.

    The second run reshapes the pending windows the retention floor has passed -- writes --
    before it asks the venue for its symbols. If those writes were still uncommitted, every
    other writer (a login, a manual sync of balances) would wait on the venue's latency, and
    past the five-second busy timeout it would fail. A second connection taking the write
    lock from inside `candidate_symbols` is the proof.
    """
    database = tmp_path / "test.db"
    harness = Harness(history_start=None)
    first_seen: list[str | None] = []
    first = SimulatedVenue(
        requires_symbol=True,
        symbols=("BTCUSDT",),
        fault=always(ExchangeUnavailableError(status=503)),
    )
    first.on_symbols = a_writer_can_begin(database, first_seen)
    await harness.run(factory, first)
    harness.clock.advance(timedelta(days=3))
    seen: list[str | None] = []
    venue = SimulatedVenue(nine_fills(), requires_symbol=True, symbols=("BTCUSDT",))
    venue.on_symbols = a_writer_can_begin(database, seen)

    summary = await harness.run(factory, venue)

    assert first_seen == [None]
    assert seen == [None], "a write transaction was open while the venue was being asked"
    assert summary.status is SyncRunStatus.SUCCESS


async def test_no_write_transaction_is_open_while_a_rate_limited_discovery_waits(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """F4 with the retry: both attempts at discovery, and the wait between them, hold no lock."""
    database = tmp_path / "test.db"
    harness = Harness(history_start=None)
    await harness.run(
        factory,
        SimulatedVenue(
            requires_symbol=True,
            symbols=("BTCUSDT",),
            fault=always(ExchangeUnavailableError(status=503)),
        ),
    )
    harness.clock.advance(timedelta(days=3))
    seen: list[str | None] = []
    venue = SimulatedVenue(
        nine_fills(),
        requires_symbol=True,
        symbols=("BTCUSDT",),
        symbols_fault=throttled(1000),
        symbols_fault_times=1,
    )
    venue.on_symbols = a_writer_can_begin(database, seen)

    summary = await harness.run(factory, venue)

    assert seen == [None, None]
    assert harness.sleeper.slept == [1]
    assert summary.status is SyncRunStatus.SUCCESS


async def test_symbol_discovery_is_retried_after_a_rate_limit_like_a_page(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The spec gives `candidate_symbols()` the same rate-limit retry as a page."""
    harness = Harness()
    venue = SimulatedVenue(
        nine_fills(),
        requires_symbol=True,
        symbols=("BTCUSDT",),
        symbols_fault=throttled(2500),
        symbols_fault_times=2,
    )

    summary = await harness.run(factory, venue)

    assert venue.symbol_calls == 3
    assert harness.sleeper.slept == [3, 3]
    assert only(summary).status is AccountOutcomeStatus.SUCCESS
    assert_each_once(await trade_ids(factory), ALL_NINE)


@pytest.mark.parametrize(
    ("error", "kind", "status"),
    [
        (ExchangeAuthError(status=401), ExchangeSyncErrorKind.AUTH, "auth_failed"),
        (ExchangeUnavailableError(status=503), ExchangeSyncErrorKind.UNAVAILABLE, "error"),
    ],
    ids=["auth", "unavailable"],
)
async def test_symbol_discovery_fails_like_a_page(
    factory: async_sessionmaker[AsyncSession],
    error: Exception,
    kind: ExchangeSyncErrorKind,
    status: str,
) -> None:
    harness = Harness()
    fills = nine_fills()
    venue = SimulatedVenue(fills, requires_symbol=True, symbols=("BTCUSDT",), symbols_fault=error)

    summary = await harness.run(factory, venue)

    assert only(summary).error_kind is kind
    assert (await account_row(factory))["sync_status"] == status
    assert venue.calls == []

    # The plan that discovery failed under must not be lost: the next run, with discovery
    # answering, still reads the window the failed run planned -- at the same clock, when
    # nothing new would be planned.
    recovered = SimulatedVenue(fills, requires_symbol=True, symbols=("BTCUSDT",))
    await harness.run(factory, recovered, SyncTrigger.MANUAL)

    assert {(call.window, call.symbol) for call in recovered.calls} == {(WINDOW, "BTCUSDT")}
    assert_each_once(await trade_ids(factory), ALL_NINE)


def refuse_older_than(edge: datetime) -> Fault:
    """A venue whose real retention is shorter than it declares."""

    def fault(call_number: int, window: FillWindow, cursor: str | None) -> BaseException | None:
        del call_number, cursor
        if window.since < edge:
            return ExchangeRetentionWindowError(status=400, venue_code="40704")
        return None

    return fault


async def test_a_retention_refusal_steps_the_window_forward_a_day_at_a_time(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The venue keeps 88.5 days, not the 90 it declares: two steps, then it answers."""
    harness = Harness(history_start=None)
    edge = T0 - timedelta(days=88, hours=12)
    venue = SimulatedVenue(nine_fills(), fault=refuse_older_than(edge))
    oldest = T0 - timedelta(days=90) + timedelta(minutes=5)

    with capture_logs() as captured:
        summary = await harness.run(factory, venue)

    asked = [
        call.window.since for call in venue.calls if call.window.until == T0 - timedelta(days=84)
    ]
    assert asked == [oldest, oldest + timedelta(days=1), oldest + timedelta(days=2)]
    assert summary.status is SyncRunStatus.SUCCESS
    account = await account_row(factory)
    assert account["effective_since"] == sqlite_timestamp(oldest + timedelta(days=2))
    assert account["requested_since"] == sqlite_timestamp(HISTORY_GENESIS)
    assert await window_rows(factory) == []
    assert (
        len([entry for entry in captured if entry["event"] == "exchange_sync_retention_step"]) == 2
    )


async def test_history_the_venue_refused_is_not_asked_for_again_on_the_next_run(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A retention step raises the floor above the clamp; the next run must not plan below it.

    `plan_account` plans a bottom range whenever the clamp reaches earlier than the recorded
    floor, which the spec says "only happens when the owner moves the history start
    earlier". After a retention step it also happens with nothing moved: the floor is the
    stepped `since`, the clamp still says ninety days. The next run fifteen minutes later
    must read only its top window, not ask the venue again for the days it just refused.
    """
    harness = Harness(history_start=None)
    edge = T0 - timedelta(days=88, hours=12)
    await harness.run(factory, SimulatedVenue(nine_fills(), fault=refuse_older_than(edge)))
    harness.clock.advance(timedelta(minutes=15))
    venue = SimulatedVenue(nine_fills(), fault=refuse_older_than(edge + timedelta(minutes=15)))

    summary = await harness.run(factory, venue)

    # Two pages of that one window: the five fills of its last five minutes, three a page.
    assert {call.window for call in venue.calls} == {
        FillWindow(since=T0 - timedelta(minutes=5), until=T0 + timedelta(minutes=15))
    }
    assert summary.status is SyncRunStatus.SUCCESS


async def test_exhausted_retention_steps_fail_the_account_and_keep_the_moved_since(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three steps per window per run, a fourth refusal fails `retention_window`.

    The venue keeps only eighty days. Windows are read newest first, so the refusals start
    at the window beginning eighty-four days back; each step also drops the oldest window,
    which now lies wholly below the stepped `since`. The moved `since` is persisted, and the
    next run, one step on, gets its answer.
    """
    harness = Harness(history_start=None)
    edge = T0 - timedelta(days=80)
    venue = SimulatedVenue(nine_fills(), fault=refuse_older_than(edge))

    summary = await harness.run(factory, venue)

    refused = [call.window.since for call in venue.calls if call.window.since < edge]
    base = T0 - timedelta(days=84)
    assert refused == [
        base,
        base + timedelta(days=1),
        base + timedelta(days=2),
        base + timedelta(days=3),
    ]
    outcome = only(summary)
    assert outcome.error_kind is ExchangeSyncErrorKind.RETENTION_WINDOW
    assert (await account_row(factory))["sync_status"] == "error"
    (pending,) = await window_rows(factory)
    assert pending["since"] == sqlite_timestamp(base + timedelta(days=3))
    assert (await account_row(factory))["effective_since"] == sqlite_timestamp(
        base + timedelta(days=3)
    )

    again = SimulatedVenue(nine_fills(), fault=refuse_older_than(edge))
    second = await harness.run(factory, again)

    assert [call.window.since for call in again.calls] == [
        base + timedelta(days=3),
        base + timedelta(days=4),
    ]
    assert second.status is SyncRunStatus.SUCCESS
    assert await window_rows(factory) == []


async def test_one_venue_failing_leaves_the_other_to_succeed_as_partial(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Accounts run in `exchange_key` order, and one's failure is its outcome alone."""
    harness = Harness()
    bitget = SimulatedVenue(nine_fills())
    bingx = SimulatedVenue(
        exchange_key=ExchangeKey.BINGX, fault=always(ExchangeUnavailableError(status=503))
    )

    summary = await harness.run(factory, {ExchangeKey.BITGET: bitget, ExchangeKey.BINGX: bingx})

    assert summary.status is SyncRunStatus.PARTIAL
    assert [outcome.exchange_key for outcome in summary.accounts] == [
        ExchangeKey.BINGX,
        ExchangeKey.BITGET,
    ]
    assert [outcome.status for outcome in summary.accounts] == [
        AccountOutcomeStatus.FAILED,
        AccountOutcomeStatus.SUCCESS,
    ]
    assert (summary.accounts_total, summary.accounts_succeeded, summary.accounts_failed) == (
        2,
        1,
        1,
    )
    assert bingx.calls, "the failing venue was asked"
    assert_each_once(await trade_ids(factory), ALL_NINE)
    statuses = {
        row["exchange_key"]: row["sync_status"] for row in await rows(factory, ACCOUNTS_SQL)
    }
    assert statuses == {"bingx": "error", "bitget": "ok"}


async def test_every_attempted_account_failing_is_a_failed_run(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    down = always(ExchangeUnavailableError(status=503))

    summary = await harness.run(
        factory,
        {
            ExchangeKey.BITGET: SimulatedVenue(fault=down),
            ExchangeKey.BINGX: SimulatedVenue(exchange_key=ExchangeKey.BINGX, fault=down),
        },
    )

    assert summary.status is SyncRunStatus.FAILED


async def test_a_skipped_account_does_not_turn_a_failed_run_partial(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The run status is computed over the accounts **attempted**; a skipped one is not.

    BingX is `auth_failed` and skipped by the scheduled run; Bitget, the one attempted,
    fails. Every attempted account failed, so the run failed -- counting the skipped one
    would call it `partial`, as though something had worked.
    """
    harness = Harness()
    await harness.run(
        factory,
        {
            ExchangeKey.BITGET: SimulatedVenue(nine_fills()),
            ExchangeKey.BINGX: SimulatedVenue(
                exchange_key=ExchangeKey.BINGX, fault=always(ExchangeAuthError(status=401))
            ),
        },
    )
    harness.clock.advance(timedelta(minutes=15))

    summary = await harness.run(
        factory,
        {
            ExchangeKey.BITGET: SimulatedVenue(fault=always(ExchangeUnavailableError(status=503))),
            ExchangeKey.BINGX: SimulatedVenue(exchange_key=ExchangeKey.BINGX),
        },
    )

    assert [outcome.status for outcome in summary.accounts] == [
        AccountOutcomeStatus.SKIPPED,
        AccountOutcomeStatus.FAILED,
    ]
    assert summary.status is SyncRunStatus.FAILED
    assert (summary.accounts_failed, summary.accounts_skipped) == (1, 1)


async def test_a_failure_before_any_account_leaves_the_run_row_as_evidence(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run row is committed the moment it is opened, not with a later write.

    A failure between opening the run and the first account -- the owner lookup, here --
    propagates out of `sync`. What must survive it is the `running` row that says a run
    started, for the sweep to close; a row only flushed would be rolled back with the rest.
    """

    async def refuse(self: object) -> list[object]:
        del self
        message = "database is locked"
        raise RuntimeError(message)

    monkeypatch.setattr("portfolio.repositories.users.UserRepository.list_all", refuse)
    harness = Harness()
    venue = SimulatedVenue(nine_fills())

    with pytest.raises(RuntimeError, match="database is locked"):
        await harness.run(factory, venue)

    assert [run["status"] for run in await rows(factory, RUNS_SQL)] == ["running"]
    assert venue.calls == []


async def test_a_retention_step_that_empties_a_window_deletes_it_and_moves_on(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """ "A window left empty is deleted and the sync moves on."

    Day-long windows from 2026-09-23: `[09-24 12:00, 09-25 12:00)`, `[09-23 12:00, 09-24
    12:00)` and `[09-23 00:00, 09-23 12:00)`. The venue keeps nothing before 09-24, so the
    middle window is refused; one step moves its start to exactly its end, which empties it,
    and the oldest window lies wholly below the step. Both go, and the run succeeds having
    asked about the emptied window once -- not four times, and not failing on it.
    """
    harness = Harness(history_start=date(2026, 9, 23))
    edge = datetime(2026, 9, 24, tzinfo=UTC)
    venue = SimulatedVenue(
        nine_fills(), max_query_window=timedelta(days=1), fault=refuse_older_than(edge)
    )
    middle = FillWindow(
        since=datetime(2026, 9, 23, 12, tzinfo=UTC), until=datetime(2026, 9, 24, 12, tzinfo=UTC)
    )

    summary = await harness.run(factory, venue)

    assert [call.window for call in venue.calls].count(middle) == 1
    assert all(call.window.since >= middle.since for call in venue.calls), (
        "the oldest window, wholly below the step, is never asked about"
    )
    assert summary.status is SyncRunStatus.SUCCESS
    assert await window_rows(factory) == []
    assert (await account_row(factory))["effective_since"] == sqlite_timestamp(middle.until)
    assert_each_once(await trade_ids(factory), ALL_NINE)


async def test_windows_that_aged_out_while_stalled_raise_the_floor(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pending windows the rolling retention passed are dropped or shrunk, and it is recorded.

    The first run plans ninety days and reads nothing. Ten days later the retention edge has
    moved ten days: the oldest pending window is wholly below it and is dropped, the next
    one straddles it and is shrunk. The history held now starts at the new edge, and
    `effective_since` says so -- leaving it at the old floor would claim ten days nobody
    can read any more.
    """
    harness = Harness(history_start=None)
    await harness.run(factory, SimulatedVenue(fault=always(ExchangeUnavailableError(status=503))))
    assert (await account_row(factory))["effective_since"] == sqlite_timestamp(
        T0 - timedelta(days=90) + timedelta(minutes=5)
    )
    harness.clock.advance(timedelta(days=10))
    venue = SimulatedVenue(nine_fills())

    with capture_logs() as captured:
        summary = await harness.run(factory, venue)

    new_edge = T0 + timedelta(days=10) - timedelta(days=90) + timedelta(minutes=5)
    assert summary.status is SyncRunStatus.SUCCESS
    assert (await account_row(factory))["effective_since"] == sqlite_timestamp(new_edge)
    assert min(call.window.since for call in venue.calls) == new_edge
    truncated = [entry for entry in captured if entry["event"] == "exchange_sync_history_truncated"]
    assert [entry["windows_dropped"] for entry in truncated] == [1]


async def test_a_window_reshaped_by_the_retention_floor_keeps_its_symbol(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A replacement row is written with the original's symbol, or the venue cannot be asked.

    A venue that requires a symbol plans one row per symbol. When the floor shrinks such a
    row, the rows replacing it must still name the symbol: one without it is a request the
    provider refuses as the caller's mistake.
    """
    harness = Harness(history_start=None)
    await harness.run(
        factory,
        SimulatedVenue(
            requires_symbol=True,
            symbols=("BTCUSDT",),
            fault=always(ExchangeUnavailableError(status=503)),
        ),
    )
    harness.clock.advance(timedelta(days=3))
    venue = SimulatedVenue(nine_fills(), requires_symbol=True, symbols=("BTCUSDT",))

    summary = await harness.run(factory, venue)

    assert summary.status is SyncRunStatus.SUCCESS
    assert {call.symbol for call in venue.calls} == {"BTCUSDT"}
    floor = T0 + timedelta(days=3) - timedelta(days=90) + timedelta(minutes=5)
    assert min(call.window.since for call in venue.calls) == floor


async def test_an_account_whose_venue_is_no_longer_configured_is_not_synced(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The row stays -- its fills belong to it -- and nothing is asked of a venue with no key."""
    harness = Harness()
    await harness.run(
        factory,
        {
            ExchangeKey.BITGET: SimulatedVenue(),
            ExchangeKey.BINGX: SimulatedVenue(exchange_key=ExchangeKey.BINGX),
        },
    )

    summary = await harness.run(factory, SimulatedVenue(nine_fills()))

    assert [outcome.exchange_key for outcome in summary.accounts] == [ExchangeKey.BITGET]
    assert summary.accounts_total == 1
    assert {row["exchange_key"] for row in await rows(factory, ACCOUNTS_SQL)} == {"bingx", "bitget"}


async def test_no_owner_is_a_success_with_nothing_attempted(tmp_path: Path) -> None:
    harness = Harness()
    venue = SimulatedVenue(nine_fills())
    async with migrated_sessionmaker(tmp_path) as factory:
        with capture_logs() as captured:
            summary = await harness.run(factory, venue)

        assert summary.status is SyncRunStatus.SUCCESS
        assert summary.accounts == ()
        assert summary.accounts_total == 0
        assert venue.calls == []
        assert await rows(factory, ACCOUNTS_SQL) == []
    warnings = [entry for entry in captured if entry["event"] == "exchange_sync_no_owner"]
    assert [entry["log_level"] for entry in warnings] == ["warning"]


async def test_two_owners_fail_the_run_without_calling_any_venue(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Attaching fills to a guessed owner is worse than not importing them."""
    async with factory() as session:
        await insert_user(session, "a-second-owner")
    harness = Harness()
    venue = SimulatedVenue(nine_fills())

    with capture_logs() as captured:
        summary = await harness.run(factory, venue)

    assert summary.status is SyncRunStatus.FAILED
    assert summary.accounts == ()
    assert venue.calls == []
    assert await rows(factory, ACCOUNTS_SQL) == []
    errors = [entry for entry in captured if entry["event"] == "exchange_sync_multiple_owners"]
    assert [entry["log_level"] for entry in errors] == ["error"]


async def test_a_running_row_left_by_a_dead_process_is_swept_before_the_run_opens(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO exchange_sync_runs (trigger, status, started_at, accounts_total) "
                "VALUES ('scheduled', 'running', '2026-09-24 00:00:00.000000', 1)"
            )
        )
        await session.commit()
    harness = Harness()

    with capture_logs() as captured:
        await harness.run(factory, SimulatedVenue(nine_fills()))

    assert [run["status"] for run in await rows(factory, RUNS_SQL)] == ["interrupted", "success"]
    swept = [
        entry for entry in captured if entry["event"] == "exchange_sync_runs_marked_interrupted"
    ]
    assert [entry["runs"] for entry in swept] == [1]


async def test_the_duration_comes_from_the_monotonic_clock(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A wall clock stepped back mid-run cannot make a duration negative."""
    harness = Harness()
    venue = SimulatedVenue(nine_fills())
    start = harness.monotonic.now

    async def step_back(call: PageCall) -> None:
        del call
        harness.clock.moment = T0 - timedelta(hours=1)

    venue.on_call = step_back
    summary = await harness.run(factory, venue)

    assert summary.duration_ms is not None
    assert 0 < summary.duration_ms < harness.monotonic.now - start + 1
