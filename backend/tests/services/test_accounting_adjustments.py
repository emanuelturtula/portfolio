"""Criteria 2, 3 and 4 of #18: the recompute replays the owner's adjustments with the fills.

`AccountingService.recompute(user_id)` now loads two sources -- the owner's fills and the
owner's `manual_adjustments` rows -- and replays them as **one** event list in the engine's
order (spec 023, *Loading and recompute*). Everything here runs against a real SQLite file
migrated to head and reads the snapshot tables back over a second session, so what is
asserted is what was committed.

## Where the expected figures come from

Not from the engine under test. Every snapshot is compared with spec 019's independent
`Fraction` oracle (`tests/domain/accounting/oracle.py`) run over the same events, and the
round figures are also worked by hand beside the literals. The one value taken from the
engine is the fingerprint, whose correctness `test_fingerprint.py` owns: what is asserted
here is that the service handed `replay` the right events -- the `"manual"` source and the
id padded to twenty digits -- which a wrong source or padding changes.

## Why the rows are planted by SQL here

The ordering tests need adjustments with ids 9 and 10 side by side, and the unconvertible
tests need rows the service refuses. `tests/api/test_adjustments.py` drives the same
behaviour through the endpoints, the way the owner will.
"""

from __future__ import annotations

import copy
import pickle
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import text

from portfolio.domain.accounting import (
    DEFAULT_CASH_ASSETS,
    AccountingConfig,
    Adjustment,
    EventKey,
    Trade,
    replay,
)
from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.services.accounting import (
    RecomputeOutcome,
    RecomputeReason,
    UnconvertibleAdjustmentError,
    build_accounting_service,
)
from tests.accounting_harness import (
    at,
    plant_account,
    plant_fills,
    plant_owner,
    snapshot_tables,
)
from tests.adjustments_harness import plant_adjustment
from tests.domain.accounting import oracle
from tests.exchange_sync_harness import SettableClock, make_fill
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.exchanges.base import NormalizedFill

COMPUTED_AT: Final = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)

#: A distinctive note and asset for the searches that prove a message names nothing.
LEAKY_NOTE: Final = "note-sentinel-" + "R5t" * 5
LEAKY_ASSET: Final = "ZQXWV"

#: A large id, so "the id is not in the message" is a search for six digits.
LEAKY_ADJUSTMENT_ID: Final = 734519


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


@pytest.fixture
def clock() -> SettableClock:
    return SettableClock(COMPUTED_AT)


async def recompute(
    factory: async_sessionmaker[AsyncSession], user_id: int, clock: SettableClock
) -> Any:
    """One recompute over a session of its own, as the trigger runs it."""
    async with factory() as session:
        return await build_accounting_service(session, clock=clock).recompute(user_id)


async def owner_with_fills(
    factory: async_sessionmaker[AsyncSession], fills: Sequence[NormalizedFill]
) -> int:
    """The owner and a Bitget account holding `fills`. Returns the owner's id."""
    async with factory() as session:
        user_id = await plant_owner(session)
        account = await plant_account(session, user_id, ExchangeKey.BITGET)
        if fills:
            await plant_fills(session, account, fills)
    return user_id


async def adjust(factory: async_sessionmaker[AsyncSession], user_id: int, **fields: Any) -> int:
    async with factory() as session:
        return await plant_adjustment(session, user_id, **fields)


def buy(trade_id: int, when: datetime, quantity: str, cost: str) -> NormalizedFill:
    """A BTC buy for USDT with no fee: the basis is exactly `cost`."""
    return make_fill(
        trade_id,
        when,
        quantity=quantity,
        price=str(Decimal(cost) / Decimal(quantity)),
        quote_quantity=cost,
        fee_amount="0",
        fee_asset=None,
    )


def sell(trade_id: int, when: datetime, quantity: str, proceeds: str) -> NormalizedFill:
    """A BTC sale for USDT with no fee: the proceeds are exactly `proceeds`."""
    return make_fill(
        trade_id,
        when,
        side=FillSide.SELL,
        quantity=quantity,
        price=str(Decimal(proceeds) / Decimal(quantity)),
        quote_quantity=proceeds,
        fee_amount="0",
        fee_asset=None,
    )


