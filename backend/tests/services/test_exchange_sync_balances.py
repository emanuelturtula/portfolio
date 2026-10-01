"""Criteria 3 and 4 of #104 (spec 025): the balance read at the end of an account's sync.

Every test drives the real `ExchangeSyncService` over a real migrated SQLite file, against
`SimulatedVenue` (`tests/exchange_sync_harness.py`), and reads the result back over a second
session -- what was committed, the only state a restart ever sees.

## What the spec says, and where each sentence is held

1. **The balances are read after the account's success outcome is committed.** Asserted from
   inside the venue's `fetch_balances`: a second connection reads the run log and the account
   while the venue is being asked, and finds the success already there.
2. **No read for an account whose fill sync failed or was skipped.** Fresh balances beside a
   stale history would produce differences that mean nothing.
3. **A successful read replaces the stored one**, whole, and stamps `balances_read_at` with
   our clock.
4. **A failed read changes nothing else.** The account's outcome, its `sync_status` and the
   run's status are compared, column for column, with a control run whose balance read
   succeeded: the only differences allowed are in `balances_error`. The last good rows and
   `balances_read_at` stay -- including when the failure is ours, part way through storing
   the answer, which is the case that tells a rollback from none.
   (Kept is not compared: since R9 the reconciliation leaves a failed venue's rows out of
   the sum. That rule is the service's, and is tested there.)
5. **`auth` and `insufficient_scope` are retried only by a manual sync.** A scheduled or
   startup run does not ask the venue again; every other kind is asked again.
6. **`fetch_balances` goes through the rate-limit retry**, and no write transaction is open
   while the venue answers, or while a rate-limited read waits.
7. **The log names the venue, a count, a kind and a type** -- never an asset or an amount.
   `tests/security/test_exchange_balance_logging.py` reads the production pipeline's stdout
   for the same thing; here the structured fields are pinned.
8. **The isolation is absolute (R5).** If *recording* the failure itself fails, the sync
   rolls back, logs `exchange_balances_failure_not_recorded`, and carries on with the
   outcome it already committed: the next account is still synced and the run is closed.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import event
from structlog.testing import capture_logs

from portfolio.domain.exchanges import ExchangeKey
from portfolio.providers.exchanges.errors import (
    ExchangeAuthError,
    ExchangeInsufficientScopeError,
    ExchangeInvalidRequestError,
    ExchangeRateLimitedError,
    ExchangeRetentionWindowError,
    ExchangeSchemaError,
    ExchangeUnavailableError,
)
from portfolio.repositories.exchange_balances import ExchangeBalanceRepository
from portfolio.repositories.exchange_sync_runs import (
    AccountOutcomeStatus,
    SyncRunStatus,
    SyncTrigger,
)
from tests.balance_harness import insert_user
from tests.exchange_sync_harness import (
    ACCOUNTS_SQL,
    BALANCE_STATE_SQL,
    BALANCES_SQL,
    OUTCOMES_SQL,
    RUNS_SQL,
    T0,
    SimulatedPowerLoss,
    SimulatedVenue,
    always,
    always_balances,
    balance_faults_on,
    held,
    rows,
    sqlite_timestamp,
    trade_ids,
)
from tests.services.test_exchange_sync import (
    ALL_NINE,
    Harness,
    a_writer_can_begin,
    nine_fills,
    only,
    throttled,
)
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.exchanges.base import AssetBalance

#: Distinctive, so their absence from a log or a column means something. Synthetic.
MARKED_ASSET: Final = "ZZMARKED"
MARKED_AMOUNT: Final = "4242.4242"

FIRST_READING: Final = (held("BTC", "0.25"), held("KAS", "1500"))
SECOND_READING: Final = (held("BTC", "0.75"), held("BGB", "40"))
FIRST_STORED: Final = [
    ("bitget", "BTC", "0.250000000000000000"),
    ("bitget", "KAS", "1500.000000000000000000"),
]
SECOND_STORED: Final = [
    ("bitget", "BGB", "40.000000000000000000"),
    ("bitget", "BTC", "0.750000000000000000"),
]
QUARTER_HOUR: Final = timedelta(minutes=15)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        async with built() as session:
            await insert_user(session)
        yield built


async def stored(factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, str, str]]:
    """Every balance on disk as `(exchange_key, asset, quantity as the column's text)`."""
    return [
        (str(row["exchange_key"]), str(row["asset"]), str(row["quantity"]))
        for row in await rows(factory, BALANCES_SQL)
    ]


async def balance_state(
    factory: async_sessionmaker[AsyncSession], key: str = "bitget"
) -> dict[str, Any]:
    (row,) = [row for row in await rows(factory, BALANCE_STATE_SQL) if row["exchange_key"] == key]
    return row


def events_named(captured: Sequence[Any], name: str) -> list[dict[str, Any]]:
    return [dict(entry) for entry in captured if entry["event"] == name]


def balance_events(captured: Sequence[Any]) -> list[dict[str, Any]]:
    return [dict(entry) for entry in captured if "balances" in str(entry["event"])]


class RawBalance:
    """A balance that never went through `AssetBalance` or `assemble_balances`.

    For the one thing a well-behaved venue cannot script: a reading the *table* refuses after
    the old rows are already deleted, which is where a missing rollback would show.
    """

    def __init__(self, asset: str, quantity: str) -> None:
        self.asset = asset
        self.quantity = Decimal(quantity)


class UnassembledVenue(SimulatedVenue):
    """A venue whose `fetch_balances` hands back whatever it holds, unchecked."""

    raw: Sequence[RawBalance] = ()

    async def fetch_balances(self) -> Sequence[AssetBalance]:
        self.balance_calls += 1
        return self.raw  # type: ignore[return-value]


# --------------------------------------------------------------------------------------
# 1. Read after the success is committed
# --------------------------------------------------------------------------------------


async def test_a_successful_sync_is_followed_by_one_balance_read_that_is_stored(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    venue = SimulatedVenue(nine_fills(), balances=FIRST_READING)

    summary = await harness.run(factory, venue)

    assert venue.balance_calls == 1, "one read per successful account, not one per page"
    assert venue.page_calls_before_balances == [3], "every page was read first"
    assert await stored(factory) == FIRST_STORED
    state = await balance_state(factory)
    assert state["balances_read_at"] == sqlite_timestamp(T0), "our clock, not the venue's"
    assert state["balances_error"] is None
    assert only(summary).status is AccountOutcomeStatus.SUCCESS
    assert summary.status is SyncRunStatus.SUCCESS
    assert sorted(await trade_ids(factory)) == ALL_NINE


async def test_the_success_outcome_is_on_disk_before_the_venue_is_asked_for_balances(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Read from a second connection, from inside `fetch_balances`.

    The account is already `ok`, its `last_synced_at` is set and its outcome row says
    `success`: whatever becomes of the balance read, that is what a crash leaves behind.
    """
    database = tmp_path / "test.db"
    harness = Harness()
    venue = SimulatedVenue(nine_fills(), balances=FIRST_READING)
    seen: list[dict[str, Any]] = []

    def look() -> None:
        connection = sqlite3.connect(database, timeout=0)
        connection.row_factory = sqlite3.Row
        try:
            account = dict(connection.execute("SELECT * FROM exchange_accounts").fetchone())
            outcomes = [
                dict(row)
                for row in connection.execute("SELECT * FROM exchange_sync_run_accounts").fetchall()
            ]
            fills = connection.execute("SELECT COUNT(*) FROM exchange_fills").fetchone()[0]
            balances = connection.execute("SELECT COUNT(*) FROM exchange_balances").fetchone()[0]
        finally:
            connection.close()
        seen.append(
            {"account": account, "outcomes": outcomes, "fills": fills, "balances": balances}
        )

    venue.on_balances = look
    await harness.run(factory, venue)

    (found,) = seen
    assert found["account"]["sync_status"] == "ok"
    assert found["account"]["last_synced_at"] == sqlite_timestamp(T0)
    assert [(row["status"], row["error_kind"]) for row in found["outcomes"]] == [("success", None)]
    assert found["outcomes"][0]["fills_inserted"] == 9
    assert found["fills"] == 9
    assert found["balances"] == 0, "nothing is written until the venue has answered"
    assert found["account"]["balances_read_at"] is None


