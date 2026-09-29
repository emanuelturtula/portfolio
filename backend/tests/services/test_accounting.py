"""Criteria 1, 2 and 7 of #19: the recompute persists what `replay` computed, only on a change.

`AccountingService.recompute(user_id)` loads the owner's fills, converts each to a `Trade`,
replays them and writes the result as one snapshot -- a header, a position per asset, the
lots and the warnings -- unless the stored header's fingerprint is the new one. Everything
here runs against a real SQLite file migrated to head, through the application's own
engine, and reads the tables back over a second session, so what is asserted is what was
**committed**.

## Where the expected figures come from

Not from the engine under test. The snapshot's positions, lots and warnings are compared
with spec 019's independent `Fraction` oracle (`tests/domain/accounting/oracle.py`) run over
the same fills, and the round figures are also worked by hand beside the literals. The one
value taken from the engine is the fingerprint, whose correctness `test_fingerprint.py`
owns: what is asserted here is that the service handed `replay` the right events, which a
wrong `source` or `external_id` would change.
"""

from __future__ import annotations

import copy
import pickle
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import event, text
from sqlalchemy.dialects import sqlite

from portfolio.domain.accounting import (
    DEFAULT_CASH_ASSETS,
    ENGINE_VERSION,
    METHOD,
    AccountingConfig,
    EventKey,
    Trade,
    replay,
)
from portfolio.domain.chains import CHAIN_ASSET_SYMBOLS, ChainKey
from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.providers.exchanges import bingx, bitget
from portfolio.providers.prices.base import SUPPORTED_PAIRS
from portfolio.repositories.exchanges import select_fills_for_accounting
from portfolio.services.accounting import (
    PRICED_ASSETS,
    RecomputeOutcome,
    UnconvertibleFillError,
    build_accounting_service,
)
from tests.accounting_harness import (
    at,
    dump_accounting_tables,
    plant_account,
    plant_fills,
    plant_owner,
    plant_price,
    plant_unconvertible_fill,
    snapshot_tables,
)
from tests.domain.accounting import oracle
from tests.exchange_sync_harness import SettableClock, make_fill
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.exchanges.base import NormalizedFill

#: The instant the recompute's clock reads, a whole second.
COMPUTED_AT: Final = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)

#: An obviously synthetic trade id, distinctive enough that finding it anywhere is a leak.
LEAKY_TRADE_ID: Final = "tid-7QZ3-unconvertible"

#: A large account id, so "the id is not in the message" is a search for six digits.
LEAKY_ACCOUNT_ID: Final = 918273


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


# --------------------------------------------------------------------------------------
# The history: six fills over two venues, every shape a snapshot has to hold
# --------------------------------------------------------------------------------------


def bitget_fills() -> list[NormalizedFill]:
    """Buys of BTC (a quote fee, then an unattributable BGB fee), and KAS bought and sold."""
    return [
        # 1 BTC for 30000 + 30 fee: C = 30030.
        make_fill(
            1001,
            at(0),
            quantity="1",
            price="30000",
            quote_quantity="30000",
            fee_amount="30",
            fee_asset="USDT",
        ),
        # 0.5 BTC for 20000, the fee in BGB nobody bought: C = 50030, UNATTRIBUTED_FEE.
        make_fill(
            1002,
            at(10),
            quantity="0.5",
            price="40000",
            quote_quantity="20000",
            fee_amount="0.001",
            fee_asset="BGB",
        ),
        # 1000 KAS for 100, no fee.
        make_fill(
            1003,
            at(40),
            symbol="KASUSDT",
            base_asset="KAS",
            quantity="1000",
            price="0.1",
            quote_quantity="100",
            fee_amount="0",
            fee_asset=None,
        ),
        # All of it sold for 150 - 0.15: realized 149.85 - 100 = 49.85.
        make_fill(
            1004,
            at(50),
            symbol="KASUSDT",
            base_asset="KAS",
            side=FillSide.SELL,
            quantity="1000",
            price="0.15",
            quote_quantity="150",
            fee_amount="0.15",
            fee_asset="USDT",
        ),
    ]


