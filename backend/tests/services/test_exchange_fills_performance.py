"""Spec 024's performance budget: 20,000 fills are listed and totalled in under a bound.

*The budget* (spec 024, *Performance budget*): 20,000 fills in under one second on the
Raspberry Pi, taken as four times slower than the development machine. Filtering, ordering
and totalling run in Python, because `executed_at` and every amount are text in SQLite, so
the whole selected history is loaded, filtered, sorted and summed on every request.

*The guard*: the bound below is three times the time the backend developer measured for this
call with coverage on, on the development machine: 0.666 s at 20,000 fills (median; 0.158 s at
5,000 and 1.631 s at 50,000; without coverage 0.040, 0.199 and 0.460 s). That catches a
regression by an order of magnitude -- a per-row query, a quadratic filter -- without being
flaky in the gate. The best of three calls after a warm-up is what is compared, so one
scheduling hiccup on a busy runner cannot fail it.

**What it does not catch is a revert of ruling R1.** Review reverted `money.add` to the integer
algorithm and measured this call at 0.340 s against 0.171 s: twice as slow, well inside a bound
set at three times the measurement. So R1's speed has a guard of its own, below, that needs no
wall-clock bound at all: in one process, `money.add` against a plain `Decimal.__add__` on the
same operands. The old algorithm is dozens of times slower than a plain `+`; the new one a few.

**What is timed** is `ExchangeService.list_fills` alone, over a session of its own, in the
worst case the endpoint has: no venue filter and no range, so every fill is loaded, kept,
sorted and totalled. Planting the fills is setup and is not timed. The rows are synthetic:
four assets, three quote assets, fees in four assets with both signs, two venues.
"""

from __future__ import annotations

import gc
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest

from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.domain.money import add, require_amount
from portfolio.services.exchanges import DEFAULT_FILLS_LIMIT, build_exchange_service
from tests.accounting_harness import FillRow, plant_account, plant_fills, plant_owner
from tests.domain.test_money import oracle_exact_sum
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.services.exchanges import FillsPage

#: The spec's guarded case.
GUARDED_FILLS: Final = 20_000

#: Three times the developer's measurement of this call at 20,000 fills with coverage on:
#: 3 x 0.666 s, rounded to 2.0.
GUARD_BOUND_SECONDS: Final = 2.0

#: Timed calls after the warm-up; the fastest is compared with the bound.
TIMED_CALLS: Final = 3

START: Final = datetime(2025, 1, 1, tzinfo=UTC)
ASSETS: Final = ("BTC", "ETH", "KAS", "DOGE")
QUOTES: Final = ("USDT", "USDT", "USDC", "BTC")


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


def synthetic_rows(count: int) -> dict[ExchangeKey, list[FillRow]]:
    """`count` fills over two venues, eighteen-place amounts, every quote and fee shape."""
    rows: dict[ExchangeKey, list[FillRow]] = {ExchangeKey.BITGET: [], ExchangeKey.BINGX: []}
    for index in range(count):
        asset = ASSETS[index % len(ASSETS)]
        quote = QUOTES[index % len(QUOTES)]
        if quote == asset:
            quote = "USDT"
        fee_asset, fee = (
            (None, Decimal(0))
            if index % 7 == 0
            else (
                ("BNB", Decimal("-0.000000000000000123"))
                if index % 5 == 0
                else (quote, Decimal("0.1") + index)
            )
        )
        venue = ExchangeKey.BITGET if index % 2 == 0 else ExchangeKey.BINGX
        rows[venue].append(
            FillRow(
                external_trade_id=f"perf-{index}",
                base_asset=asset,
                quote_asset=quote,
                side=FillSide.SELL if index % 3 == 2 else FillSide.BUY,
                quantity=Decimal("0.123456789012345678") * (1 + index % 11),
                quote_quantity=Decimal("1000.123456789012345678") + index,
                fee_amount=fee,
                fee_asset=fee_asset,
                executed_at=START + timedelta(seconds=37 * index),
                price=Decimal("30000.5"),
                external_order_id=f"perf-order-{index // 3}",
            )
        )
    return rows


async def plant(factory: async_sessionmaker[AsyncSession], count: int) -> int:
    async with factory() as session:
        user_id = await plant_owner(session)
        for exchange_key, venue_rows in synthetic_rows(count).items():
            account = await plant_account(session, user_id, exchange_key)
            await plant_fills(session, account, venue_rows)
    return user_id


async def timed_list(
    factory: async_sessionmaker[AsyncSession], user_id: int
) -> tuple[FillsPage, float]:
    async with factory() as session:
        service = build_exchange_service(session, configured=frozenset(), syncing=False)
        started = time.perf_counter()
        page = await service.list_fills(
            user_id,
            exchanges=None,
            from_=None,
            to=None,
            limit=DEFAULT_FILLS_LIMIT,
            offset=0,
        )
        elapsed = time.perf_counter() - started
    return page, elapsed


