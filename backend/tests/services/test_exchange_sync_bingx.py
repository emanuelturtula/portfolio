"""Spec 017, R6: the real BingX provider through the real exchange sync, end to end.

`test_exchange_sync.py` drives `ExchangeSyncService` against `SimulatedVenue`, which pages by
trade id. BingX pages by **time**, and its cursor re-reads the newest millisecond of every
full page on purpose: more fills may share it, so the next request starts **at** it, not after
it. That design leans on #15's unique constraint to make the second read insert nothing, and
on the repository's conflict check to find the second read identical to the first. Neither
had been exercised with a real time cursor. Here it is:

* `BingXProvider`, signing every request, over `bingx_harness.FakeBingX` -- the fake that
  behaves as the owner's probe found the venue behaving, and verifies every signature;
* the real `ExchangeSyncService` and repositories, on a migrated SQLite **file** under
  `tmp_path`, read back over a second session so what is asserted was committed.

## The window, worked out by hand

The clock stands at `T0`, 2026-09-25 12:00 UTC, and the history start is that date, so the
sync plans one window, `[00:00, 12:00)`: `startTime` 1790294400000 (`date -u -d
2026-09-25T00:00:00Z +%s` is 1790294400) and `endTime` 1790337599999. 1,200 fills, one every
thirty seconds from 00:00:30, alternate ETH-USDT and BTC-USDT with overlapping ids. The
venue serves 500 at a time, ascending, both bounds inclusive:

| Page | `startTime` | Fills | Newest fill |
|---|---|---|---|
| 1 | 1790294400000 | 0..499 | 499, at 00:00:30 + 499 x 30 s = 04:10:00, 1790309400000 |
| 2 | 1790309400000 | 499..998 | 998, at 08:19:30, 1790324370000 |
| 3 | 1790324370000 | 998..1199 | 202 fills: short, the window is done |

So 1,202 fills are seen and 1,200 inserted: fills 499 and 998 are each read twice.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import text

from portfolio.domain.exchanges import ExchangeKey
from portfolio.repositories.exchange_sync_runs import (
    AccountOutcomeStatus,
    SyncRunStatus,
    SyncTrigger,
)
from portfolio.services.exchange_sync import build_exchange_sync_service
from tests.balance_harness import insert_user
from tests.exchange_sync_harness import (
    ACCOUNTS_SQL,
    FILLS_SQL,
    OUTCOMES_SQL,
    WINDOWS_SQL,
    RecordingSleeper,
    SettableClock,
    TickingMonotonic,
    rows,
    sqlite_timestamp,
)
from tests.providers.exchanges.bingx_harness import (
    FakeBingX,
    FixedClock,
    VenueFill,
    bingx_client,
    bingx_provider,
    spread_fills,
)
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.repositories.exchange_sync_runs import ExchangeSyncRunSummary

T0: Final = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
TODAY: Final = date(2026, 9, 25)
MIDNIGHT_MS: Final = 1790294400000
END_TIME_MS: Final = MIDNIGHT_MS + 12 * 3_600_000 - 1
FIRST_CURSOR_MS: Final = 1790309400000
SECOND_CURSOR_MS: Final = 1790324370000


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        async with built() as session:
            await insert_user(session)
        yield built


async def sync_bingx(
    factory: async_sessionmaker[AsyncSession], fake: FakeBingX
) -> ExchangeSyncRunSummary:
    """One scheduled run of the real service, with the real BingX provider on the fake."""
    async with bingx_client(fake) as client, factory() as session:
        provider = bingx_provider(client, clock=FixedClock(T0))
        service = build_exchange_sync_service(
            session,
            providers={ExchangeKey.BINGX: provider},
            clock=SettableClock(T0),
            monotonic=TickingMonotonic(),
            sleep=RecordingSleeper(),
            history_start=TODAY,
        )
        return await service.sync(SyncTrigger.SCHEDULED)


def expected_ids(fills: Sequence[VenueFill]) -> list[str]:
    return sorted(f"{fill.symbol}:{fill.trade_id}" for fill in fills)


async def stored_ids(factory: async_sessionmaker[AsyncSession]) -> list[str]:
    return [str(row["external_trade_id"]) for row in await rows(factory, FILLS_SQL)]


def test_the_hand_worked_instants_are_right() -> None:
    """The premises of the table above, from the harness's own arithmetic."""
    fills = spread_fills(1200, first_ms=MIDNIGHT_MS + 30_000, step_ms=30_000)

    assert fills[499].executed_ms == FIRST_CURSOR_MS
    assert fills[998].executed_ms == SECOND_CURSOR_MS
    assert fills[-1].executed_ms < END_TIME_MS