async def test_a_sync_with_nothing_new_to_read_still_reads_the_balances(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The clock did not move, so no window is planned and no page is asked for. The account
    still succeeded, and a withdrawal since the last run shows only in the balances."""
    harness = Harness()
    await harness.run(factory, SimulatedVenue(nine_fills(), balances=FIRST_READING))
    venue = SimulatedVenue(nine_fills(), balances=SECOND_READING)

    summary = await harness.run(factory, venue)

    assert venue.calls == []
    assert venue.balance_calls == 1
    assert only(summary).status is AccountOutcomeStatus.SUCCESS
    assert await stored(factory) == SECOND_STORED


# --------------------------------------------------------------------------------------
# 2. No read after a failed or a skipped account
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ExchangeUnavailableError(status=503),
        ExchangeSchemaError("quantity must be greater than zero"),
        ExchangeAuthError(status=401),
        KeyError("tradeId"),
    ],
    ids=["unavailable", "schema", "auth", "internal"],
)
async def test_no_balances_are_read_for_an_account_whose_fill_sync_failed(
    factory: async_sessionmaker[AsyncSession], error: Exception
) -> None:
    """The last good reading is kept exactly as it was, and no error is recorded for it:
    nobody tried to read the balances."""
    harness = Harness()
    await harness.run(factory, SimulatedVenue(nine_fills(), balances=FIRST_READING))
    before = await balance_state(factory)
    harness.clock.advance(QUARTER_HOUR)
    venue = SimulatedVenue(nine_fills(), fault=always(error), balances=SECOND_READING)

    summary = await harness.run(factory, venue, SyncTrigger.MANUAL)

    assert only(summary).status is AccountOutcomeStatus.FAILED
    assert venue.balance_calls == 0
    assert await stored(factory) == FIRST_STORED
    after = await balance_state(factory)
    assert after["balances_read_at"] == before["balances_read_at"] == sqlite_timestamp(T0)
    assert after["balances_error"] is None


async def test_a_failure_on_a_later_page_reads_no_balances_either(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two pages stored, the third failed: a partly synced history is still a stale one."""
    harness = Harness()
    venue = SimulatedVenue(
        nine_fills(),
        fault=lambda number, _window, _cursor: (
            ExchangeUnavailableError(status=503) if number == 3 else None
        ),
        balances=FIRST_READING,
    )

    summary = await harness.run(factory, venue)

    assert only(summary).pages == 2
    assert venue.balance_calls == 0
    assert await stored(factory) == []
    assert (await balance_state(factory))["balances_read_at"] is None


@pytest.mark.parametrize("trigger", [SyncTrigger.SCHEDULED, SyncTrigger.STARTUP])
async def test_no_balances_are_read_for_a_skipped_account(
    factory: async_sessionmaker[AsyncSession], trigger: SyncTrigger
) -> None:
    """An `auth_failed` account is skipped by a timer, and a skipped account is not asked."""
    harness = Harness()
    await harness.run(factory, SimulatedVenue(fault=always(ExchangeAuthError(status=401))))
    harness.clock.advance(QUARTER_HOUR)
    venue = SimulatedVenue(nine_fills(), balances=FIRST_READING)

    summary = await harness.run(factory, venue, trigger)

    assert only(summary).status is AccountOutcomeStatus.SKIPPED
    assert venue.balance_calls == 0
    assert await stored(factory) == []
    state = await balance_state(factory)
    assert (state["balances_read_at"], state["balances_error"]) == (None, None)


async def test_no_balances_are_read_for_an_account_whose_venue_is_not_configured(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    bitget = SimulatedVenue(nine_fills(), balances=FIRST_READING)
    await harness.run(factory, bitget)
    bingx = SimulatedVenue(exchange_key=ExchangeKey.BINGX, balances=(held("KAS", "7"),))

    await harness.run(factory, bingx)

    assert bitget.balance_calls == 1, "the venue that is no longer configured was not asked"
    assert await stored(factory) == [("bingx", "KAS", "7.000000000000000000"), *FIRST_STORED]


# --------------------------------------------------------------------------------------
# 3. A successful read replaces the stored one
# --------------------------------------------------------------------------------------


async def test_a_second_read_replaces_the_first_whole(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """KAS is gone and BGB is new: the rows are the venue's last answer, never a mix of two."""
    harness = Harness()
    await harness.run(factory, SimulatedVenue(nine_fills(), balances=FIRST_READING))
    harness.clock.advance(QUARTER_HOUR)

    await harness.run(factory, SimulatedVenue(nine_fills(), balances=SECOND_READING))

    assert await stored(factory) == SECOND_STORED
    state = await balance_state(factory)
    assert state["balances_read_at"] == sqlite_timestamp(T0 + QUARTER_HOUR)
    assert state["balances_error"] is None


async def test_a_venue_that_holds_nothing_clears_the_reading_and_says_when(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    await harness.run(factory, SimulatedVenue(nine_fills(), balances=FIRST_READING))
    harness.clock.advance(QUARTER_HOUR)

    await harness.run(factory, SimulatedVenue(nine_fills(), balances=()))

    assert await stored(factory) == []
    assert (await balance_state(factory))["balances_read_at"] == sqlite_timestamp(T0 + QUARTER_HOUR)


async def test_a_zero_balance_is_not_stored(factory: async_sessionmaker[AsyncSession]) -> None:
    harness = Harness()
    venue = SimulatedVenue(nine_fills(), balances=(held("BTC", "0.25"), held("ETH", "0")))

    await harness.run(factory, venue)

    assert await stored(factory) == [("bitget", "BTC", "0.250000000000000000")]


async def test_each_venue_has_its_own_reading(factory: async_sessionmaker[AsyncSession]) -> None:
    harness = Harness()
    venues = {
        ExchangeKey.BITGET: SimulatedVenue(nine_fills(), balances=FIRST_READING),
        ExchangeKey.BINGX: SimulatedVenue(
            exchange_key=ExchangeKey.BINGX, balances=(held("BTC", "0.1"), held("kas", "7"))
        ),
    }

    summary = await harness.run(factory, venues)

    assert summary.status is SyncRunStatus.SUCCESS
    assert [venue.balance_calls for venue in venues.values()] == [1, 1]
    assert await stored(factory) == [
        ("bingx", "BTC", "0.100000000000000000"),
        ("bingx", "kas", "7.000000000000000000"),
        *FIRST_STORED,
    ]


# --------------------------------------------------------------------------------------
# 4. A failed read changes nothing else
# --------------------------------------------------------------------------------------

BALANCE_COLUMNS: Final = frozenset({"balances_read_at", "balances_error"})

FAILURES: Final = [
    pytest.param(ExchangeAuthError(status=401), "auth", id="auth"),
    pytest.param(ExchangeInsufficientScopeError(status=403), "insufficient_scope", id="scope"),
    pytest.param(
        ExchangeInvalidRequestError(status=400, venue_code="40017"),
        "invalid_request",
        id="invalid request",
    ),
    pytest.param(
        ExchangeRetentionWindowError(status=400, venue_code="40017"),
        "retention_window",
        id="retention window",
    ),
    pytest.param(ExchangeUnavailableError(status=503), "unavailable", id="unavailable"),
    pytest.param(ExchangeSchemaError("data must be an array of assets"), "schema", id="schema"),
    pytest.param(throttled(61_000), "rate_limited", id="rate limited"),
    pytest.param(RuntimeError("our own defect"), "internal", id="internal"),
    pytest.param(ValueError("the clock reads before the epoch"), "internal", id="value error"),
]


async def whole_state(factory: async_sessionmaker[AsyncSession]) -> dict[str, list[dict[str, Any]]]:
    """Every row the fills side of a sync writes, with the two balance columns left out."""
    full = await rows(factory, "SELECT * FROM exchange_accounts ORDER BY id")
    return {
        "accounts": [
            {name: value for name, value in row.items() if name not in BALANCE_COLUMNS}
            for row in full
        ],
        "runs": await rows(factory, RUNS_SQL),
        "outcomes": await rows(factory, OUTCOMES_SQL),
        "windows": await rows(factory, "SELECT * FROM exchange_sync_windows ORDER BY id"),
    }


@pytest.mark.parametrize(("error", "kind"), FAILURES)
async def test_a_failed_balance_read_leaves_the_outcome_the_status_and_the_run_untouched(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    error: Exception,
    kind: str,
) -> None:
    """The same sync twice over two databases: one whose balance read works, one whose fails.

    Everything the fills side writes -- the account's status and history bounds, the run row
    with its counters, the outcome row with its kind and detail -- must be identical. The
    failure is visible in `balances_error` and nowhere else.
    """
    control = Harness()
    async with migrated_sessionmaker(tmp_path, name="control.db") as control_factory:
        async with control_factory() as session:
            await insert_user(session)
        await control.run(control_factory, SimulatedVenue(nine_fills(), balances=FIRST_READING))
        expected = await whole_state(control_factory)
    harness = Harness()
    venue = SimulatedVenue(
        nine_fills(), balances=FIRST_READING, balance_fault=always_balances(error)
    )

    summary = await harness.run(factory, venue)

    assert venue.balance_calls == 1
    assert await whole_state(factory) == expected
    outcome = only(summary)
    assert outcome.status is AccountOutcomeStatus.SUCCESS
    assert (outcome.error_kind, outcome.detail) == (None, None)
    assert (outcome.pages, outcome.fills_inserted) == (3, 9)
    assert summary.status is SyncRunStatus.SUCCESS
    assert (summary.accounts_succeeded, summary.accounts_failed) == (1, 0)
    (account,) = await rows(factory, ACCOUNTS_SQL)
    assert account["sync_status"] == "ok"
    assert account["last_synced_at"] == sqlite_timestamp(T0)
    state = await balance_state(factory)
    assert state["balances_error"] == kind
    assert state["balances_read_at"] is None
    assert await stored(factory) == []
    assert sorted(await trade_ids(factory)) == ALL_NINE


@pytest.mark.parametrize(("error", "kind"), FAILURES)
async def test_a_failed_balance_read_keeps_the_last_good_reading_and_when_it_was_read(
    factory: async_sessionmaker[AsyncSession], error: Exception, kind: str
) -> None:
    """The storage half of the rule: the rows and `balances_read_at` are not touched.

    Whether a kept reading is still *compared* is the reconciliation's decision, and since
    R9 it is not: `tests/services/test_reconciliation_service.py` holds that half. What is
    kept here is what lets the dashboard say when the venue was last read.
    """
    harness = Harness()
    await harness.run(factory, SimulatedVenue(nine_fills(), balances=FIRST_READING))
    harness.clock.advance(QUARTER_HOUR)
    venue = SimulatedVenue(
        nine_fills(), balances=SECOND_READING, balance_fault=always_balances(error)
    )

    summary = await harness.run(factory, venue)

    assert only(summary).status is AccountOutcomeStatus.SUCCESS
    assert await stored(factory) == FIRST_STORED
    state = await balance_state(factory)
    assert state["balances_read_at"] == sqlite_timestamp(T0), "the failure did not move it"
    assert state["balances_error"] == kind
    assert state["sync_status"] == "ok"


async def test_a_reading_the_table_refuses_is_rolled_back_whole(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Our own failure, *after* the old rows were deleted and before the commit.

    The answer names an asset twice, which the unique constraint refuses at the insert. The
    delete that ran first must be rolled back with it: the failure is then committed on its
    own, and without the rollback that commit would carry the delete and the last good
    reading would be gone.
    """
    harness = Harness()
    await harness.run(factory, SimulatedVenue(nine_fills(), balances=FIRST_READING))
    harness.clock.advance(QUARTER_HOUR)
    venue = UnassembledVenue(nine_fills())
    venue.raw = (RawBalance(MARKED_ASSET, MARKED_AMOUNT), RawBalance(MARKED_ASSET, "1"))

    with capture_logs() as captured:
        summary = await harness.run(factory, venue)

    assert venue.balance_calls == 1
    assert only(summary).status is AccountOutcomeStatus.SUCCESS
    assert summary.status is SyncRunStatus.SUCCESS
    assert await stored(factory) == FIRST_STORED, "the delete was committed with the failure"
    state = await balance_state(factory)
    assert state["balances_read_at"] == sqlite_timestamp(T0)
    assert state["balances_error"] == "internal"
    (failed,) = events_named(captured, "exchange_balances_read_failed")
    assert failed["error_type"] == "IntegrityError"


async def test_a_commit_that_fails_while_storing_the_reading_keeps_the_last_one(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The disk refuses the commit that carries the new reading: the old one stands."""
    harness = Harness()
    await harness.run(factory, SimulatedVenue(nine_fills(), balances=FIRST_READING))
    harness.clock.advance(QUARTER_HOUR)
    venue = SimulatedVenue(nine_fills(), balances=SECOND_READING)
    armed = [True]

    def fail_the_reading(_session: object) -> None:
        if armed[0] and venue.balance_calls == 1:
            armed[0] = False
            message = "disk I/O error"
            raise OSError(message)

    def install(session: AsyncSession) -> None:
        event.listen(session.sync_session, "before_commit", fail_the_reading)

    summary = await harness.run(factory, venue, on_session=install)

    assert armed == [False], "the hook never fired, so this proves nothing"
    assert only(summary).status is AccountOutcomeStatus.SUCCESS
    assert await stored(factory) == FIRST_STORED
    state = await balance_state(factory)
    assert state["balances_read_at"] == sqlite_timestamp(T0)
    assert state["balances_error"] == "internal"
    assert state["sync_status"] == "ok"


async def test_one_venues_failed_balance_read_does_not_stop_the_next_venue(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """BingX is synced first, by key. Its balance read fails; Bitget is still synced and read,
    and the run is a success: nothing about the fills failed."""
    harness = Harness()
    venues = {
        ExchangeKey.BINGX: SimulatedVenue(
            exchange_key=ExchangeKey.BINGX,
            balances=(held("KAS", "7"),),
            balance_fault=always_balances(ExchangeUnavailableError(status=503)),
        ),
        ExchangeKey.BITGET: SimulatedVenue(nine_fills(), balances=FIRST_READING),
    }

    summary = await harness.run(factory, venues)

    assert summary.status is SyncRunStatus.SUCCESS
    assert (summary.accounts_succeeded, summary.accounts_failed) == (2, 0)
    assert [outcome.status for outcome in summary.accounts] == [AccountOutcomeStatus.SUCCESS] * 2
    assert await stored(factory) == FIRST_STORED
    assert (await balance_state(factory, "bingx"))["balances_error"] == "unavailable"
    assert (await balance_state(factory, "bitget"))["balances_error"] is None
    assert sorted(await trade_ids(factory)) == ALL_NINE


async def test_a_success_after_a_failure_clears_the_error(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    await harness.run(
        factory,
        SimulatedVenue(
            nine_fills(),
            balances=FIRST_READING,
            balance_fault=always_balances(ExchangeUnavailableError(status=503)),
        ),
    )
    assert (await balance_state(factory))["balances_error"] == "unavailable"
    harness.clock.advance(QUARTER_HOUR)

    await harness.run(factory, SimulatedVenue(nine_fills(), balances=FIRST_READING))

    state = await balance_state(factory)
    assert state["balances_error"] is None
    assert state["balances_read_at"] == sqlite_timestamp(T0 + QUARTER_HOUR)
    assert await stored(factory) == FIRST_STORED


async def test_the_process_dying_during_the_balance_read_loses_no_fills_and_no_reading(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Not an `Exception`: nothing is recorded, the run row stays `running`, and what was
    committed before the read -- the fills, the outcome, the last reading -- is what remains."""
    harness = Harness()
    await harness.run(factory, SimulatedVenue(nine_fills(), balances=FIRST_READING))
    harness.clock.advance(QUARTER_HOUR)
    venue = SimulatedVenue(
        nine_fills(),
        balances=SECOND_READING,
        balance_fault=always_balances(SimulatedPowerLoss()),
    )

    with pytest.raises(SimulatedPowerLoss):
        await harness.run(factory, venue)

    assert await stored(factory) == FIRST_STORED
    state = await balance_state(factory)
    assert state["balances_read_at"] == sqlite_timestamp(T0)
    assert state["balances_error"] is None, "a death is not a recorded failure"
    runs = await rows(factory, RUNS_SQL)
    assert [run["status"] for run in runs] == ["success", "running"]
    outcomes = await rows(factory, OUTCOMES_SQL)
    assert [row["status"] for row in outcomes] == ["success", "success"]


# --------------------------------------------------------------------------------------
# 8. R5: a failure that cannot even be recorded does not escape either
# --------------------------------------------------------------------------------------


def two_venues_the_first_failing() -> dict[ExchangeKey, SimulatedVenue]:
    """BingX sorts first and its balance read fails; Bitget follows with nine fills."""
    return {
        ExchangeKey.BINGX: SimulatedVenue(
            exchange_key=ExchangeKey.BINGX,
            balances=(held(MARKED_ASSET, MARKED_AMOUNT),),
            balance_fault=always_balances(ExchangeUnavailableError(status=503)),
        ),
        ExchangeKey.BITGET: SimulatedVenue(nine_fills(), balances=FIRST_READING),
    }


async def assert_the_run_went_on(
    factory: async_sessionmaker[AsyncSession],
    venues: dict[ExchangeKey, SimulatedVenue],
    summary: Any,
    captured: Sequence[Any],
    *,
    error_type: str,
) -> None:
    assert summary.status is SyncRunStatus.SUCCESS
    assert [outcome.status for outcome in summary.accounts] == [AccountOutcomeStatus.SUCCESS] * 2
    assert all(outcome.error_kind is None for outcome in summary.accounts)
    assert venues[ExchangeKey.BITGET].balance_calls == 1, "the next account was still read"
    assert await stored(factory) == FIRST_STORED
    assert sorted(await trade_ids(factory)) == ALL_NINE
    (run,) = await rows(factory, RUNS_SQL)
    assert run["status"] == "success", "the run row was left `running`"
    assert run["finished_at"] is not None
    assert (run["accounts_succeeded"], run["accounts_failed"]) == (2, 0)
    bingx = await balance_state(factory, "bingx")
    assert bingx["balances_error"] is None, "nothing could be recorded, so nothing was"
    assert bingx["balances_read_at"] is None
    assert bingx["sync_status"] == "ok"
    assert [(entry["event"], entry["exchange_key"]) for entry in balance_events(captured)] == [
        ("exchange_balances_read_failed", "bingx"),
        ("exchange_balances_failure_not_recorded", "bingx"),
        ("exchange_balances_read", "bitget"),
    ]
    assert events_named(captured, "exchange_balances_failure_not_recorded") == [
        {
            "event": "exchange_balances_failure_not_recorded",
            "log_level": "error",
            "exc_info": True,
            "exchange_key": "bingx",
            "error_type": error_type,
        }
    ]
    everything = repr(captured)
    assert MARKED_ASSET not in everything
    assert "4242" not in everything


async def test_a_failure_that_cannot_be_recorded_does_not_stop_the_next_account_or_the_run(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write that marks the read as failed raises. Every fill is already imported and
    the outcome committed, so one ancillary write may not leave the run `running`."""

    async def refuse(self: object, account_id: int, kind: object) -> None:
        del self, account_id, kind
        message = "database is locked"
        raise RuntimeError(message)

    monkeypatch.setattr(ExchangeBalanceRepository, "record_failure", refuse)
    harness = Harness()
    venues = two_venues_the_first_failing()

    with capture_logs() as captured:
        summary = await harness.run(factory, venues)

    await assert_the_run_went_on(factory, venues, summary, captured, error_type="RuntimeError")


async def test_a_commit_that_fails_while_recording_the_failure_is_rolled_back_and_logged(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The commit carrying `balances_error` fails; the commit that closes the run does not.

    The first commit after BingX's balance read was asked for is the one that records its
    failure. It is rolled back, so nothing half-written rides along with Bitget's commits.
    """
    harness = Harness()
    venues = two_venues_the_first_failing()
    bingx = venues[ExchangeKey.BINGX]
    armed = [True]

    def fail_the_record(_session: object) -> None:
        if armed[0] and bingx.balance_calls == 1:
            armed[0] = False
            message = "disk I/O error"
            raise OSError(message)

    def install(session: AsyncSession) -> None:
        event.listen(session.sync_session, "before_commit", fail_the_record)

    with capture_logs() as captured:
        summary = await harness.run(factory, venues, on_session=install)

    assert armed == [False], "the hook never fired, so this proves nothing"
    await assert_the_run_went_on(factory, venues, summary, captured, error_type="OSError")


async def test_the_process_dying_while_recording_the_failure_still_propagates(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an `Exception` is swallowed. A death leaves the run `running`, as everywhere."""

    async def die(self: object, account_id: int, kind: object) -> None:
        del self, account_id, kind
        raise SimulatedPowerLoss

    monkeypatch.setattr(ExchangeBalanceRepository, "record_failure", die)
    harness = Harness()
    venues = two_venues_the_first_failing()

    with pytest.raises(SimulatedPowerLoss):
        await harness.run(factory, venues)

    assert venues[ExchangeKey.BITGET].balance_calls == 0
    (run,) = await rows(factory, RUNS_SQL)
    assert run["status"] == "running"


# --------------------------------------------------------------------------------------
# 5. `auth` and `insufficient_scope` are retried only by a manual sync
# --------------------------------------------------------------------------------------

KEY_REFUSALS: Final = [
    pytest.param(ExchangeAuthError(status=401), "auth", id="auth"),
    pytest.param(ExchangeInsufficientScopeError(status=403), "insufficient_scope", id="scope"),
]


async def refuse_the_key(
    harness: Harness, factory: async_sessionmaker[AsyncSession], error: Exception
) -> None:
    """A first run whose fills succeed and whose balance read is refused for the key."""
    await harness.run(
        factory,
        SimulatedVenue(nine_fills(), balances=FIRST_READING, balance_fault=always_balances(error)),
    )
    harness.clock.advance(QUARTER_HOUR)


@pytest.mark.parametrize(("error", "kind"), KEY_REFUSALS)
@pytest.mark.parametrize("trigger", [SyncTrigger.SCHEDULED, SyncTrigger.STARTUP])
async def test_a_timer_does_not_ask_again_for_balances_the_venue_refused_the_key_for(
    factory: async_sessionmaker[AsyncSession],
    error: Exception,
    kind: str,
    trigger: SyncTrigger,
) -> None:
    """Asking a venue to refuse the same key every fifteen minutes is how an IP gets banned.

    The fills are still synced -- the key reads them fine -- and only the balance read is
    left out. The kind stays, so the holdings check keeps saying why the venue is missing.
    """
    harness = Harness()
    await refuse_the_key(harness, factory, error)
    venue = SimulatedVenue(nine_fills(), balances=FIRST_READING)

    with capture_logs() as captured:
        summary = await harness.run(factory, venue, trigger)

    assert venue.balance_calls == 0
    assert only(summary).status is AccountOutcomeStatus.SUCCESS, "the fills are still synced"
    assert venue.calls, "the new quarter of an hour of fills was still read"
    state = await balance_state(factory)
    assert state["balances_error"] == kind
    assert state["balances_read_at"] is None
    assert state["sync_status"] == "ok"
    assert await stored(factory) == []
    assert events_named(captured, "exchange_balances_read_skipped") == [
        {
            "event": "exchange_balances_read_skipped",
            "log_level": "info",
            "exchange_key": "bitget",
            "reason": kind,
        }
    ]
    assert events_named(captured, "exchange_balances_read") == []
    assert events_named(captured, "exchange_balances_read_failed") == []


@pytest.mark.parametrize(("error", "kind"), KEY_REFUSALS)
async def test_a_manual_sync_asks_again_and_a_success_clears_the_refusal(
    factory: async_sessionmaker[AsyncSession], error: Exception, kind: str
) -> None:
    """The owner fixed the key's permissions and pressed sync."""
    harness = Harness()
    await refuse_the_key(harness, factory, error)
    assert (await balance_state(factory))["balances_error"] == kind
    venue = SimulatedVenue(nine_fills(), balances=FIRST_READING)

    await harness.run(factory, venue, SyncTrigger.MANUAL)

    assert venue.balance_calls == 1
    state = await balance_state(factory)
    assert state["balances_error"] is None
    assert state["balances_read_at"] == sqlite_timestamp(T0 + QUARTER_HOUR)
    assert await stored(factory) == FIRST_STORED


@pytest.mark.parametrize(("error", "kind"), KEY_REFUSALS)
async def test_a_manual_sync_that_is_refused_again_keeps_the_kind_and_timers_keep_skipping(
    factory: async_sessionmaker[AsyncSession], error: Exception, kind: str
) -> None:
    harness = Harness()
    await refuse_the_key(harness, factory, error)
    manual = SimulatedVenue(
        nine_fills(), balances=FIRST_READING, balance_fault=always_balances(error)
    )

    await harness.run(factory, manual, SyncTrigger.MANUAL)
    harness.clock.advance(QUARTER_HOUR)
    scheduled = SimulatedVenue(nine_fills(), balances=FIRST_READING)
    await harness.run(factory, scheduled, SyncTrigger.SCHEDULED)

    assert manual.balance_calls == 1
    assert scheduled.balance_calls == 0
    assert (await balance_state(factory))["balances_error"] == kind


async def test_after_a_manual_sync_clears_the_refusal_timers_read_again(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    await refuse_the_key(harness, factory, ExchangeAuthError(status=401))
    await harness.run(
        factory, SimulatedVenue(nine_fills(), balances=FIRST_READING), SyncTrigger.MANUAL
    )
    harness.clock.advance(QUARTER_HOUR)
    venue = SimulatedVenue(nine_fills(), balances=SECOND_READING)

    await harness.run(factory, venue, SyncTrigger.SCHEDULED)

    assert venue.balance_calls == 1
    assert await stored(factory) == SECOND_STORED


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        pytest.param(ExchangeUnavailableError(status=503), "unavailable", id="unavailable"),
        pytest.param(ExchangeSchemaError("data must be an array of assets"), "schema", id="schema"),
        pytest.param(throttled(61_000), "rate_limited", id="rate limited"),
        pytest.param(
            ExchangeInvalidRequestError(status=400, venue_code="40017"),
            "invalid_request",
            id="invalid request",
        ),
        pytest.param(RuntimeError("our own defect"), "internal", id="internal"),
    ],
)
@pytest.mark.parametrize("trigger", [SyncTrigger.SCHEDULED, SyncTrigger.STARTUP])
async def test_every_other_kind_is_asked_again_by_a_timer(
    factory: async_sessionmaker[AsyncSession],
    error: Exception,
    kind: str,
    trigger: SyncTrigger,
) -> None:
    """Only a refused key needs a person. An outage or a surprise in the answer may be gone
    by the next interval, and skipping it would leave the venue out of the check for good."""
    harness = Harness()
    await refuse_the_key(harness, factory, error)
    assert (await balance_state(factory))["balances_error"] == kind
    venue = SimulatedVenue(nine_fills(), balances=FIRST_READING)

    await harness.run(factory, venue, trigger)

    assert venue.balance_calls == 1
    assert (await balance_state(factory))["balances_error"] is None
    assert await stored(factory) == FIRST_STORED


async def test_the_skip_reads_the_kind_the_previous_run_left_not_this_runs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A first refusal is itself an attempt: the run that discovers it has asked once."""
    harness = Harness()
    venue = SimulatedVenue(
        nine_fills(),
        balances=FIRST_READING,
        balance_fault=always_balances(ExchangeAuthError(status=401)),
    )

    await harness.run(factory, venue, SyncTrigger.SCHEDULED)

    assert venue.balance_calls == 1
    assert harness.sleeper.slept == [], "a refused key is not retried within the run"


# --------------------------------------------------------------------------------------
# 6. The rate-limit retry, and no write transaction while the venue answers
# --------------------------------------------------------------------------------------


async def test_a_rate_limited_balance_read_is_retried_and_stored(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """1.5 s from the venue is slept as 2 whole seconds, then the read is asked again."""
    harness = Harness()
    venue = SimulatedVenue(
        nine_fills(),
        balances=FIRST_READING,
        balance_fault=balance_faults_on({1: throttled(1500)}),
    )

    await harness.run(factory, venue)

    assert harness.sleeper.slept == [2]
    assert venue.balance_calls == 2
    assert await stored(factory) == FIRST_STORED
    assert (await balance_state(factory))["balances_error"] is None


async def test_three_retries_are_allowed_and_the_fourth_attempt_can_succeed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    venue = SimulatedVenue(
        nine_fills(),
        balances=FIRST_READING,
        balance_fault=balance_faults_on(dict.fromkeys((1, 2, 3), throttled(1000))),
    )

    await harness.run(factory, venue)

    assert harness.sleeper.slept == [1, 1, 1]
    assert venue.balance_calls == 4
    assert await stored(factory) == FIRST_STORED


async def test_exhausted_retries_record_rate_limited_and_the_fills_still_succeeded(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    venue = SimulatedVenue(
        nine_fills(), balances=FIRST_READING, balance_fault=always_balances(throttled(1000))
    )

    summary = await harness.run(factory, venue)

    assert venue.balance_calls == 4
    assert harness.sleeper.slept == [1, 1, 1]
    assert only(summary).status is AccountOutcomeStatus.SUCCESS
    assert (await balance_state(factory))["balances_error"] == "rate_limited"
    assert await stored(factory) == []


async def test_without_a_retry_after_the_waits_double(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    venue = SimulatedVenue(
        nine_fills(),
        balances=FIRST_READING,
        balance_fault=always_balances(
            ExchangeRateLimitedError(status=429, venue_code="429", retry_after_ms=None)
        ),
    )

    await harness.run(factory, venue)

    assert harness.sleeper.slept == [2, 4, 8]


async def test_a_wait_beyond_the_cap_is_not_slept(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    venue = SimulatedVenue(
        nine_fills(), balances=FIRST_READING, balance_fault=always_balances(throttled(61_000))
    )

    await harness.run(factory, venue)

    assert harness.sleeper.slept == []
    assert venue.balance_calls == 1
    assert (await balance_state(factory))["balances_error"] == "rate_limited"


async def test_no_write_transaction_is_open_while_the_venue_is_asked_for_balances(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A network call, and SQLite has one writer: nothing may wait behind it.

    The success outcome is a write, made just before the read. If it were still uncommitted
    every other writer -- a login, a wallet sync -- would wait on the venue's latency, and
    past the busy timeout it would fail. A second connection taking the write lock from
    inside `fetch_balances` is the proof.
    """
    database = tmp_path / "test.db"
    harness = Harness()
    seen: list[str | None] = []
    venue = SimulatedVenue(nine_fills(), balances=FIRST_READING)
    venue.on_balances = a_writer_can_begin(database, seen)

    await harness.run(factory, venue)

    assert seen == [None], "a write transaction was open while the venue was being asked"
    assert await stored(factory) == FIRST_STORED


async def test_no_write_transaction_is_open_while_a_rate_limited_balance_read_waits(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Both attempts, with a stored reading to replace: the old rows are not deleted, and no
    lock is taken, until the venue has answered."""
    database = tmp_path / "test.db"
    harness = Harness()
    await harness.run(factory, SimulatedVenue(nine_fills(), balances=FIRST_READING))
    harness.clock.advance(QUARTER_HOUR)
    seen: list[str | None] = []
    venue = SimulatedVenue(
        nine_fills(),
        balances=SECOND_READING,
        balance_fault=balance_faults_on({1: throttled(1000)}),
    )
    venue.on_balances = a_writer_can_begin(database, seen)

    await harness.run(factory, venue)

    assert seen == [None, None]
    assert harness.sleeper.slept == [1]
    assert await stored(factory) == SECOND_STORED


async def test_one_venues_reading_is_committed_before_the_next_venue_is_asked_anything(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """BingX is synced first and its reading stored. If that write were left open, every
    request to Bitget -- three pages and a balance read -- would be made inside it."""
    database = tmp_path / "test.db"
    harness = Harness()
    seen: list[str | None] = []
    writer_can_begin = a_writer_can_begin(database, seen)
    bitget = SimulatedVenue(nine_fills(), balances=FIRST_READING)

    async def on_page(_call: object) -> None:
        writer_can_begin()

    bitget.on_call = on_page
    bitget.on_balances = writer_can_begin
    venues = {
        ExchangeKey.BINGX: SimulatedVenue(
            exchange_key=ExchangeKey.BINGX, balances=(held("KAS", "7"),)
        ),
        ExchangeKey.BITGET: bitget,
    }

    await harness.run(factory, venues)

    assert seen == [None, None, None, None], "BingX's reading was still uncommitted"
    assert await stored(factory) == [("bingx", "KAS", "7.000000000000000000"), *FIRST_STORED]


async def test_one_venues_recorded_failure_is_committed_before_the_next_venue_is_asked(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The same, for the write that records a failed read."""
    database = tmp_path / "test.db"
    harness = Harness()
    seen: list[str | None] = []
    writer_can_begin = a_writer_can_begin(database, seen)
    bitget = SimulatedVenue(nine_fills(), balances=FIRST_READING)

    async def on_page(_call: object) -> None:
        writer_can_begin()

    bitget.on_call = on_page
    bitget.on_balances = writer_can_begin
    venues = {
        ExchangeKey.BINGX: SimulatedVenue(
            exchange_key=ExchangeKey.BINGX,
            balances=(held("KAS", "7"),),
            balance_fault=always_balances(ExchangeUnavailableError(status=503)),
        ),
        ExchangeKey.BITGET: bitget,
    }

    await harness.run(factory, venues)

    assert seen == [None, None, None, None]
    assert (await balance_state(factory, "bingx"))["balances_error"] == "unavailable"


async def test_no_write_transaction_is_open_while_a_failing_balance_read_is_asked(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    database = tmp_path / "test.db"
    harness = Harness()
    seen: list[str | None] = []
    venue = SimulatedVenue(
        nine_fills(),
        balances=FIRST_READING,
        balance_fault=always_balances(ExchangeUnavailableError(status=503)),
    )
    venue.on_balances = a_writer_can_begin(database, seen)

    await harness.run(factory, venue)

    assert seen == [None]


# --------------------------------------------------------------------------------------
# 7. What is logged
# --------------------------------------------------------------------------------------


async def test_a_successful_read_logs_the_venue_and_a_count_and_nothing_else(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    venue = SimulatedVenue(
        nine_fills(),
        balances=(held(MARKED_ASSET, MARKED_AMOUNT), held("BTC", "0.25"), held("ETH", "0")),
    )

    with capture_logs() as captured:
        await harness.run(factory, venue)

    assert events_named(captured, "exchange_balances_read") == [
        {
            "event": "exchange_balances_read",
            "log_level": "info",
            "exchange_key": "bitget",
            "assets": 2,
        }
    ]
    assert events_named(captured, "exchange_balances_read_failed") == []
    everything = repr(captured)
    assert MARKED_ASSET not in everything
    assert "4242" not in everything


@pytest.mark.parametrize(
    ("error", "kind", "type_name"),
    [
        pytest.param(ExchangeAuthError(status=401), "auth", "ExchangeAuthError", id="auth"),
        pytest.param(
            ExchangeInsufficientScopeError(status=403),
            "insufficient_scope",
            "ExchangeInsufficientScopeError",
            id="scope",
        ),
        pytest.param(
            ExchangeUnavailableError(status=503),
            "unavailable",
            "ExchangeUnavailableError",
            id="unavailable",
        ),
        pytest.param(
            ExchangeSchemaError("data must be an array of assets"),
            "schema",
            "ExchangeSchemaError",
            id="schema",
        ),
        pytest.param(throttled(61_000), "rate_limited", "ExchangeRateLimitedError", id="throttled"),
    ],
)
async def test_a_failed_read_logs_a_warning_with_the_kind_and_the_type(
    factory: async_sessionmaker[AsyncSession], error: Exception, kind: str, type_name: str
) -> None:
    """Exactly four fields, none of them the exception's message or a traceback."""
    harness = Harness()
    venue = SimulatedVenue(
        nine_fills(),
        balances=(held(MARKED_ASSET, MARKED_AMOUNT),),
        balance_fault=always_balances(error),
    )

    with capture_logs() as captured:
        await harness.run(factory, venue)

    assert events_named(captured, "exchange_balances_read_failed") == [
        {
            "event": "exchange_balances_read_failed",
            "log_level": "warning",
            "exchange_key": "bitget",
            "error_kind": kind,
            "error_type": type_name,
        }
    ]
    assert events_named(captured, "exchange_balances_read") == []
    everything = repr(captured)
    assert MARKED_ASSET not in everything
    assert "4242" not in everything


async def test_an_internal_failure_logs_the_type_and_the_traceback(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Our bug, not the venue's: `internal`, at error level, with the traceback."""
    harness = Harness()
    venue = SimulatedVenue(
        nine_fills(),
        balances=FIRST_READING,
        balance_fault=always_balances(KeyError("free")),
    )

    with capture_logs() as captured:
        await harness.run(factory, venue)

    (logged,) = events_named(captured, "exchange_balances_read_failed")
    assert logged["log_level"] == "error"
    assert logged.get("exc_info"), "the traceback is logged"
    assert logged["exchange_key"] == "bitget"
    assert logged["error_kind"] == "internal"
    assert logged["error_type"] == "KeyError"
    assert set(logged) == {
        "event",
        "log_level",
        "exc_info",
        "exchange_key",
        "error_kind",
        "error_type",
    }


async def test_a_duplicate_asset_in_the_answer_is_a_schema_failure_that_names_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`assemble_balances` refuses it, and what is logged and stored is the kind alone."""
    harness = Harness()
    venue = SimulatedVenue(
        nine_fills(),
        balances=(held(MARKED_ASSET, MARKED_AMOUNT), held(MARKED_ASSET, "1")),
    )

    with capture_logs() as captured:
        summary = await harness.run(factory, venue)

    assert only(summary).status is AccountOutcomeStatus.SUCCESS
    assert (await balance_state(factory))["balances_error"] == "schema"
    assert await stored(factory) == []
    everything = repr(captured)
    assert MARKED_ASSET not in everything
    assert "4242" not in everything
    full = await rows(factory, "SELECT * FROM exchange_accounts")
    outcomes = await rows(factory, "SELECT * FROM exchange_sync_run_accounts")
    assert MARKED_ASSET not in repr(full) + repr(outcomes)
    assert "4242" not in repr(full) + repr(outcomes)


async def test_no_balance_event_is_logged_when_no_balance_read_is_made(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    harness = Harness()
    venue = SimulatedVenue(
        nine_fills(), fault=always(ExchangeUnavailableError(status=503)), balances=FIRST_READING
    )

    with capture_logs() as captured:
        await harness.run(factory, venue)

    assert balance_events(captured) == []