async def test_twenty_thousand_fills_are_listed_and_totalled_within_the_guard(
    factory: async_sessionmaker[AsyncSession], record_property: Any
) -> None:
    user_id = await plant(factory, GUARDED_FILLS)
    await timed_list(factory, user_id)  # The warm-up: imports, statement caches, the pool.

    timings = [await timed_list(factory, user_id) for _ in range(TIMED_CALLS)]
    page = timings[-1][0]
    elapsed = min(seconds for _page, seconds in timings)

    record_property("fills_20k_list_seconds", round(elapsed, 4))
    print(f"\n{GUARDED_FILLS} fills listed and totalled in {elapsed * 1000:.1f} ms")  # noqa: T201
    # The control: the call did the whole job, so its time means something.
    assert page.total_count == page.totals.fill_count == GUARDED_FILLS
    assert len(page.fills) == DEFAULT_FILLS_LIMIT
    assert page.fills[0].executed_at == START + timedelta(seconds=37 * (GUARDED_FILLS - 1))
    assert sum(row.fill_count for row in page.totals.by_asset) == GUARDED_FILLS
    assert [row.asset for row in page.totals.by_asset] == sorted(ASSETS)
    assert {fee.asset for fee in page.totals.fees} == {"BNB", "BTC", "USDC", "USDT"}
    assert elapsed < GUARD_BOUND_SECONDS, (
        f"{elapsed:.3f} s is over the {GUARD_BOUND_SECONDS} s guard for {GUARDED_FILLS} fills"
    )


# --------------------------------------------------------------------------------------
# Ruling R1's speed, without a wall clock: `money.add` against a plain `Decimal.__add__`
# --------------------------------------------------------------------------------------

#: How many times slower than a plain `Decimal.__add__` `money.add` may be. Measured on the
#: development machine without instrumentation: the new `add` about 4x, the integer
#: algorithm R1 replaced about 49x. 15x leaves the first more than three times its own
#: figure and fails the second by as much.
RATIO_BOUND: Final = 15

#: Operand pairs per timed round, and rounds per function; the fastest round is compared.
RATIO_PAIRS: Final = 2_000
RATIO_ROUNDS: Final = 7


def replaced_add(left: Decimal, right: Decimal) -> Decimal:
    """`money.add` as it was before R1: the same guards, then the integer algorithm."""
    require_amount(left, subject="add")
    require_amount(right, subject="add")
    return oracle_exact_sum(left, right)


def ratio_pairs() -> list[tuple[Decimal, Decimal]]:
    """Eighteen-place amounts, as every stored fill carries: a running total and a fill."""
    return [
        (Decimal(f"{1000 + index}.123456789012345678"), Decimal(f"0.{index:018d}"))
        for index in range(RATIO_PAIRS)
    ]


def fastest_rounds(
    functions: Sequence[Callable[[Decimal, Decimal], Decimal]],
    pairs: Sequence[tuple[Decimal, Decimal]],
) -> list[float]:
    """The fastest of `RATIO_ROUNDS` rounds for each function, the rounds interleaved.

    Interleaved so that a machine that slows down halfway slows every function alike, and the
    collector is off so that a collection lands in no one's round.
    """
    best = [float("inf")] * len(functions)
    gc.disable()
    try:
        for _ in range(RATIO_ROUNDS):
            for position, function in enumerate(functions):
                started = time.perf_counter()
                for left, right in pairs:
                    function(left, right)
                best[position] = min(best[position], time.perf_counter() - started)
    finally:
        gc.enable()
    return best


@pytest.mark.no_cover
def test_money_add_stays_a_small_multiple_of_a_plain_decimal_add(record_property: Any) -> None:
    """R1's speed as a ratio in one process, so the machine's own speed cancels out.

    `no_cover` because coverage traces `money.add`'s Python lines and not the C `__add__` it
    is compared with, which would measure the instrument rather than the code: under the
    gate's coverage the new `add` reads about 39x. Production runs uninstrumented.

    The control is in the same test: the algorithm R1 replaced fails the same bound, in the
    same process, so a bound that has stopped telling the two apart fails here rather than
    passing silently.
    """
    pairs = ratio_pairs()
    plain, current, replaced = fastest_rounds([Decimal.__add__, add, replaced_add], pairs)

    ratio = current / plain
    replaced_ratio = replaced / plain
    record_property("money_add_ratio", round(ratio, 2))
    record_property("replaced_add_ratio", round(replaced_ratio, 2))
    print(  # noqa: T201
        f"\nmoney.add is {ratio:.1f}x a plain add; the replaced one {replaced_ratio:.1f}x"
    )
    assert replaced_ratio > RATIO_BOUND, (
        f"the control: the replaced algorithm is only {replaced_ratio:.1f}x a plain add, "
        f"so a bound of {RATIO_BOUND}x no longer tells the two apart"
    )
    assert ratio < RATIO_BOUND, f"money.add is {ratio:.1f}x a plain Decimal add"
