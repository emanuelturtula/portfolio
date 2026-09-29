"""Criterion 8 of #19: a recompute over spec 019's golden scenario is fast, and scales.

*Interpretation* (spec 021): the service's recompute runs over the golden scenario's fills
through a real SQLite file, and must finish in **under 0.5 s** here -- a 4x margin under the
issue's two seconds on the Raspberry Pi. A second case of 5,000 synthetic fills records its
time rather than asserting one; after the deploy, the startup recompute's `duration_ms` on
the Pi is the measurement on the real hardware.

**What is timed** is `recompute(user_id)` alone, over a session of its own: loading the
fills, converting them, `replay` in the worker thread, and the write of the whole snapshot.
Planting the fills is setup and is not timed.

The golden scenario holds adjustments and transfers beside its 39 trade rows; a recompute
reads fills only (adjustments are #18), so the trades are what is planted -- 38 of them,
since one is the same fill read twice. Their snapshot is also compared with the
`Fraction` oracle over the same trades, so the fast answer is shown to be the right one.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest

from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.services.accounting import RecomputeOutcome, build_accounting_service
from tests.accounting_harness import (
    FillRow,
    plant_account,
    plant_fills,
    plant_owner,
    snapshot_tables,
)
from tests.domain.accounting import oracle
from tests.exchange_sync_harness import SettableClock
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: Spec 021's bound on the development machine: a quarter of the Pi's two seconds.
GOLDEN_BOUND_SECONDS: Final = 0.5

#: The scale case's size, from the spec.
SCALE_FILLS: Final = 5_000

COMPUTED_AT: Final = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


def golden_trades() -> list[oracle.Trade]:
    """The scenario's trades, each identity once, in file order."""
    _cash, events = oracle.load_scenario()
    seen: set[tuple[str, str]] = set()
    trades = []
    for event in events:
        if not isinstance(event, oracle.Trade):
            continue
        identity = (event.key.source, event.key.external_id)
        if identity in seen:
            continue
        seen.add(identity)
        trades.append(event)
    return trades


def row_of(trade: oracle.Trade) -> FillRow:
    return FillRow(
        external_trade_id=trade.key.external_id,
        base_asset=trade.base_asset,
        quote_asset=trade.quote_asset,
        side=FillSide.BUY if trade.side == "buy" else FillSide.SELL,
        quantity=oracle.to_decimal(trade.quantity),
        quote_quantity=oracle.to_decimal(trade.quote_quantity),
        fee_amount=oracle.to_decimal(trade.fee_amount),
        fee_asset=trade.fee_asset,
        executed_at=trade.key.occurred_at,
    )


async def plant_rows(
    factory: async_sessionmaker[AsyncSession], rows: dict[ExchangeKey, list[FillRow]]
) -> int:
    async with factory() as session:
        user_id = await plant_owner(session)
        for exchange_key, venue_rows in rows.items():
            account = await plant_account(session, user_id, exchange_key)
            await plant_fills(session, account, venue_rows)
    return user_id


async def timed_recompute(
    factory: async_sessionmaker[AsyncSession], user_id: int
) -> tuple[Any, float]:
    clock = SettableClock(COMPUTED_AT)
    async with factory() as session:
        service = build_accounting_service(session, clock=clock)
        started = time.perf_counter()
        report = await service.recompute(user_id)
        elapsed = time.perf_counter() - started
    return report, elapsed