async def test_more_than_a_page_imports_every_fill_exactly_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three pages, 1,202 fills seen, 1,200 inserted, no conflict, nothing left queued."""
    fills = spread_fills(1200, first_ms=MIDNIGHT_MS + 30_000, step_ms=30_000)
    fake = FakeBingX(fills)

    summary = await sync_bingx(factory, fake)

    assert summary.status is SyncRunStatus.SUCCESS
    (outcome,) = summary.accounts
    assert outcome.status is AccountOutcomeStatus.SUCCESS
    assert outcome.error_kind is None, "the overlap's second read raised a conflict"
    assert (outcome.windows_completed, outcome.pages) == (1, 3)
    # `fills_seen` counts the double read of the two overlap milliseconds; `fills_inserted`
    # counts rows. Spec 017, "Accepted, not changed".
    assert (outcome.fills_seen, outcome.fills_inserted) == (1202, 1200)

    stored = await stored_ids(factory)
    assert len(stored) == len(set(stored)) == 1200, "a fill is stored twice or missing"
    assert sorted(stored) == expected_ids(fills)
    assert "ETH-USDT:36767057" in stored
    assert "BTC-USDT:36767057" in stored, "overlapping ids across symbols both stored"
    assert await rows(factory, WINDOWS_SQL) == [], "the window was not completed"

    assert [params["startTime"] for params in fake.params()] == [
        str(MIDNIGHT_MS),
        str(FIRST_CURSOR_MS),
        str(SECOND_CURSOR_MS),
    ]
    assert {params["endTime"] for params in fake.params()} == {str(END_TIME_MS)}
    assert fake.signature_failures == []
    assert [len(served) for served in fake.served] == [500, 500, 202]

    (recorded,) = await rows(factory, OUTCOMES_SQL)
    assert recorded["exchange_key"] == "bingx"
    assert (recorded["fills_seen"], recorded["fills_inserted"]) == (1202, 1200)
    assert recorded["error_kind"] is None


async def test_every_fill_sharing_the_overlap_millisecond_is_stored_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three fills in the millisecond where page 1 ends, only two of which fit on it.

    498 fills a minute apart from 00:01, then three at 08:19 (1790294400000 + 499 x 60000 =
    1790324340000), then ten more. Page 1 holds the 498 and two of the three; page 2 starts
    **at** 08:19 and holds all three and the ten. The two read twice insert nothing; the
    third, read only on page 2, is stored.
    """
    boundary_ms = MIDNIGHT_MS + 499 * 60_000
    boundary = [
        VenueFill(trade_id=41_000_001, executed_ms=boundary_ms, symbol="ETH-USDT"),
        VenueFill(trade_id=41_000_001, executed_ms=boundary_ms, symbol="BTC-USDT"),
        VenueFill(trade_id=41_000_002, executed_ms=boundary_ms, symbol="ETH-USDT"),
    ]
    fills = [
        *spread_fills(498, first_ms=MIDNIGHT_MS + 60_000),
        *boundary,
        *spread_fills(10, first_ms=boundary_ms + 60_000, first_id=42_000_000),
    ]
    fake = FakeBingX(fills)

    summary = await sync_bingx(factory, fake)

    (outcome,) = summary.accounts
    assert outcome.status is AccountOutcomeStatus.SUCCESS
    assert outcome.error_kind is None
    assert (outcome.pages, outcome.fills_seen, outcome.fills_inserted) == (2, 513, 511)
    assert "ETH-USDT:41000002" not in {
        f"{fill.symbol}:{fill.trade_id}" for fill in fake.served[0]
    }, "the premise: the third fill did not fit on page 1"
    stored = await stored_ids(factory)
    assert len(stored) == len(set(stored)) == 511
    assert sorted(stored) == expected_ids(fills)


async def test_a_second_read_of_the_whole_window_inserts_nothing_and_conflicts_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The same 1,200 fills read again in a later run: each meets its stored twin, is found
    identical by the conflict check, and inserts nothing.

    The window is queued again by hand, which is what a moved history start or #15's overlap
    does on a smaller scale. 1,202 fills are seen again, 0 inserted, and nothing conflicts.
    """
    fills = spread_fills(1200, first_ms=MIDNIGHT_MS + 30_000, step_ms=30_000)
    await sync_bingx(factory, FakeBingX(fills))
    (account,) = await rows(factory, ACCOUNTS_SQL)
    async with factory() as session:
        await session.execute(
            text(
                'INSERT INTO exchange_sync_windows (exchange_account_id, "since", "until") '
                "VALUES (:account, :since, :until)"
            ),
            {
                "account": account["id"],
                "since": sqlite_timestamp(datetime(2026, 9, 25, tzinfo=UTC)),
                "until": sqlite_timestamp(T0),
            },
        )
        await session.commit()
    fake = FakeBingX(fills)

    summary = await sync_bingx(factory, fake)

    (outcome,) = summary.accounts
    assert outcome.status is AccountOutcomeStatus.SUCCESS
    assert outcome.error_kind is None, "an identical re-read was taken for a conflict"
    assert (outcome.pages, outcome.fills_seen, outcome.fills_inserted) == (3, 1202, 0)
    assert len(fake.requests) == 3, "the premise: the window really was read again"
    stored = await stored_ids(factory)
    assert len(stored) == len(set(stored)) == 1200