def bingx_fills() -> list[NormalizedFill]:
    """A BTC sale for USDC, and a swap from ETH nobody held into BTC of unknown cost."""
    return [
        # 0.3 of 1.5 BTC sold for 15000 - 15: basis 50030 x 0.2 = 10006, realized 4979.
        make_fill(
            2001,
            at(20),
            symbol="BTC-USDC",
            quote_asset="USDC",
            side=FillSide.SELL,
            quantity="0.3",
            price="50000",
            quote_quantity="15000",
            fee_amount="15",
            fee_asset="USDC",
        ),
        # 2 ETH that were never bought, for 0.1 BTC less a 0.0001 BTC fee: a 2 ETH
        # shortfall, and 0.0999 BTC of unknown cost.
        make_fill(
            2002,
            at(30),
            symbol="ETH-BTC",
            base_asset="ETH",
            quote_asset="BTC",
            side=FillSide.SELL,
            quantity="2",
            price="0.05",
            quote_quantity="0.1",
            fee_amount="0.0001",
            fee_asset="BTC",
        ),
    ]


async def plant_history(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int, int]:
    """The owner, a Bitget and a BingX account, and the six fills. Returns the three ids."""
    async with factory() as session:
        user_id = await plant_owner(session)
        bitget_account = await plant_account(session, user_id, ExchangeKey.BITGET)
        bingx_account = await plant_account(
            session, user_id, ExchangeKey.BINGX, account_id=LEAKY_ACCOUNT_ID
        )
        await plant_fills(session, bitget_account, bitget_fills())
        await plant_fills(session, bingx_account, bingx_fills())
    return user_id, bitget_account, bingx_account


def trade_of(fill: NormalizedFill, source: str) -> Trade:
    """The `Trade` spec 021 says a stored fill maps to: the venue key as the source."""
    return Trade(
        key=EventKey(fill.executed_at, source, fill.external_trade_id),
        base_asset=fill.base_asset,
        quote_asset=fill.quote_asset,
        side=fill.side,
        quantity=fill.quantity,
        quote_quantity=fill.quote_quantity,
        fee_amount=fill.fee_amount,
        fee_asset=fill.fee_asset,
    )


def history_trades() -> list[Trade]:
    return [trade_of(fill, "bitget") for fill in bitget_fills()] + [
        trade_of(fill, "bingx") for fill in bingx_fills()
    ]


def oracle_trade(trade: Trade) -> oracle.Trade:
    return oracle.Trade(
        key=oracle.Key(trade.key.occurred_at, trade.key.source, trade.key.external_id),
        base_asset=trade.base_asset,
        quote_asset=trade.quote_asset,
        side="buy" if trade.side is FillSide.BUY else "sell",
        quantity=Fraction(trade.quantity),
        quote_quantity=Fraction(trade.quote_quantity),
        fee_amount=Fraction(trade.fee_amount),
        fee_asset=trade.fee_asset,
    )


def expected_document(trades: list[Trade]) -> dict[str, Any]:
    """The oracle's answer for these trades, in the golden file's shape."""
    result = oracle.replay([oracle_trade(trade) for trade in trades], DEFAULT_CASH_ASSETS)
    document: dict[str, Any] = oracle.result_to_json(result)
    return document


def amount(value: object) -> Decimal | None:
    """A stored or rendered amount as a `Decimal`, `None` kept."""
    return None if value is None else Decimal(str(value))


def stored_instant(moment: str) -> str:
    """An oracle ISO instant as `UtcDateTime` stores it."""
    return oracle.parse_time(moment).strftime("%Y-%m-%d %H:%M:%S.%f")


# --------------------------------------------------------------------------------------
# Criterion 1: the snapshot is persisted
# --------------------------------------------------------------------------------------