# --------------------------------------------------------------------------------------
# The oracle's side: the same events, computed independently
# --------------------------------------------------------------------------------------


def oracle_trade(fill: NormalizedFill, source: str = "bitget") -> oracle.Trade:
    return oracle.Trade(
        key=oracle.Key(fill.executed_at, source, fill.external_trade_id),
        base_asset=fill.base_asset,
        quote_asset=fill.quote_asset,
        side="buy" if fill.side is FillSide.BUY else "sell",
        quantity=Fraction(fill.quantity),
        quote_quantity=Fraction(fill.quote_quantity),
        fee_amount=Fraction(fill.fee_amount),
        fee_asset=fill.fee_asset,
    )


def oracle_adjustment(
    adjustment_id: int, when: datetime, asset: str, quantity: str, unit_cost: str | None
) -> oracle.Adjustment:
    """Spec 023's event: `EventKey(occurred_at, "manual", f"{id:020d}")`, written out here."""
    return oracle.Adjustment(
        key=oracle.Key(when, "manual", str(adjustment_id).zfill(20)),
        asset=asset,
        quantity=Fraction(Decimal(quantity)),
        unit_cost=None if unit_cost is None else Fraction(Decimal(unit_cost)),
    )


def expected(events: Sequence[oracle.Event]) -> dict[str, Any]:
    document: dict[str, Any] = oracle.result_to_json(oracle.replay(events, DEFAULT_CASH_ASSETS))
    return document