def amount(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


async def test_the_golden_scenario_recomputes_in_under_half_a_second(
    factory: async_sessionmaker[AsyncSession], record_property: Any
) -> None:
    trades = golden_trades()
    by_venue: dict[ExchangeKey, list[FillRow]] = {}
    for trade in trades:
        by_venue.setdefault(ExchangeKey(trade.key.source), []).append(row_of(trade))
    user_id = await plant_rows(factory, by_venue)

    report, elapsed = await timed_recompute(factory, user_id)

    record_property("golden_recompute_seconds", round(elapsed, 4))
    print(f"\ngolden scenario: {len(trades)} fills recomputed in {elapsed * 1000:.1f} ms")  # noqa: T201
    assert report.outcome is RecomputeOutcome.WRITTEN
    assert report.event_count == len(trades) == 38
    assert elapsed < GOLDEN_BOUND_SECONDS, (
        f"{elapsed:.3f} s is over the {GOLDEN_BOUND_SECONDS} s bound"
    )

    expected: dict[str, Any] = oracle.result_to_json(
        oracle.replay(trades, frozenset({"USDC", "USDT"}))
    )
    stored = (await snapshot_tables(factory))["positions"]
    assert [
        (
            row["asset"],
            amount(row["quantity"]),
            amount(row["cost_basis"]),
            amount(row["realized_pnl"]),
        )
        for row in stored
    ] == [
        (
            position["asset"],
            amount(position["quantity"]),
            amount(position["cost_basis"]),
            amount(position["realized_pnl"]),
        )
        for position in expected["positions"]
    ]


def synthetic_rows(count: int) -> dict[ExchangeKey, list[FillRow]]:
    """`count` valid fills over four assets and two venues: two buys, then a partial sale.

    Quantities carry eighteen places so every proportional split rounds, and the fees move
    between the quote, the base and a third asset, so every leg the engine has is exercised.
    """
    assets = ("BTC", "KAS", "ETH", "DOGE")
    start = datetime(2025, 1, 1, tzinfo=UTC)
    rows: dict[ExchangeKey, list[FillRow]] = {ExchangeKey.BITGET: [], ExchangeKey.BINGX: []}
    for index in range(count):
        asset = assets[index % len(assets)]
        venue = ExchangeKey.BITGET if index % 2 == 0 else ExchangeKey.BINGX
        selling = index % 3 == 2
        quantity = Decimal("0.123456789012345678") * (1 + index % 7)
        fee_asset, fee = (
            ("USDT", Decimal("0.1"))
            if index % 5
            else (
                (asset, Decimal("0.000000000000000123"))
                if not selling
                else ("BGB", Decimal("0.01"))
            )
        )
        rows[venue].append(
            FillRow(
                external_trade_id=str(100_000 + index),
                base_asset=asset,
                quote_asset="USDT",
                side=FillSide.SELL if selling else FillSide.BUY,
                quantity=quantity * 2 if not selling else quantity,
                quote_quantity=Decimal("1000.5") + index,
                fee_amount=fee,
                fee_asset=fee_asset,
                executed_at=start + timedelta(minutes=index),
            )
        )
    return rows


async def test_five_thousand_fills_record_their_recompute_time(
    factory: async_sessionmaker[AsyncSession], record_property: Any
) -> None:
    """Recorded, not bounded: the number the spec asks to see, printed and in the report."""
    user_id = await plant_rows(factory, synthetic_rows(SCALE_FILLS))

    report, elapsed = await timed_recompute(factory, user_id)
    again, unchanged_elapsed = await timed_recompute(factory, user_id)

    record_property("scale_recompute_seconds", round(elapsed, 4))
    record_property("scale_unchanged_recompute_seconds", round(unchanged_elapsed, 4))
    print(  # noqa: T201
        f"\n{SCALE_FILLS} fills: written in {elapsed * 1000:.1f} ms, "
        f"unchanged in {unchanged_elapsed * 1000:.1f} ms"
    )
    assert (report.outcome, report.event_count) == (RecomputeOutcome.WRITTEN, SCALE_FILLS)
    assert (again.outcome, again.event_count) == (RecomputeOutcome.UNCHANGED, SCALE_FILLS)
    tables = await snapshot_tables(factory)
    assert [row["asset"] for row in tables["positions"]] == ["BGB", "BTC", "DOGE", "ETH", "KAS"]