async def test_recompute_persists_the_header(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """Method, engine version, fingerprint, event count, unallocated costs, computed at."""
    user_id, _bitget, _bingx = await plant_history(factory)
    expected = replay(history_trades(), AccountingConfig(DEFAULT_CASH_ASSETS))

    report = await recompute(factory, user_id, clock)

    assert report.outcome is RecomputeOutcome.WRITTEN
    assert report.event_count == 6
    (header,) = (await snapshot_tables(factory))["header"]
    assert header["user_id"] == user_id
    assert header["method"] == METHOD == "weighted_average"
    assert header["engine_version"] == ENGINE_VERSION == 1
    assert header["input_fingerprint"] == expected.input_fingerprint
    assert header["event_count"] == 6
    assert amount(header["unallocated_costs"]) == Decimal(0)
    assert header["computed_at"] == COMPUTED_AT.strftime("%Y-%m-%d %H:%M:%S.%f")


async def test_recompute_persists_one_position_per_asset_as_the_oracle_computes_it(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """Every column of every position, against the `Fraction` oracle over the same fills."""
    user_id, _bitget, _bingx = await plant_history(factory)
    expected = expected_document(history_trades())

    await recompute(factory, user_id, clock)

    tables = await snapshot_tables(factory)
    (header,) = tables["header"]
    stored = tables["positions"]
    assert [row["asset"] for row in stored] == ["BGB", "BTC", "ETH", "KAS"]
    assert {row["snapshot_id"] for row in stored} == {header["id"]}
    assert [
        {
            "asset": row["asset"],
            "quantity": amount(row["quantity"]),
            "unknown_basis_quantity": amount(row["unknown_basis_quantity"]),
            "cost_basis": amount(row["cost_basis"]),
            "average_cost": amount(row["average_cost"]),
            "realized_pnl": amount(row["realized_pnl"]),
            "unmatched_proceeds": amount(row["unmatched_proceeds"]),
            "flags": row["flags"],
        }
        for row in stored
    ] == [
        {
            "asset": position["asset"],
            "quantity": amount(position["quantity"]),
            "unknown_basis_quantity": amount(position["unknown_basis_quantity"]),
            "cost_basis": amount(position["cost_basis"]),
            "average_cost": amount(position["average_cost"]),
            "realized_pnl": amount(position["realized_pnl"]),
            "unmatched_proceeds": amount(position["unmatched_proceeds"]),
            "flags": ",".join(flag.lower() for flag in position["flags"]),
        }
        for position in expected["positions"]
    ]


async def test_recompute_persists_the_hand_checked_figures(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """The round numbers, worked without the oracle, so the oracle is held to something too.

    BTC: 1 + 0.5 - 0.3 = 1.2 known at 50030 - 10006 = 40024, plus 0.0999 of unknown cost;
    realized 14985 - 10006 = 4979; average 40024 / 1.2 = 33353.333... at 18 places.
    KAS: bought and sold out, realized 149.85 - 100 = 49.85, nothing left.
    ETH: sold without being held -- nothing held, and the history flagged incomplete.
    """
    user_id, _bitget, _bingx = await plant_history(factory)

    await recompute(factory, user_id, clock)

    by_asset = {row["asset"]: row for row in (await snapshot_tables(factory))["positions"]}
    btc, kas, eth = by_asset["BTC"], by_asset["KAS"], by_asset["ETH"]
    assert amount(btc["quantity"]) == Decimal("1.2999")
    assert amount(btc["unknown_basis_quantity"]) == Decimal("0.0999")
    assert amount(btc["cost_basis"]) == Decimal(40024)
    assert amount(btc["average_cost"]) == Decimal("33353.333333333333333333")
    assert amount(btc["realized_pnl"]) == Decimal(4979)
    assert btc["flags"] == "unattributed_fee,unknown_basis"
    assert amount(kas["quantity"]) == 0
    assert kas["average_cost"] is None
    assert amount(kas["realized_pnl"]) == Decimal("49.85")
    assert kas["flags"] == ""
    assert amount(eth["quantity"]) == 0
    assert eth["flags"] == "history_incomplete"


async def test_recompute_persists_the_lots_in_seq_order(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """One lot per acquisition, `seq` from 0 in event order, each with its event's kind."""
    user_id, _bitget, _bingx = await plant_history(factory)
    expected = expected_document(history_trades())

    await recompute(factory, user_id, clock)

    tables = await snapshot_tables(factory)
    (header,) = tables["header"]
    lots = tables["lots"]
    assert [row["seq"] for row in lots] == list(range(len(expected["lots"])))
    assert {row["snapshot_id"] for row in lots} == {header["id"]}
    assert [
        (
            row["asset"],
            row["occurred_at"],
            row["source"],
            row["external_id"],
            row["kind"],
            amount(row["quantity"]),
            amount(row["cost_basis"]),
            amount(row["unknown_basis_quantity"]),
        )
        for row in lots
    ] == [
        (
            lot["asset"],
            stored_instant(lot["key"]["occurred_at"]),
            lot["key"]["source"],
            lot["key"]["external_id"],
            "trade",
            amount(lot["quantity"]),
            amount(lot["cost_basis"]),
            amount(lot["unknown_basis_quantity"]),
        )
        for lot in expected["lots"]
    ]
    # By hand: BTC (1001), BTC (1002), BTC of unknown cost (2002), KAS (1003).
    assert [(row["asset"], row["external_id"]) for row in lots] == [
        ("BTC", "1001"),
        ("BTC", "1002"),
        ("BTC", "2002"),
        ("KAS", "1003"),
    ]


async def test_recompute_persists_the_warnings_in_seq_order(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """Both kinds, in event order, `seq` from 0 -- and no column for the trade id."""
    user_id, _bitget, _bingx = await plant_history(factory)
    expected = expected_document(history_trades())

    await recompute(factory, user_id, clock)

    tables = await snapshot_tables(factory)
    warnings = tables["warnings"]
    assert [row["seq"] for row in warnings] == list(range(len(expected["warnings"])))
    assert "external_id" not in warnings[0]
    assert [
        (
            row["kind"],
            row["occurred_at"],
            row["source"],
            row["asset"],
            amount(row["quantity"]),
            row["charged_to"],
        )
        for row in warnings
    ] == [
        (
            warning["type"],
            stored_instant(warning["key"]["occurred_at"]),
            warning["key"]["source"],
            warning["asset"] if warning["type"] == "negative_inventory" else warning["fee_asset"],
            amount(
                warning["shortfall"]
                if warning["type"] == "negative_inventory"
                else warning["quantity"]
            ),
            warning.get("charged_to"),
        )
        for warning in expected["warnings"]
    ]
    kinds = {row["kind"] for row in warnings}
    assert kinds == {"negative_inventory", "unattributed_fee"}
    fee = next(row for row in warnings if row["kind"] == "unattributed_fee")
    assert (fee["asset"], amount(fee["quantity"]), fee["charged_to"]) == (
        "BGB",
        Decimal("0.001"),
        "BTC",
    )


async def test_recompute_reads_only_the_owners_fills(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """Every account the user owns, and no account anybody else does."""
    user_id, _bitget, _bingx = await plant_history(factory)
    async with factory() as session:
        stranger = await plant_owner(session, "someone-else")
        their_account = await plant_account(session, stranger, ExchangeKey.BITGET)
        await plant_fills(session, their_account, [make_fill(5001, at(5), quantity="7")])

    mine = await recompute(factory, user_id, clock)
    theirs = await recompute(factory, stranger, clock)

    assert (mine.event_count, theirs.event_count) == (6, 1)
    headers = {row["user_id"]: row for row in (await snapshot_tables(factory))["header"]}
    assert set(headers) == {user_id, stranger}
    assert headers[stranger]["event_count"] == 1


async def test_a_user_without_fills_gets_an_empty_snapshot(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """Zero events is a result like any other: a header, no positions, zero costs."""
    async with factory() as session:
        user_id = await plant_owner(session)

    report = await recompute(factory, user_id, clock)

    assert (report.outcome, report.event_count) == (RecomputeOutcome.WRITTEN, 0)
    tables = await snapshot_tables(factory)
    assert [row["event_count"] for row in tables["header"]] == [0]
    assert tables["positions"] == tables["lots"] == tables["warnings"] == []


# --------------------------------------------------------------------------------------
# Criterion 1: `raw_payload` is never loaded
# --------------------------------------------------------------------------------------


def test_the_fill_select_names_its_columns_and_never_the_payload() -> None:
    """The compiled statement, as SQLite would receive it."""
    compiled = str(
        select_fills_for_accounting(1).compile(
            dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "raw_payload" not in compiled
    assert "*" not in compiled, "the columns are selected explicitly"
    for column in ("external_trade_id", "base_asset", "quote_asset", "fee_amount", "executed_at"):
        assert column in compiled
    # Nothing is ordered in SQL but the integer id (spec 021, *Loading events*).
    if "ORDER BY" in compiled:
        assert compiled.split("ORDER BY", 1)[1].strip() == "exchange_fills.id"


async def test_no_statement_the_recompute_runs_touches_the_payload(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """Every statement sent to SQLite during a recompute, not only the one compiled above."""
    user_id, _bitget, _bingx = await plant_history(factory)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        del conn, cursor, rest
        statements.append(statement)

    async with factory() as session:
        engine = session.bind
        assert engine is not None
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            await build_accounting_service(session, clock=clock).recompute(user_id)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert any("exchange_fills" in statement for statement in statements), statements
    assert [statement for statement in statements if "raw_payload" in statement] == []


async def test_a_sentinel_payload_reaches_no_snapshot_row(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """A fill carrying a distinctive payload: its text is in no accounting row afterwards."""
    sentinel = "payload-sentinel-" + "Q7" * 8
    async with factory() as session:
        user_id = await plant_owner(session)
        account = await plant_account(session, user_id)
        await plant_fills(
            session, account, [make_fill(3001, at(0), raw_payload=f'{{"note":"{sentinel}"}}')]
        )

    await recompute(factory, user_id, clock)

    async with factory() as session:
        stored_payload = await session.scalar(text("SELECT raw_payload FROM exchange_fills"))
    dumped = await dump_accounting_tables(factory)
    assert sentinel in str(stored_payload), "the control: the payload was stored"
    assert "3001" in dumped, "the control: the dump holds the fill's lot"
    assert sentinel not in dumped


# --------------------------------------------------------------------------------------
# Criterion 1: a stored row that does not convert fails the recompute loudly
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["same_asset", "fee_consumes_received", "rebate_exceeds_given"])
async def test_an_unconvertible_row_fails_the_recompute_and_keeps_the_old_snapshot(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock, shape: str
) -> None:
    """Spec 020's *For #19*: never skipped, never a partial write, and no id in the message."""
    user_id, _bitget, bingx_account = await plant_history(factory)
    await recompute(factory, user_id, clock)
    before = await snapshot_tables(factory)
    assert bingx_account == LEAKY_ACCOUNT_ID
    async with factory() as session:
        await plant_unconvertible_fill(
            session, bingx_account, trade_id=LEAKY_TRADE_ID, shape=shape, executed_at=at(60)
        )
    clock.advance(timedelta(hours=1))

    with pytest.raises(UnconvertibleFillError) as raised:
        await recompute(factory, user_id, clock)

    error = raised.value
    assert isinstance(error, ValueError)
    assert error.exchange_account_id == LEAKY_ACCOUNT_ID
    assert error.external_trade_id == LEAKY_TRADE_ID
    for rendering in (str(error), repr(error), repr(error.args)):
        assert LEAKY_TRADE_ID not in rendering
        assert str(LEAKY_ACCOUNT_ID) not in rendering
    assert isinstance(error.__cause__, (ValueError, TypeError)), "the rule it broke is chained"
    assert await snapshot_tables(factory) == before, "the previous snapshot stands, untouched"


async def unconvertible_error(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> UnconvertibleFillError:
    async with factory() as session:
        user_id = await plant_owner(session)
        account = await plant_account(
            session, user_id, ExchangeKey.BINGX, account_id=LEAKY_ACCOUNT_ID
        )
        await plant_unconvertible_fill(
            session, account, trade_id=LEAKY_TRADE_ID, shape="same_asset"
        )
    with pytest.raises(UnconvertibleFillError) as raised:
        await recompute(factory, user_id, clock)
    return raised.value


async def test_the_unconvertible_error_pickles_and_copies_with_its_attributes(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """The way `ConflictingEventError` does: a round trip keeps the type and both ids."""
    error = await unconvertible_error(factory, clock)

    for twin in (pickle.loads(pickle.dumps(error)), copy.copy(error), copy.deepcopy(error)):  # noqa: S301
        assert type(twin) is UnconvertibleFillError
        assert twin.exchange_account_id == LEAKY_ACCOUNT_ID
        assert twin.external_trade_id == LEAKY_TRADE_ID
        assert str(twin) == str(error)


async def test_the_unconvertible_message_is_the_same_whichever_fill_it_is(
    tmp_path: Path, clock: SettableClock
) -> None:
    """A fixed message: two different ids, two different shapes, one text."""
    first_dir, second_dir = tmp_path / "one", tmp_path / "two"
    first_dir.mkdir()
    second_dir.mkdir()
    messages = []
    for directory, trade_id, shape in (
        (first_dir, "tid-one", "same_asset"),
        (second_dir, "tid-two", "rebate_exceeds_given"),
    ):
        async with migrated_sessionmaker(directory) as built:
            async with built() as session:
                user_id = await plant_owner(session)
                account = await plant_account(session, user_id)
                await plant_unconvertible_fill(session, account, trade_id=trade_id, shape=shape)
            with pytest.raises(UnconvertibleFillError) as raised:
                await recompute(built, user_id, clock)
            messages.append(str(raised.value))

    assert messages[0] == messages[1]
    assert messages[0].strip()


# --------------------------------------------------------------------------------------
# Criterion 1: a value no column can hold rolls the whole write back
# --------------------------------------------------------------------------------------


async def test_a_write_the_column_refuses_rolls_back_and_keeps_the_old_snapshot(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """Spec 019, *Risks*: 1 BTC for 9E19 with a 9E19 fee is a basis of 1.8E20.

    Every amount is legal on its own, `replay` returns the basis unbounded, and
    `NumericText(18)` refuses it on the way in. The delete of the old header and the inserts
    are one transaction, so the refusal leaves the previous snapshot exactly as it was.
    """
    user_id, bitget_account, _bingx = await plant_history(factory)
    await recompute(factory, user_id, clock)
    before = await snapshot_tables(factory)
    absurd = "90000000000000000000"
    async with factory() as session:
        await plant_fills(
            session,
            bitget_account,
            [
                make_fill(
                    1999,
                    at(70),
                    quantity="1",
                    price=absurd,
                    quote_quantity=absurd,
                    fee_amount=absurd,
                    fee_asset="USDT",
                )
            ],
        )
    clock.advance(timedelta(hours=1))

    with pytest.raises(Exception) as raised:  # noqa: PT011 -- which wrapper is the driver's
        await recompute(factory, user_id, clock)

    assert not isinstance(raised.value, UnconvertibleFillError)
    assert absurd not in str(raised.value), "the refusal quotes no amount"
    assert await snapshot_tables(factory) == before


# --------------------------------------------------------------------------------------
# Criterion 2: an unchanged fingerprint writes nothing
# --------------------------------------------------------------------------------------


async def test_a_second_recompute_is_unchanged_and_writes_nothing(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """Same row ids, same `computed_at`, though the clock has moved on."""
    user_id, _bitget, _bingx = await plant_history(factory)
    first = await recompute(factory, user_id, clock)
    before = await snapshot_tables(factory)
    clock.advance(timedelta(hours=3))

    second = await recompute(factory, user_id, clock)

    assert first.outcome is RecomputeOutcome.WRITTEN
    assert second.outcome is RecomputeOutcome.UNCHANGED
    assert second.event_count == 6
    assert await snapshot_tables(factory) == before


async def test_an_unchanged_recompute_issues_no_write(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """Not a delete-and-reinsert of identical rows: no `INSERT`, `UPDATE` or `DELETE` at all."""
    user_id, _bitget, _bingx = await plant_history(factory)
    await recompute(factory, user_id, clock)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        del conn, cursor, rest
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    async with factory() as session:
        engine = session.bind
        assert engine is not None
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            report = await build_accounting_service(session, clock=clock).recompute(user_id)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert report.outcome is RecomputeOutcome.UNCHANGED
    assert statements, "the control: the recompute did read"
    assert set(statements) <= {"SELECT", "PRAGMA"}, statements


async def test_a_new_fill_is_written(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    user_id, bitget_account, _bingx = await plant_history(factory)
    await recompute(factory, user_id, clock)
    async with factory() as session:
        await plant_fills(session, bitget_account, [make_fill(1005, at(90), quantity="0.25")])
    clock.advance(timedelta(minutes=15))

    report = await recompute(factory, user_id, clock)

    assert (report.outcome, report.event_count) == (RecomputeOutcome.WRITTEN, 7)
    (header,) = (await snapshot_tables(factory))["header"]
    assert header["event_count"] == 7
    assert header["computed_at"] == clock.moment.strftime("%Y-%m-%d %H:%M:%S.%f")


def patch_engine_version(monkeypatch: pytest.MonkeyPatch, value: int) -> list[str]:
    """Rebind `ENGINE_VERSION` wherever an accounting module holds it (see `test_fingerprint`)."""
    patched = []
    for name, module in list(sys.modules.items()):
        if name.startswith("portfolio.domain.accounting") and hasattr(module, "ENGINE_VERSION"):
            monkeypatch.setattr(module, "ENGINE_VERSION", value)
            patched.append(name)
    return patched


async def test_an_engine_upgrade_is_written_over_the_same_fills(
    factory: async_sessionmaker[AsyncSession],
    clock: SettableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ENGINE_VERSION` is in the fingerprint, so a fixed engine never keeps a stale snapshot."""
    user_id, _bitget, _bingx = await plant_history(factory)
    await recompute(factory, user_id, clock)
    (old,) = (await snapshot_tables(factory))["header"]

    assert patch_engine_version(monkeypatch, ENGINE_VERSION + 1)
    report = await recompute(factory, user_id, clock)

    assert report.outcome is RecomputeOutcome.WRITTEN
    (new,) = (await snapshot_tables(factory))["header"]
    assert new["engine_version"] == ENGINE_VERSION + 1
    assert new["input_fingerprint"] != old["input_fingerprint"]


# --------------------------------------------------------------------------------------
# Criterion 7 and the price lookup
# --------------------------------------------------------------------------------------


def test_priced_assets_are_the_chain_assets() -> None:
    """Spec 021: only a chain's asset is priced, and every chain asset has a USD price.

    The service decides `unsupported_pair` from `CHAIN_ASSET_SYMBOLS` because a request path
    may not import `SUPPORTED_PAIRS`; the two declarations are held together here, the way
    `domain/currencies.py`'s copies are.
    """
    chain_assets = set(CHAIN_ASSET_SYMBOLS.values())

    assert chain_assets == PRICED_ASSETS, "the set the service decides `unsupported_pair` by"
    assert {asset for asset, _currency in SUPPORTED_PAIRS} == chain_assets
    assert {asset for asset, currency in SUPPORTED_PAIRS if currency == "USD"} == chain_assets
    assert {ChainKey(key).asset_symbol for key in ChainKey} == chain_assets


def test_venue_and_chain_symbols_are_both_upper_case() -> None:
    """Spec 021, *Risks*: a case mismatch would leave an asset unpriced, with nothing failing.

    A chain's symbol, and the base asset each venue's documented payload parses to, compare
    as exact strings -- so both sides are pinned upper case, and equal for BTC and KAS.
    """
    assert all(symbol == symbol.upper() for symbol in CHAIN_ASSET_SYMBOLS.values())
    from_bingx = bingx.parse_fill(
        {
            "symbol": "BTC-USDT",
            "id": 36767057,
            "orderId": 1745362930595004400,
            "price": "46820.155",
            "qty": "0.1430254",
            "quoteQty": "6696.471396937",
            "commission": "-0.000046483255",
            "commissionAsset": "BTC",
            "time": 1704961925000,
            "isBuyer": True,
            "isMaker": False,
        }
    )
    from_bitget = bitget.parse_symbol_info(
        [{"symbol": "KASUSDT", "baseCoin": "KAS", "quoteCoin": "USDT", "status": "online"}],
        symbol="KASUSDT",
    )

    assert from_bingx.base_asset == CHAIN_ASSET_SYMBOLS[ChainKey.BITCOIN] == "BTC"
    assert from_bitget.base_asset == CHAIN_ASSET_SYMBOLS[ChainKey.KASPA] == "KAS"


async def test_positions_price_chain_assets_and_name_why_the_others_are_not(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """BTC priced from its row, KAS a chain asset with no row, ETH and BGB no chain's at all.

    Staleness is decided against the service's clock: the BTC row is two hours old.
    """
    user_id, _bitget, _bingx = await plant_history(factory)
    async with factory() as session:
        # KAS bought again, so a KAS holding is open and unpriced.
        account = await session.scalar(
            text("SELECT id FROM exchange_accounts WHERE exchange_key = 'bitget'")
        )
        await plant_fills(
            session,
            int(account),
            [
                make_fill(
                    1010,
                    at(100),
                    symbol="KASUSDT",
                    base_asset="KAS",
                    quantity="10",
                    price="0.1",
                    quote_quantity="1",
                    fee_amount="0",
                    fee_asset=None,
                )
            ],
        )
        await plant_price(
            session, symbol="BTC", amount=Decimal(60000), as_of=COMPUTED_AT - timedelta(hours=2)
        )
    await recompute(factory, user_id, clock)

    async with factory() as session:
        view = await build_accounting_service(session, clock=clock).positions(user_id)

    by_asset = {entry.value.position.asset: entry for entry in view.positions}
    assert list(by_asset) == ["BGB", "BTC", "ETH", "KAS"]
    btc = by_asset["BTC"]
    assert btc.price is not None
    assert btc.price.amount == Decimal(60000)
    assert btc.price.stale is True
    # 1.2999 BTC at 60000 = 77994, stale or not.
    assert btc.value.market_value == Decimal(77994)
    assert btc.value.market_value_unavailable_reason is None
    kas = by_asset["KAS"]
    assert kas.price is None
    assert kas.value.market_value is None
    assert str(kas.value.market_value_unavailable_reason) == "never_fetched"
    for asset in ("BGB", "ETH"):
        entry = by_asset[asset]
        assert entry.price is None
        # Nothing held: a zero, not a reason.
        assert entry.value.market_value == 0
    assert str(view.quote_currency) == "USD"
    assert view.computed_at == COMPUTED_AT
    assert view.event_count == 7


async def test_an_open_holding_of_a_non_chain_asset_is_unsupported_without_a_lookup(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """ETH bought and held: `unsupported_pair`, not `never_fetched`, even with an ETH row absent."""
    async with factory() as session:
        user_id = await plant_owner(session)
        account = await plant_account(session, user_id)
        await plant_fills(
            session,
            account,
            [
                make_fill(
                    4001,
                    at(0),
                    symbol="ETHUSDT",
                    base_asset="ETH",
                    quantity="2",
                    price="2500",
                    quote_quantity="5000",
                    fee_amount="0",
                    fee_asset=None,
                )
            ],
        )
    await recompute(factory, user_id, clock)

    async with factory() as session:
        view = await build_accounting_service(session, clock=clock).positions(user_id)

    (eth,) = view.positions
    assert eth.value.market_value is None
    assert str(eth.value.market_value_unavailable_reason) == "unsupported_pair"
    assert [(entry.asset, str(entry.reason)) for entry in view.totals.excluded] == [
        ("ETH", "unpriced")
    ]


async def test_positions_without_a_snapshot_are_empty_zeros(
    factory: async_sessionmaker[AsyncSession], clock: SettableClock
) -> None:
    """No recompute has run: no timestamp, nothing listed, zero totals, no percentage."""
    async with factory() as session:
        user_id = await plant_owner(session)
        view = await build_accounting_service(session, clock=clock).positions(user_id)

    assert view.computed_at is None
    assert view.event_count == 0
    assert view.positions == ()
    assert view.warnings == ()
    assert view.unallocated_costs == 0
    assert view.totals.total_invested == view.totals.market_value == 0
    assert view.totals.unrealized_pnl == view.totals.realized_pnl == 0
    assert view.totals.unrealized_return_pct is None