def amount(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def stored_instant(moment: str) -> str:
    return oracle.parse_time(moment).strftime("%Y-%m-%d %H:%M:%S.%f")


async def assert_snapshot_is_the_oracles(
    factory: async_sessionmaker[AsyncSession], events: Sequence[oracle.Event]
) -> dict[str, Any]:
    """Every position, lot and warning column against the oracle. Returns the tables."""
    document = expected(events)
    tables = await snapshot_tables(factory)
    (header,) = tables["header"]
    assert header["event_count"] == document["event_count"]
    assert amount(header["unallocated_costs"]) == amount(document["unallocated_costs"])
    assert [
        (
            row["asset"],
            amount(row["quantity"]),
            amount(row["unknown_basis_quantity"]),
            amount(row["cost_basis"]),
            amount(row["average_cost"]),
            amount(row["realized_pnl"]),
            amount(row["unmatched_proceeds"]),
            row["flags"],
        )
        for row in tables["positions"]
    ] == [
        (
            position["asset"],
            amount(position["quantity"]),
            amount(position["unknown_basis_quantity"]),
            amount(position["cost_basis"]),
            amount(position["average_cost"]),
            amount(position["realized_pnl"]),
            amount(position["unmatched_proceeds"]),
            ",".join(flag.lower() for flag in position["flags"]),
        )
        for position in document["positions"]
    ]
    assert [
        (
            row["asset"],
            row["occurred_at"],
            row["source"],
            row["external_id"],
            amount(row["quantity"]),
            amount(row["cost_basis"]),
            amount(row["unknown_basis_quantity"]),
        )
        for row in tables["lots"]
    ] == [
        (
            lot["asset"],
            stored_instant(lot["key"]["occurred_at"]),
            lot["key"]["source"],
            lot["key"]["external_id"],
            amount(lot["quantity"]),
            amount(lot["cost_basis"]),
            amount(lot["unknown_basis_quantity"]),
        )
        for lot in document["lots"]
    ]
    assert [
        (row["kind"], row["occurred_at"], row["source"], row["asset"], amount(row["quantity"]))
        for row in tables["warnings"]
    ] == [
        (
            warning["type"],
            stored_instant(warning["key"]["occurred_at"]),
            warning["key"]["source"],
            warning["asset"],
            amount(warning["shortfall"]),
        )
        for warning in document["warnings"]
        if warning["type"] == "negative_inventory"
    ]
    return tables


def btc(tables: dict[str, Any]) -> dict[str, Any]:
    (position,) = [row for row in tables["positions"] if row["asset"] == "BTC"]
    found: dict[str, Any] = position
    return found


# --------------------------------------------------------------------------------------
# Criterion 2: an opening balance repairs a sale that exceeded the history
# --------------------------------------------------------------------------------------

#: The imported history: 1 BTC bought for 30000, then 1.5 BTC sold for 75000.
HISTORY_BUY: Final = buy(1001, at(10), "1", "30000")
HISTORY_SALE: Final = sell(1002, at(20), "1.5", "75000")


async def test_a_sale_past_the_history_warns_and_flags_before_any_adjustment(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """The problem #18 exists for, as the engine reports it without an adjustment.

    The sale takes the whole pool of 1 BTC (basis 30000) and falls 0.5 short. The proceeds
    split by the known share: 75000 x 1 / 1.5 = 50000 against the basis, so 20000 realized,
    and the other 25000 unmatched.
    """
    user_id = await owner_with_fills(factory, [HISTORY_BUY, HISTORY_SALE])

    await recompute(factory, user_id, clock)

    tables = await assert_snapshot_is_the_oracles(
        factory, [oracle_trade(HISTORY_BUY), oracle_trade(HISTORY_SALE)]
    )
    position = btc(tables)
    assert position["flags"] == "history_incomplete"
    assert amount(position["realized_pnl"]) == Decimal(20000)
    assert amount(position["unmatched_proceeds"]) == Decimal(25000)
    (warning,) = tables["warnings"]
    assert (warning["kind"], warning["asset"], amount(warning["quantity"])) == (
        "negative_inventory",
        "BTC",
        Decimal("0.5"),
    )


async def test_an_opening_balance_before_the_sale_removes_the_warning_and_the_flag(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """Criterion 2: the same history with 1 BTC at 20000 recorded before it began.

    By hand: the pool before the sale is 1 BTC at 20000 plus 1 BTC at 30000, so 2 BTC at a
    basis of 50000 and an average of 25000. Selling 1.5 takes 1.5 x 25000 = 37500 of basis
    against 75000 of proceeds: 37500 realized, nothing unmatched, no shortfall. 0.5 BTC stays
    at a basis of 12500.
    """
    user_id = await owner_with_fills(factory, [HISTORY_BUY, HISTORY_SALE])
    await recompute(factory, user_id, clock)
    adjustment_id = await adjust(
        factory, user_id, asset="BTC", quantity="1", unit_cost="20000", occurred_at=at(0)
    )
    clock.advance(timedelta(minutes=5))

    report = await recompute(factory, user_id, clock)

    assert report.outcome is RecomputeOutcome.WRITTEN
    assert report.event_count == 3
    tables = await assert_snapshot_is_the_oracles(
        factory,
        [
            oracle_trade(HISTORY_BUY),
            oracle_trade(HISTORY_SALE),
            oracle_adjustment(adjustment_id, at(0), "BTC", "1", "20000"),
        ],
    )
    position = btc(tables)
    assert tables["warnings"] == [], "the negative_inventory warning is gone"
    assert position["flags"] == "", "and so is the history_incomplete flag"
    assert amount(position["realized_pnl"]) == Decimal(37500)
    assert amount(position["unmatched_proceeds"]) == 0
    assert amount(position["quantity"]) == Decimal("0.5")
    assert amount(position["cost_basis"]) == Decimal(12500)
    assert amount(position["average_cost"]) == Decimal(25000)


async def test_the_adjustments_lot_is_stored_under_its_manual_identity(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """Spec 023, *The event*: kind `adjustment`, source `manual`, the id padded to twenty."""
    user_id = await owner_with_fills(factory, [HISTORY_BUY, HISTORY_SALE])
    adjustment_id = await adjust(
        factory, user_id, asset="BTC", quantity="1", unit_cost="20000", occurred_at=at(0)
    )

    await recompute(factory, user_id, clock)

    lots = (await snapshot_tables(factory))["lots"]
    assert [(row["kind"], row["source"], row["external_id"]) for row in lots] == [
        ("adjustment", "manual", f"{adjustment_id:020d}"),
        ("trade", "bitget", "1001"),
    ]
    assert len(lots[0]["external_id"]) == 20
    assert amount(lots[0]["cost_basis"]) == Decimal(20000)
    assert amount(lots[0]["unknown_basis_quantity"]) == 0


# --------------------------------------------------------------------------------------
# Criterion 3: no unit cost is unknown cost, never zero
# --------------------------------------------------------------------------------------


async def test_an_adjustment_without_a_cost_is_unknown_basis_and_realizes_nothing(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """1 BTC of unknown cost, then 0.4 sold for 20000.

    By hand: the pool holds nothing known, so the sale's known share is zero. It realizes
    nothing and books all 20000 to `unmatched_proceeds`. 0.6 BTC stays, all of it of unknown
    cost, flagged, with no average. Valued at zero instead, the sale would have realized
    20000 of fictitious profit -- which the control below shows is what a zero cost does.
    """
    sale = sell(1101, at(10), "0.4", "20000")
    user_id = await owner_with_fills(factory, [sale])
    adjustment_id = await adjust(
        factory, user_id, asset="BTC", quantity="1", unit_cost=None, occurred_at=at(0)
    )

    await recompute(factory, user_id, clock)

    tables = await assert_snapshot_is_the_oracles(
        factory,
        [oracle_trade(sale), oracle_adjustment(adjustment_id, at(0), "BTC", "1", None)],
    )
    position = btc(tables)
    assert position["flags"] == "unknown_basis"
    assert amount(position["quantity"]) == Decimal("0.6")
    assert amount(position["unknown_basis_quantity"]) == Decimal("0.6")
    assert amount(position["cost_basis"]) == 0
    assert position["average_cost"] is None
    assert amount(position["realized_pnl"]) == 0
    assert amount(position["unmatched_proceeds"]) == Decimal(20000)
    assert tables["warnings"] == []
    (lot,) = [row for row in tables["lots"] if row["kind"] == "adjustment"]
    assert amount(lot["unknown_basis_quantity"]) == Decimal(1)
    assert amount(lot["cost_basis"]) == 0


async def test_once_the_unknown_units_are_sold_the_flag_clears_and_the_proceeds_stay_unmatched(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """Spec 023, R3: `unknown_basis` describes units still held; the sale's effect is permanent.

    1 BTC of unknown cost, all of it sold for 50000: nothing is held, so nothing is flagged,
    and the sale realized nothing -- all 50000 unmatched, never counted as profit.
    """
    sale = sell(1102, at(10), "1", "50000")
    user_id = await owner_with_fills(factory, [sale])
    adjustment_id = await adjust(
        factory, user_id, asset="BTC", quantity="1", unit_cost=None, occurred_at=at(0)
    )

    await recompute(factory, user_id, clock)

    tables = await assert_snapshot_is_the_oracles(
        factory,
        [oracle_trade(sale), oracle_adjustment(adjustment_id, at(0), "BTC", "1", None)],
    )
    position = btc(tables)
    assert position["flags"] == ""
    assert amount(position["quantity"]) == 0
    assert amount(position["realized_pnl"]) == 0
    assert amount(position["unmatched_proceeds"]) == Decimal(50000)


async def test_a_zero_cost_is_a_known_cost_of_nothing(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """The control: `"0"` is accepted and is not `null`. The whole sale is realized profit."""
    sale = sell(1101, at(10), "0.4", "20000")
    user_id = await owner_with_fills(factory, [sale])
    adjustment_id = await adjust(
        factory, user_id, asset="BTC", quantity="1", unit_cost="0", occurred_at=at(0)
    )

    await recompute(factory, user_id, clock)

    tables = await assert_snapshot_is_the_oracles(
        factory,
        [oracle_trade(sale), oracle_adjustment(adjustment_id, at(0), "BTC", "1", "0")],
    )
    position = btc(tables)
    assert position["flags"] == ""
    assert amount(position["unknown_basis_quantity"]) == 0
    assert amount(position["realized_pnl"]) == Decimal(20000)
    assert amount(position["unmatched_proceeds"]) == 0


# --------------------------------------------------------------------------------------
# Criterion 4: one order, and a stable fingerprint
# --------------------------------------------------------------------------------------


async def test_a_fill_and_two_adjustments_at_one_instant_replay_in_the_documented_order(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """`(occurred_at, source, external_id, kind)`: the venue's fill, then id 9, then id 10.

    `"bitget" < "manual"`, so the fill comes first; and the ids are padded to twenty digits,
    so id 9 sorts before id 10 as text. Unpadded, `"10" < "9"` and they would swap. The lots
    are stored in event order, so their `seq` is the replay order.
    """
    instant = at(30)
    fill = buy(1201, instant, "1", "30000")
    user_id = await owner_with_fills(factory, [fill])
    # Entered in the opposite order of their ids, so insertion order proves nothing.
    ten = await adjust(
        factory, user_id, adjustment_id=10, quantity="2", unit_cost="10", occurred_at=instant
    )
    nine = await adjust(
        factory, user_id, adjustment_id=9, quantity="3", unit_cost="20", occurred_at=instant
    )

    await recompute(factory, user_id, clock)

    tables = await assert_snapshot_is_the_oracles(
        factory,
        [
            oracle_trade(fill),
            oracle_adjustment(ten, instant, "BTC", "2", "10"),
            oracle_adjustment(nine, instant, "BTC", "3", "20"),
        ],
    )
    assert [(row["seq"], row["source"], row["external_id"]) for row in tables["lots"]] == [
        (0, "bitget", "1201"),
        (1, "manual", "00000000000000000009"),
        (2, "manual", "00000000000000000010"),
    ]


async def test_an_adjustment_at_the_same_instant_as_the_sale_does_not_cover_it(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """Spec 023, *Risks*: `"manual"` sorts after every venue, so the sale replays first.

    The owner who dates an opening balance at the very instant of the sale it should cover
    still sees the warning. That is the documented behaviour, and this pins it: the order is
    the engine's, not the order the owner meant.
    """
    user_id = await owner_with_fills(factory, [HISTORY_BUY, HISTORY_SALE])
    adjustment_id = await adjust(
        factory, user_id, asset="BTC", quantity="1", unit_cost="20000", occurred_at=at(20)
    )

    await recompute(factory, user_id, clock)

    tables = await assert_snapshot_is_the_oracles(
        factory,
        [
            oracle_trade(HISTORY_BUY),
            oracle_trade(HISTORY_SALE),
            oracle_adjustment(adjustment_id, at(20), "BTC", "1", "20000"),
        ],
    )
    position = btc(tables)
    assert position["flags"] == "history_incomplete"
    assert [row["kind"] for row in tables["warnings"]] == ["negative_inventory"]
    assert amount(position["quantity"]) == Decimal(1), "the adjustment arrived after the sale"


async def test_the_fingerprint_is_the_engines_over_the_manual_events_and_is_stable(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """The stored fingerprint is `replay`'s over exactly spec 023's events, twice over.

    The expected events are built here, from the spec's rule, not by the code under test: a
    source other than `"manual"`, an unpadded id or a dropped unit cost is a different
    fingerprint. A second recompute over the same rows is `UNCHANGED` and writes nothing.
    """
    instant = at(30)
    fill = buy(1301, instant, "1", "30000")
    user_id = await owner_with_fills(factory, [fill])
    nine = await adjust(
        factory, user_id, adjustment_id=9, quantity="3", unit_cost=None, occurred_at=instant
    )
    ten = await adjust(
        factory, user_id, adjustment_id=10, quantity="2", unit_cost="10", occurred_at=at(5)
    )
    events: list[Trade | Adjustment] = [
        Trade(
            key=EventKey(instant, "bitget", "1301"),
            base_asset="BTC",
            quote_asset="USDT",
            side=FillSide.BUY,
            quantity=Decimal(1),
            quote_quantity=Decimal(30000),
            fee_amount=Decimal(0),
            fee_asset=None,
        ),
        Adjustment(
            key=EventKey(instant, "manual", "00000000000000000009"),
            asset="BTC",
            quantity=Decimal(3),
            unit_cost=None,
        ),
        Adjustment(
            key=EventKey(at(5), "manual", "00000000000000000010"),
            asset="BTC",
            quantity=Decimal(2),
            unit_cost=Decimal(10),
        ),
    ]
    assert (nine, ten) == (9, 10)
    engine_says = replay(events, AccountingConfig(DEFAULT_CASH_ASSETS)).input_fingerprint

    first = await recompute(factory, user_id, clock)
    before = await snapshot_tables(factory)
    clock.advance(timedelta(hours=1))
    second = await recompute(factory, user_id, clock)

    assert (first.outcome, second.outcome) == (RecomputeOutcome.WRITTEN, RecomputeOutcome.UNCHANGED)
    assert first.event_count == second.event_count == 3
    (header,) = before["header"]
    assert header["input_fingerprint"] == engine_says
    assert await snapshot_tables(factory) == before


async def test_editing_or_deleting_an_adjustment_changes_the_fingerprint(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """The other half of stability: a changed row is a changed input, and is written."""
    user_id = await owner_with_fills(factory, [HISTORY_BUY])
    adjustment_id = await adjust(factory, user_id, quantity="1", occurred_at=at(0))
    await recompute(factory, user_id, clock)
    fingerprints = [(await snapshot_tables(factory))["header"][0]["input_fingerprint"]]

    async with factory() as session:
        await session.execute(
            text("UPDATE manual_adjustments SET unit_cost = NULL WHERE id = :id"),
            {"id": adjustment_id},
        )
        await session.commit()
    edited = await recompute(factory, user_id, clock)
    fingerprints.append((await snapshot_tables(factory))["header"][0]["input_fingerprint"])
    async with factory() as session:
        await session.execute(
            text("DELETE FROM manual_adjustments WHERE id = :id"), {"id": adjustment_id}
        )
        await session.commit()
    deleted = await recompute(factory, user_id, clock)
    fingerprints.append((await snapshot_tables(factory))["header"][0]["input_fingerprint"])

    assert (edited.outcome, deleted.outcome) == (RecomputeOutcome.WRITTEN,) * 2
    assert (edited.event_count, deleted.event_count) == (2, 1)
    assert len(set(fingerprints)) == 3


async def test_the_recompute_reads_only_the_owners_adjustments(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    user_id = await owner_with_fills(factory, [HISTORY_BUY])
    async with factory() as session:
        stranger = await plant_owner(session, "someone-else")
    await adjust(factory, user_id, occurred_at=at(0))
    await adjust(factory, stranger, occurred_at=at(0), asset="KAS", quantity="500")
    await adjust(factory, stranger, occurred_at=at(1), asset="KAS", quantity="500")

    mine = await recompute(factory, user_id, clock)
    theirs = await recompute(factory, stranger, clock)

    assert (mine.event_count, theirs.event_count) == (2, 2)
    positions = (await snapshot_tables(factory))["positions"]
    assert sorted(row["asset"] for row in positions) == ["BTC", "KAS"]


def test_the_recompute_reason_for_an_adjustment() -> None:
    assert RecomputeReason.ADJUSTMENT.value == "adjustment"
    assert RecomputeReason("adjustment") is RecomputeReason.ADJUSTMENT


# --------------------------------------------------------------------------------------
# A stored row that does not convert fails the recompute, loudly
# --------------------------------------------------------------------------------------

#: Rows the service refuses at entry, written by SQL: each breaks one rule `Adjustment` holds.
UNCONVERTIBLE: Final[dict[str, dict[str, Any]]] = {
    "zero quantity": {"raw_quantity": "0.000000000000000000"},
    "negative quantity": {"raw_quantity": "-1.000000000000000000"},
    "negative unit cost": {"unit_cost": "-1"},
    "blank asset": {"asset": "   "},
    "total cost past the range": {
        "raw_quantity": "10000000000000000000.000000000000000000",
        "unit_cost": "10000000000000000000",
    },
}


@pytest.mark.parametrize("shape", sorted(UNCONVERTIBLE))
async def test_an_unconvertible_adjustment_fails_the_recompute_and_keeps_the_old_snapshot(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock, shape: str
) -> None:
    """Spec 023: never skipped, never a partial write, and nothing identifying in the message."""
    user_id = await owner_with_fills(factory, [HISTORY_BUY])
    await adjust(factory, user_id, occurred_at=at(0))
    await recompute(factory, user_id, clock)
    before = await snapshot_tables(factory)
    await adjust(
        factory,
        user_id,
        adjustment_id=LEAKY_ADJUSTMENT_ID,
        occurred_at=at(40),
        note=LEAKY_NOTE,
        **UNCONVERTIBLE[shape],
    )
    clock.advance(timedelta(hours=1))

    with pytest.raises(UnconvertibleAdjustmentError) as raised:
        await recompute(factory, user_id, clock)

    error = raised.value
    assert error.adjustment_id == LEAKY_ADJUSTMENT_ID
    for rendering in (str(error), repr(error), repr(error.args)):
        assert str(LEAKY_ADJUSTMENT_ID) not in rendering
        assert LEAKY_NOTE not in rendering
        assert "BTC" not in rendering
    assert str(error).strip()
    assert isinstance(error.__cause__, (ValueError, TypeError)), "the rule it broke is chained"
    assert await snapshot_tables(factory) == before, "the previous snapshot stands, untouched"


async def test_an_unconvertible_adjustment_names_no_asset(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """A distinctive asset spelling, so the search above for `BTC` is not the only one."""
    user_id = await owner_with_fills(factory, [])
    await adjust(
        factory, user_id, asset=LEAKY_ASSET, raw_quantity="0.000000000000000000", occurred_at=at(0)
    )

    with pytest.raises(UnconvertibleAdjustmentError) as raised:
        await recompute(factory, user_id, clock)

    assert LEAKY_ASSET not in str(raised.value)
    assert LEAKY_ASSET not in repr(raised.value)


async def test_the_unconvertible_adjustment_error_pickles_and_copies(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """The way `UnconvertibleFillError` does: a round trip keeps the type and the id."""
    user_id = await owner_with_fills(factory, [])
    await adjust(
        factory,
        user_id,
        adjustment_id=LEAKY_ADJUSTMENT_ID,
        raw_quantity="0.000000000000000000",
        occurred_at=at(0),
    )
    with pytest.raises(UnconvertibleAdjustmentError) as raised:
        await recompute(factory, user_id, clock)
    error = raised.value

    for twin in (pickle.loads(pickle.dumps(error)), copy.copy(error), copy.deepcopy(error)):  # noqa: S301
        assert type(twin) is UnconvertibleAdjustmentError
        assert twin.adjustment_id == LEAKY_ADJUSTMENT_ID
        assert str(twin) == str(error)


async def test_the_unconvertible_message_is_the_same_whichever_row_it_is(
    tmp_path: Path, clock: SettableClock
) -> None:
    """A fixed message: two ids, two shapes, one text."""
    messages = []
    for directory, adjustment_id, fields in (
        (tmp_path / "one", 11, {"raw_quantity": "0.000000000000000000"}),
        (tmp_path / "two", 222222, {"unit_cost": "-5", "asset": "KAS"}),
    ):
        directory.mkdir()
        async with migrated_sessionmaker(directory) as built:
            user_id = await owner_with_fills(built, [])
            await adjust(built, user_id, adjustment_id=adjustment_id, occurred_at=at(0), **fields)
            with pytest.raises(UnconvertibleAdjustmentError) as raised:
                await recompute(built, user_id, clock)
            messages.append(str(raised.value))

    assert messages[0] == messages[1]
