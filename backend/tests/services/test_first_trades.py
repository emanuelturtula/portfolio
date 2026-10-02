"""Criteria 1 to 3 of #111 (spec 027): when the imported history of each asset begins.

Two layers, in this order.

**`first_trades_of`, pure.** No database and no fixture: records are built by hand, so a row
no venue would send -- a fee with no asset, a side that is neither `buy` nor `sell` -- can be
handed to it. The rule is the spec's, clause by clause:

* an asset takes part in a fill as its **base** asset, as its **quote** asset, or as its
  **fee** asset when the fee amount is not zero;
* the cash assets, `DEFAULT_CASH_ASSETS`, are left out in every role;
* the instant is that of the **earliest** such fill, whatever order the fills arrive in;
* the answer is sorted by asset, in **code-point** order;
* no fills, no entries.

The examples are worked by hand beside their literals. Behind them stands an oracle written
from those five clauses asset by asset -- it keeps no running minimum and no dictionary, so it
cannot share the reducer's bug -- and Hypothesis compares the two over generated histories,
which is where the cases nobody writes down live: a zero fee in the asset that is also the
base, a tie between a fee and a quote, a cash asset in all three roles of one fill.

**`AccountingService.first_trades`, over a real SQLite file migrated to head.** What the
pure tests cannot see: the owner filter, the eighteen-place zero a fee comes back as, the
adjustments table left unread, and the statement itself. `executed_at` is `TEXT` in SQLite, so
a `MIN()`, an `ORDER BY` or a comparison on it would compare strings (rule 2 covers datetimes
stored as text as well as money): the statements one read issues are recorded and held to the
one the recompute already issues.

## Nothing here is sensitive

Assets are the venues' public symbols, trade ids are small integers or obviously synthetic
text, and no address, key or hostname appears.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from itertools import pairwise, permutations
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import event, text

from portfolio.domain.accounting import DEFAULT_CASH_ASSETS
from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.repositories.exchanges import AccountingFillRecord, ExchangeFillRepository
from portfolio.services import accounting as accounting_module
from portfolio.services.accounting import (
    FirstTrade,
    UnconvertibleFillError,
    build_accounting_service,
    first_trades_of,
)
from tests.accounting_harness import (
    INGESTED_AT,
    fixed,
    plant_account,
    plant_fills,
    plant_owner,
    plant_unconvertible_fill,
    snapshot_tables,
)
from tests.adjustments_harness import plant_adjustment
from tests.balance_harness import sqlite_timestamp
from tests.exchange_sync_harness import make_fill
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from random import Random

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: The first instant of every hand-written history: the spec's own example, to the second.
START: Final = datetime(2025, 3, 1, 10, 0, 37, tzinfo=UTC)

#: The cash assets, written out. `test_the_cash_assets_are_the_two_this_file_names` holds
#: them to `DEFAULT_CASH_ASSETS`, so a third one added there fails here and is argued.
CASH: Final = ("USDC", "USDT")

#: Two offsets a venue's clock is never in, for "the same instant, spelled differently".
KOLKATA: Final = timezone(timedelta(hours=5, minutes=30))
BUENOS_AIRES: Final = timezone(timedelta(hours=-3))


def minute(offset: int) -> datetime:
    """`START` plus `offset` minutes."""
    return START + timedelta(minutes=offset)


def record(
    offset: int = 0,
    *,
    base: str = "BTC",
    quote: str = "USDT",
    fee: str = "0",
    fee_asset: str | None = None,
    side: str = "buy",
    executed_at: datetime | None = None,
    fill_id: int = 1,
) -> AccountingFillRecord:
    """One stored fill as the accounting reads it, built by hand and validated by nothing."""
    return AccountingFillRecord(
        id=fill_id,
        exchange_account_id=1,
        exchange_key=ExchangeKey.BITGET,
        external_trade_id=f"synthetic-{fill_id}",
        external_order_id=None,
        symbol=f"{base}{quote}",
        base_asset=base,
        quote_asset=quote,
        side=side,
        quantity=Decimal(1),
        price=Decimal(1),
        quote_quantity=Decimal(1),
        quote_quantity_derived=False,
        fee_amount=Decimal(fee),
        fee_asset=fee_asset,
        executed_at=minute(offset) if executed_at is None else executed_at,
        ingested_at=INGESTED_AT,
    )


def pairs(first_trades: Sequence[FirstTrade]) -> list[tuple[str, datetime]]:
    """The answer as `(asset, instant)` pairs, in the order it was given."""
    return [(entry.asset, entry.first_trade_at) for entry in first_trades]


def reduced(fills: Sequence[AccountingFillRecord]) -> list[tuple[str, datetime]]:
    return pairs(first_trades_of(fills))


# --------------------------------------------------------------------------------------
# The shape of the answer
# --------------------------------------------------------------------------------------


def test_no_fills_is_an_empty_answer() -> None:
    assert first_trades_of([]) == ()


def test_an_entry_is_an_asset_and_an_aware_instant_and_nothing_else() -> None:
    """Two fields: a third would be a figure about the owner's trades nobody asked for."""
    (entry,) = first_trades_of([record()])

    assert is_dataclass(entry)
    assert [field.name for field in fields(entry)] == ["asset", "first_trade_at"]
    assert entry == FirstTrade(asset="BTC", first_trade_at=START)
    assert entry.first_trade_at.utcoffset() == timedelta(0)


def test_the_reducer_is_a_plain_function_that_leaves_its_input_alone() -> None:
    """Pure: synchronous, the same answer twice, and the list it was given is untouched."""
    fills = [record(2, fill_id=1), record(0, base="ETH", fill_id=2)]
    before = list(fills)

    assert not inspect.iscoroutinefunction(first_trades_of)
    assert first_trades_of(fills) == first_trades_of(fills)
    assert fills == before


# --------------------------------------------------------------------------------------
# The three roles
# --------------------------------------------------------------------------------------


def test_the_base_asset_takes_part() -> None:
    """BTC bought with USDT, no fee: BTC, and only BTC, because USDT is cash."""
    assert reduced([record(base="BTC", quote="USDT")]) == [("BTC", START)]


def test_the_quote_asset_takes_part() -> None:
    """ETH bought with BTC: both begin at that fill."""
    assert reduced([record(base="ETH", quote="BTC")]) == [("BTC", START), ("ETH", START)]


def test_the_fee_asset_takes_part_when_the_fee_is_not_zero() -> None:
    """BTC for USDT, the fee paid in BGB: BGB's history begins there too."""
    answer = reduced([record(fee="0.001", fee_asset="BGB")])

    assert answer == [("BGB", START), ("BTC", START)]


def test_one_fill_can_begin_three_assets() -> None:
    """ETH for BTC with a fee in BGB: three entries, one instant, sorted."""
    answer = reduced([record(3, base="ETH", quote="BTC", fee="0.5", fee_asset="BGB")])

    assert answer == [("BGB", minute(3)), ("BTC", minute(3)), ("ETH", minute(3))]


def test_an_asset_in_two_roles_of_one_fill_is_listed_once() -> None:
    """The fee of a BTC buy paid in BTC: one BTC entry, not two."""
    assert reduced([record(fee="0.0005", fee_asset="BTC")]) == [("BTC", START)]


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_a_buy_and_a_sell_count_alike(side: str) -> None:
    """The side does not decide who took part: a sale of ETH for BTC begins both."""
    answer = reduced([record(base="ETH", quote="BTC", side=side)])

    assert answer == [("BTC", START), ("ETH", START)]


# --------------------------------------------------------------------------------------
# A zero fee moves nothing
# --------------------------------------------------------------------------------------

#: Every spelling of zero a `Decimal` has, the stored one -- eighteen places -- included.
ZEROS: Final = ("0", "0.0", "0.000000000000000000", "0E-18", "-0", "0E+3")


@pytest.mark.parametrize("zero", ZEROS)
def test_a_zero_fee_does_not_count(zero: str) -> None:
    """A fee of nothing in BGB: BGB took no part, however the nothing is spelled."""
    assert reduced([record(fee=zero, fee_asset="BGB")]) == [("BTC", START)]


@pytest.mark.parametrize(
    "fee",
    [
        "0.001",
        "1",
        # The smallest amount a fill column holds.
        "0.000000000000000001",
        # A rebate: the venue paid the owner. Something moved, so the asset took part.
        "-0.001",
        "-0.000000000000000001",
    ],
)
def test_a_fee_that_is_not_zero_counts_whatever_its_sign_or_size(fee: str) -> None:
    assert reduced([record(fee=fee, fee_asset="BGB")]) == [("BGB", START), ("BTC", START)]


def test_a_zero_fee_leaves_the_base_and_the_quote_counted() -> None:
    """Only the fee's role is dropped: the fill itself still took place."""
    answer = reduced([record(base="ETH", quote="BTC", fee="0", fee_asset="BGB")])

    assert answer == [("BTC", START), ("ETH", START)]


def test_a_zero_fee_in_the_base_asset_does_not_hide_the_base() -> None:
    """BTC is the base and the asset of a zero fee: it is listed, as the base."""
    assert reduced([record(fee="0", fee_asset="BTC")]) == [("BTC", START)]


def test_an_earlier_zero_fee_does_not_date_the_asset() -> None:
    """BGB named by a zero fee at 10:00 and paid for real at 10:05: its history begins at
    10:05."""
    fills = [
        record(0, fee="0", fee_asset="BGB", fill_id=1),
        record(5, fee="0.01", fee_asset="BGB", fill_id=2),
    ]

    assert reduced(fills) == [("BGB", minute(5)), ("BTC", START)]


def test_a_fill_with_no_fee_asset_names_no_third_asset() -> None:
    assert reduced([record(fee="0", fee_asset=None)]) == [("BTC", START)]


def test_a_fee_amount_with_no_asset_is_not_an_entry_and_not_an_error() -> None:
    """A row no venue sends -- an amount and no asset to name it in -- written by hand.

    The reducer reads the record's own columns and converts nothing, so it does not raise,
    and there is no asset to list: every entry's `asset` is text.
    """
    answer = first_trades_of([record(fee="0.5", fee_asset=None)])

    assert pairs(answer) == [("BTC", START)]
    assert all(isinstance(entry.asset, str) for entry in answer)


# --------------------------------------------------------------------------------------
# Cash assets are left out, in every role
# --------------------------------------------------------------------------------------


def test_the_cash_assets_are_the_two_this_file_names() -> None:
    """The control on `CASH`: the literals below are the constant, no more and no fewer."""
    assert frozenset(CASH) == DEFAULT_CASH_ASSETS


@pytest.mark.parametrize("cash", CASH)
def test_a_cash_asset_is_absent_as_the_quote(cash: str) -> None:
    assert reduced([record(base="BTC", quote=cash)]) == [("BTC", START)]


@pytest.mark.parametrize("cash", CASH)
def test_a_cash_asset_is_absent_as_the_fee(cash: str) -> None:
    """A real, non-zero fee in cash, on a pair that has no cash in it."""
    answer = reduced([record(base="ETH", quote="BTC", fee="12.5", fee_asset=cash)])

    assert answer == [("BTC", START), ("ETH", START)]


@pytest.mark.parametrize("cash", CASH)
def test_a_cash_asset_is_absent_as_the_base(cash: str) -> None:
    """Cash sold for BTC: BTC begins there, the cash does not."""
    assert reduced([record(base=cash, quote="BTC", side="sell")]) == [("BTC", START)]


def test_a_fill_of_nothing_but_cash_begins_nothing() -> None:
    """A stablecoin conversion with its fee in a stablecoin: no asset at all."""
    assert reduced([record(base="USDC", quote="USDT", fee="0.1", fee_asset="USDT")]) == []


def test_only_the_cash_assets_are_left_out() -> None:
    """EUR is money and is not a cash asset of this engine, so it is listed like any other."""
    assert reduced([record(base="BTC", quote="EUR")]) == [("BTC", START), ("EUR", START)]


# --------------------------------------------------------------------------------------
# The earliest fill wins
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_the_earliest_of_several_fills_wins_in_any_input_order(order: tuple[int, ...]) -> None:
    """Three BTC fills, at 10:00, 10:07 and 10:30, fed in each of their six orders."""
    fills = [
        record(0, fill_id=1),
        record(7, fill_id=2),
        record(30, fill_id=3),
    ]

    assert reduced([fills[index] for index in order]) == [("BTC", START)]


@pytest.mark.parametrize("earliest", ["base", "quote", "fee"])
@pytest.mark.parametrize("reverse", [False, True])
def test_the_earliest_fill_wins_whichever_role_the_asset_had_in_it(
    earliest: str, reverse: bool
) -> None:
    """BTC is the base of one fill, the quote of another and the fee of a third.

    Whichever of the three is at 10:00 dates it, the other two being at 10:10 and 10:20.
    """
    others = iter((10, 20))
    offsets = {role: 0 if role == earliest else next(others) for role in ("base", "quote", "fee")}
    fills = [
        record(offsets["base"], base="BTC", quote="USDT", fill_id=1),
        record(offsets["quote"], base="ETH", quote="BTC", fill_id=2),
        record(offsets["fee"], base="KAS", quote="USDT", fee="0.0001", fee_asset="BTC", fill_id=3),
    ]
    if reverse:
        fills.reverse()

    assert dict(reduced(fills))["BTC"] == START
    assert dict(reduced(fills)) == {
        "BTC": START,
        "ETH": minute(offsets["quote"]),
        "KAS": minute(offsets["fee"]),
    }


def test_each_asset_has_its_own_earliest_fill() -> None:
    """ETH begins at 10:02 and BTC at 10:01: neither borrows the other's instant."""
    fills = [
        record(9, base="BTC", fill_id=1),
        record(2, base="ETH", fill_id=2),
        record(1, base="BTC", fill_id=3),
        record(4, base="ETH", fill_id=4),
    ]

    assert reduced(fills) == [("BTC", minute(1)), ("ETH", minute(2))]


def test_two_fills_at_one_instant_are_that_instant() -> None:
    """A tie is not a choice: both fills say the same thing."""
    fills = [record(4, fill_id=1), record(4, fill_id=2), record(6, fill_id=3)]

    assert reduced(fills) == [("BTC", minute(4))]
    assert reduced(fills[::-1]) == [("BTC", minute(4))]


def test_the_instant_keeps_its_fraction_of_a_second() -> None:
    """A venue reports milliseconds, and the earlier of two fills in one second wins."""
    early = START + timedelta(milliseconds=123)
    late = START + timedelta(milliseconds=124)
    fills = [record(executed_at=late, fill_id=1), record(executed_at=early, fill_id=2)]

    assert reduced(fills) == [("BTC", early)]


def test_instants_are_compared_as_instants_not_as_their_spelling() -> None:
    """Two clocks, one history. 15:25 in Kolkata is 09:55 UTC, before 10:00 UTC, and it reads
    later than `10:00` as text; 06:50 in Buenos Aires is 09:50 UTC, earlier still."""
    kolkata = datetime(2025, 3, 1, 15, 25, tzinfo=KOLKATA)
    buenos_aires = datetime(2025, 3, 1, 6, 50, tzinfo=BUENOS_AIRES)
    utc = datetime(2025, 3, 1, 10, 0, tzinfo=UTC)

    for fills in permutations(
        [
            record(executed_at=utc, fill_id=1),
            record(executed_at=kolkata, fill_id=2),
            record(executed_at=buenos_aires, base="ETH", fill_id=3),
            record(executed_at=utc, base="ETH", fill_id=4),
        ]
    ):
        answer = reduced(list(fills))
        assert answer == [
            ("BTC", datetime(2025, 3, 1, 9, 55, tzinfo=UTC)),
            ("ETH", datetime(2025, 3, 1, 9, 50, tzinfo=UTC)),
        ]


# --------------------------------------------------------------------------------------
# Sorted by asset, in code-point order
# --------------------------------------------------------------------------------------

#: Digits, then upper case, then lower case: the order of the code points, written by hand.
#: A case-insensitive sort would put `aUSD` first and `kBONK` before `ZRX`; a numeric-aware
#: one would put `1INCH` before `1000SATS`.
CODE_POINT_ORDER: Final = ("1000SATS", "1INCH", "AAVE", "BTC", "BTC2", "ZRX", "aUSD", "kBONK")


def test_the_hand_written_order_is_the_code_point_order() -> None:
    """The control on the literal above, by the rule itself and not by `sorted` on text."""
    by_code_points = sorted(CODE_POINT_ORDER, key=lambda name: [ord(char) for char in name])

    assert list(CODE_POINT_ORDER) == by_code_points


@pytest.mark.parametrize("step", [1, -1])
def test_the_answer_is_sorted_by_asset_in_code_point_order(step: int) -> None:
    """One fill per asset, fed in that order and in the opposite one. The instants run
    against the names, so an answer sorted by instant would be the reverse."""
    fills = [
        record(len(CODE_POINT_ORDER) - index, base=asset, fill_id=index)
        for index, asset in enumerate(CODE_POINT_ORDER)
    ]

    answer = reduced(fills[::step])

    assert [asset for asset, _instant in answer] == list(CODE_POINT_ORDER)
    assert dict(answer)["1000SATS"] == minute(len(CODE_POINT_ORDER))


def test_the_order_is_by_asset_across_roles() -> None:
    """The base, quote and fee of one fill come out by name, not base first."""
    answer = reduced([record(base="ZRX", quote="AAVE", fee="1", fee_asset="1000SATS")])

    assert [asset for asset, _instant in answer] == ["1000SATS", "AAVE", "ZRX"]


# --------------------------------------------------------------------------------------
# It converts nothing, so no stored fill can make it raise
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "broken",
    [
        # A side the column's CHECK would refuse and `FillSide` cannot hold.
        record(base="ETH", quote="BTC", side="neither"),
        # Spec 020's three shapes `Trade` refuses, as rows stored before #99 would be.
        record(base="BTC", quote="BTC"),
        record(base="BTC", quote="ETH", fee="1", fee_asset="BTC"),
        record(base="BTC", quote="ETH", side="buy", fee="-1", fee_asset="ETH"),
    ],
    ids=["unknown_side", "same_asset", "fee_consumes_received", "rebate_exceeds_given"],
)
def test_a_row_the_engine_refuses_still_answers(broken: AccountingFillRecord) -> None:
    """Each of these fails a recompute. None of them fails this read, and each is counted."""
    answer = dict(reduced([broken]))

    assert answer
    assert set(answer) == {broken.base_asset, broken.quote_asset}
    assert set(answer.values()) == {START}


@pytest.mark.parametrize("fee", ["NaN", "-NaN", "sNaN", "Infinity", "-Infinity"])
def test_a_fee_that_is_not_a_number_does_not_make_it_raise(fee: str) -> None:
    """The amounts a `Decimal` can hold that no arithmetic survives, as a hand-edited row
    would carry them. A comparison with a signalling NaN raises; this read makes none.

    The base and the quote are listed whatever the fee is. None of these fees is zero --
    ruling R7: a fee that is not a number is "not zero" -- so the fee asset is listed too.
    """
    answer = dict(reduced([record(base="ETH", quote="BTC", fee=fee, fee_asset="BGB")]))

    assert answer == {"BGB": START, "BTC": START, "ETH": START}


def test_a_row_with_every_hostile_value_at_once_still_answers() -> None:
    """An unknown side, the same asset on both sides, a NaN fee with no asset to name it in,
    and amounts no venue reports: one entry, and no error."""
    hostile = AccountingFillRecord(
        id=1,
        exchange_account_id=1,
        exchange_key=ExchangeKey.BITGET,
        external_trade_id="synthetic-hostile",
        external_order_id=None,
        symbol="",
        base_asset="BTC",
        quote_asset="BTC",
        side="",
        quantity=Decimal("-1"),
        price=Decimal("NaN"),
        quote_quantity=Decimal(0),
        quote_quantity_derived=False,
        fee_amount=Decimal("sNaN"),
        fee_asset=None,
        executed_at=START,
        ingested_at=INGESTED_AT,
    )

    assert reduced([hostile, record(3, base="ETH", fill_id=2)]) == [
        ("BTC", START),
        ("ETH", minute(3)),
    ]


# --------------------------------------------------------------------------------------
# Any history: the reducer against an oracle written from the spec
# --------------------------------------------------------------------------------------

#: Few enough that every asset meets every role, and the roles collide within one fill.
ASSETS: Final = ("BTC", "ETH", "KAS", "BGB", "EUR", "1000SATS", "aUSD", "USDT", "USDC")
FEES: Final = ("0", "0.000000000000000000", "-0", "0.000000000000000001", "0.5", "-0.25")
ZONES: Final = (UTC, KOLKATA, BUENOS_AIRES)

PROPERTY: Final = settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)


@st.composite
def records(draw: st.DrawFn) -> AccountingFillRecord:
    """Any stored fill: every role drawn freely, so the base may be the quote or the fee.

    Seven minutes and three half-seconds give ties and near-ties; the zone changes how an
    instant is spelled and never which instant it is.
    """
    moment = minute(draw(st.integers(0, 6))) + timedelta(milliseconds=500 * draw(st.integers(0, 2)))
    return record(
        base=draw(st.sampled_from(ASSETS)),
        quote=draw(st.sampled_from(ASSETS)),
        fee=draw(st.sampled_from(FEES)),
        fee_asset=draw(st.sampled_from((None, *ASSETS))),
        side=draw(st.sampled_from(("buy", "sell"))),
        executed_at=moment.astimezone(draw(st.sampled_from(ZONES))),
        fill_id=draw(st.integers(1, 10_000)),
    )


def took_part(asset: str, fill: AccountingFillRecord) -> bool:
    """Spec 027: as the base, as the quote, or as the fee when the fee amount is not zero."""
    if fill.base_asset == asset or fill.quote_asset == asset:
        return True
    return fill.fee_asset == asset and fill.fee_amount != 0


def oracle(fills: Sequence[AccountingFillRecord]) -> list[tuple[str, datetime]]:
    """The spec's five clauses, asked once per asset. No running minimum and no dictionary.

    Every name any fill mentions is a candidate; a cash asset is dropped; the others are
    asked for the fills they took part in, and the earliest of those -- found by a scan that
    keeps whichever instant nothing else is before -- is the answer. The order is by the
    names' code points, compared as lists of integers.
    """
    mentioned = {
        name
        for fill in fills
        for name in (fill.base_asset, fill.quote_asset, fill.fee_asset)
        if name is not None
    }
    answer: list[tuple[str, datetime]] = []
    for asset in sorted(mentioned, key=lambda name: [ord(char) for char in name]):
        if asset in ("USDC", "USDT"):
            continue
        instants = [fill.executed_at for fill in fills if took_part(asset, fill)]
        firsts = [instant for instant in instants if not any(other < instant for other in instants)]
        if firsts:
            answer.append((asset, firsts[0]))
    return answer


@PROPERTY
@given(fills=st.lists(records(), max_size=30))
def test_any_history_reduces_to_what_the_spec_says(fills: list[AccountingFillRecord]) -> None:
    assert reduced(fills) == oracle(fills)


@PROPERTY
@given(fills=st.lists(records(), max_size=30), shuffler=st.randoms(use_true_random=False))
def test_the_answer_does_not_depend_on_the_order_the_fills_were_read_in(
    fills: list[AccountingFillRecord], shuffler: Random
) -> None:
    shuffled = list(fills)
    shuffler.shuffle(shuffled)

    assert reduced(shuffled) == reduced(fills)
    assert reduced(fills[::-1]) == reduced(fills)


@PROPERTY
@given(fills=st.lists(records(), max_size=30))
def test_every_entry_is_a_fill_the_asset_took_part_in(fills: list[AccountingFillRecord]) -> None:
    """What a reader of the answer would check: sorted, unique, no cash, and every instant
    is the time of a real fill of that asset with none of its fills before it."""
    answer = reduced(fills)
    names = [asset for asset, _instant in answer]

    assert all(
        [ord(char) for char in left] < [ord(char) for char in right]
        for left, right in pairwise(names)
    ), "strictly ascending by code point, so no asset appears twice"
    assert not set(names) & {"USDC", "USDT"}
    for asset, instant in answer:
        instants = [fill.executed_at for fill in fills if took_part(asset, fill)]
        assert instant in instants
        assert all(instant <= other for other in instants)
        assert instant.utcoffset() is not None


@PROPERTY
@given(known=st.lists(records(), max_size=20), more=st.lists(records(), max_size=20))
def test_more_history_never_removes_an_asset_and_only_moves_a_date_earlier(
    known: list[AccountingFillRecord], more: list[AccountingFillRecord]
) -> None:
    """Importing older fills can only bring an asset's beginning forward in time."""
    before = dict(reduced(known))
    after = dict(reduced([*more, *known]))

    assert set(before) <= set(after)
    assert set(after) == set(before) | set(dict(reduced(more)))
    for asset, instant in before.items():
        assert after[asset] <= instant


# --------------------------------------------------------------------------------------
# The service, over a real database
# --------------------------------------------------------------------------------------


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


async def first_trades(
    factory: async_sessionmaker[AsyncSession], user_id: int
) -> list[tuple[str, datetime]]:
    """One read over a session of its own, as a request makes it."""
    async with factory() as session:
        return pairs(await build_accounting_service(session).first_trades(user_id))


async def plant_two_venues(factory: async_sessionmaker[AsyncSession]) -> int:
    """An owner with a Bitget and a BingX account, and a history that uses every clause.

    | Venue | When | Fill | Fee | Begins |
    |---|---|---|---|---|
    | Bitget | 10:20 | buy BTC for USDT | 0 | nothing: BTC was the quote at 10:05 |
    | Bitget | 10:05 | sell ETH for BTC | 0.01 BGB | BTC, as the quote |
    | Bitget | 10:01 | buy KAS for USDT | 0 KAS | KAS, as the base |
    | Bitget | 10:02 | buy SOL for USDC | 3 USDT | SOL |
    | BingX | 10:09 | buy BTC for USDT | 0 BNB | nothing; BNB never begins |
    | BingX | 10:03 | buy ETH for USDT | -0.1 BGB, a rebate | ETH, and BGB by the rebate |

    So: BGB 10:03, BTC 10:05, ETH 10:03, KAS 10:01, SOL 10:02, and no BNB, USDC or USDT. Each
    role decides one date: BTC as a quote, BGB as a fee -- a rebate, ruling R1 -- and the rest
    as a base; and ETH's date comes from the second venue.
    """
    async with factory() as session:
        user_id = await plant_owner(session)
        bitget = await plant_account(session, user_id, ExchangeKey.BITGET)
        bingx = await plant_account(session, user_id, ExchangeKey.BINGX)
        await plant_fills(
            session,
            bitget,
            [
                make_fill(1001, minute(20), fee_amount="0", fee_asset=None),
                make_fill(
                    1002,
                    minute(5),
                    symbol="ETHBTC",
                    base_asset="ETH",
                    quote_asset="BTC",
                    side=FillSide.SELL,
                    quantity="2",
                    price="0.05",
                    quote_quantity="0.1",
                    fee_amount="0.01",
                    fee_asset="BGB",
                ),
                make_fill(
                    1003,
                    minute(1),
                    symbol="KASUSDT",
                    base_asset="KAS",
                    quantity="1000",
                    price="0.1",
                    quote_quantity="100",
                    fee_amount="0",
                    fee_asset="KAS",
                ),
                make_fill(
                    1004,
                    minute(2),
                    symbol="SOLUSDC",
                    base_asset="SOL",
                    quote_asset="USDC",
                    quantity="10",
                    price="150",
                    quote_quantity="1500",
                    fee_amount="3",
                    fee_asset="USDT",
                ),
            ],
        )
        await plant_fills(
            session,
            bingx,
            [
                make_fill(2001, minute(9), fee_amount="0", fee_asset="BNB"),
                make_fill(
                    2002,
                    minute(3),
                    symbol="ETHUSDT",
                    base_asset="ETH",
                    quantity="1",
                    price="2500",
                    quote_quantity="2500",
                    fee_amount="-0.1",
                    fee_asset="BGB",
                ),
            ],
        )
    return user_id


EXPECTED_TWO_VENUES: Final = [
    ("BGB", minute(3)),
    ("BTC", minute(5)),
    ("ETH", minute(3)),
    ("KAS", minute(1)),
    ("SOL", minute(2)),
]


async def test_the_service_answers_from_the_stored_fills_of_every_venue(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The earliest fill across both accounts, the stored zero fee uncounted, cash absent."""
    user_id = await plant_two_venues(factory)

    answer = await first_trades(factory, user_id)

    assert answer == EXPECTED_TWO_VENUES
    assert all(instant.utcoffset() == timedelta(0) for _asset, instant in answer)


async def test_the_service_returns_the_reducers_records(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await plant_two_venues(factory)

    async with factory() as session:
        answer = await build_accounting_service(session).first_trades(user_id)

    assert answer == tuple(
        FirstTrade(asset=asset, first_trade_at=instant) for asset, instant in EXPECTED_TWO_VENUES
    )


async def test_an_owner_without_fills_gets_an_empty_answer(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        user_id = await plant_owner(session)
        await plant_account(session, user_id, ExchangeKey.BITGET)

    assert await first_trades(factory, user_id) == []


async def test_an_owner_without_an_account_gets_an_empty_answer(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        user_id = await plant_owner(session)

    assert await first_trades(factory, user_id) == []
    assert await first_trades(factory, user_id + 4242) == [], "an id nobody has"


async def test_another_owners_fills_are_not_read(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A stranger traded BTC before the owner did, and an asset the owner never touched.

    Neither moves the owner's answer, and the stranger's answer holds only their own fills.
    """
    user_id = await plant_two_venues(factory)
    async with factory() as session:
        stranger = await plant_owner(session, "someone-else")
        theirs = await plant_account(session, stranger, ExchangeKey.BITGET)
        await plant_fills(
            session,
            theirs,
            [
                # Before every fill of the owner's, in an asset both of them hold.
                make_fill(9001, minute(-600), fee_amount="0", fee_asset=None),
                make_fill(
                    9002,
                    minute(-300),
                    symbol="ZZOTHERUSDT",
                    base_asset="ZZOTHER",
                    quantity="1",
                    price="1",
                    quote_quantity="1",
                    fee_amount="0",
                    fee_asset=None,
                ),
            ],
        )

    assert await first_trades(factory, user_id) == EXPECTED_TWO_VENUES
    assert await first_trades(factory, stranger) == [
        ("BTC", minute(-600)),
        ("ZZOTHER", minute(-300)),
    ]


async def test_manual_adjustments_are_not_counted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An opening balance of BTC dated before its first fill, and one of an asset no fill
    names: the answer is when the *imported* history begins, so neither changes it."""
    user_id = await plant_two_venues(factory)
    async with factory() as session:
        await plant_adjustment(session, user_id, asset="BTC", occurred_at=minute(-1000))
        await plant_adjustment(session, user_id, asset="DOGE", occurred_at=minute(-2000))

    assert await first_trades(factory, user_id) == EXPECTED_TWO_VENUES


async def test_an_owner_with_adjustments_and_no_fills_gets_an_empty_answer(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        user_id = await plant_owner(session)
        await plant_adjustment(session, user_id, asset="BTC", occurred_at=minute(0))

    assert await first_trades(factory, user_id) == []


async def test_the_stored_instant_keeps_its_milliseconds(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Through the column and back: the earlier of two fills 1 ms apart, to the millisecond."""
    early = START + timedelta(milliseconds=123)
    async with factory() as session:
        user_id = await plant_owner(session)
        account = await plant_account(session, user_id, ExchangeKey.BITGET)
        await plant_fills(
            session,
            account,
            [
                make_fill(1, early + timedelta(milliseconds=1), fee_amount="0", fee_asset=None),
                make_fill(2, early, fee_amount="0", fee_asset=None),
            ],
        )

    assert await first_trades(factory, user_id) == [("BTC", early)]


@pytest.mark.parametrize("shape", ["same_asset", "fee_consumes_received", "rebate_exceeds_given"])
async def test_a_stored_fill_the_recompute_refuses_does_not_fail_this_read(
    factory: async_sessionmaker[AsyncSession], shape: str
) -> None:
    """A row written before #99, which no recompute gets past. The owner still needs the
    form's date hint, so the read answers, and counts the row: it is part of the history."""
    async with factory() as session:
        user_id = await plant_owner(session)
        account = await plant_account(session, user_id, ExchangeKey.BITGET)
        await plant_fills(
            session, account, [make_fill(1, minute(9), fee_amount="0", fee_asset=None)]
        )
        await plant_unconvertible_fill(
            session, account, trade_id="synthetic-unconvertible", shape=shape, executed_at=minute(4)
        )

    async with factory() as session:
        service = build_accounting_service(session)
        with pytest.raises(UnconvertibleFillError):
            await service.recompute(user_id)

    assert await first_trades(factory, user_id) == [("BTC", minute(4))]


@pytest.mark.parametrize("fee", ["NaN", "sNaN", "Infinity", "-Infinity"])
async def test_a_stored_fee_that_is_not_a_number_does_not_fail_this_read(
    factory: async_sessionmaker[AsyncSession], fee: str
) -> None:
    """The column is text, so a hand edit can leave these in it, and they load as `Decimal`s.

    Nothing the application writes: `NormalizedFill` refuses an amount that is not finite.
    The read answers, with the fill's base and quote dated by it, and its fee asset too:
    none of these is zero (ruling R7).
    """
    async with factory() as session:
        user_id = await plant_owner(session)
        account = await plant_account(session, user_id, ExchangeKey.BITGET)
        await session.execute(
            text(
                "INSERT INTO exchange_fills (exchange_account_id, external_trade_id, "
                "external_order_id, symbol, base_asset, quote_asset, side, quantity, price, "
                "quote_quantity, quote_quantity_derived, fee_amount, fee_asset, executed_at, "
                "raw_payload, ingested_at) VALUES (:account, 'synthetic-not-a-number', NULL, "
                "'ETHBTC', 'ETH', 'BTC', 'buy', :one, :one, :one, 0, :fee, 'BGB', :at, '{}', "
                ":ingested)"
            ),
            {
                "account": account,
                "one": fixed(Decimal(1)),
                "fee": fee,
                "at": sqlite_timestamp(minute(4)),
                "ingested": sqlite_timestamp(INGESTED_AT),
            },
        )
        await session.commit()

    answer = dict(await first_trades(factory, user_id))

    assert answer == {"BGB": minute(4), "BTC": minute(4), "ETH": minute(4)}


# --------------------------------------------------------------------------------------
# Criterion 3: nothing about a datetime is decided in SQL
# --------------------------------------------------------------------------------------


async def recorded_statements(
    factory: async_sessionmaker[AsyncSession], user_id: int, *, through: str
) -> tuple[list[str], Any]:
    """Every statement sent to SQLite by one read, and what the read returned.

    `through` is `service` for `AccountingService.first_trades`, or `repository` for the read
    the recompute already makes, `ExchangeFillRepository.list_fills_for_accounting`.
    """
    statements: list[str] = []

    def keep(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        del conn, cursor, rest
        statements.append(statement)

    async with factory() as session:
        engine = session.bind
        assert engine is not None
        event.listen(engine.sync_engine, "before_cursor_execute", keep)
        try:
            if through == "service":
                answer: Any = await build_accounting_service(session).first_trades(user_id)
            else:
                answer = await ExchangeFillRepository(session).list_fills_for_accounting(user_id)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", keep)
    return statements, answer


async def test_the_read_issues_the_recomputes_fill_select_and_nothing_else(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Spec 027: it reads the fills the recompute already reads, and reduces them in Python.

    So the statements of one read are exactly the statements of that repository read: no
    second query that asks SQLite for a minimum, and none against the adjustments or the
    snapshot.
    """
    user_id = await plant_two_venues(factory)

    through_service, answer = await recorded_statements(factory, user_id, through="service")
    through_repository, fills = await recorded_statements(factory, user_id, through="repository")

    assert pairs(answer) == EXPECTED_TWO_VENUES, "the control: the read returned the answer"
    assert len(fills) == 6, "the control: the repository read returned the six fills"
    assert through_repository, "nothing was recorded"
    assert through_service == through_repository
    assert len(through_service) == 1
    assert "exchange_fills" in through_service[0]


async def test_no_statement_aggregates_orders_or_compares_a_datetime(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`executed_at` is text in SQLite: `MIN()`, `ORDER BY` and `<` on it compare strings.

    Held on the statements themselves, so that it fails with the SQL in the message whether
    the cause is a new query in the service or a change to the repository's select.
    """
    user_id = await plant_two_venues(factory)

    statements, _answer = await recorded_statements(factory, user_id, through="service")

    assert statements, "nothing was recorded"
    for statement in statements:
        upper = " ".join(statement.upper().split())
        assert upper.startswith("SELECT"), statement
        for aggregate in ("MIN(", "MAX(", "SUM(", "AVG(", "TOTAL(", "COUNT(", "GROUP BY"):
            assert aggregate not in upper, statement
        assert "RAW_PAYLOAD" not in upper, statement
        assert "MANUAL_ADJUSTMENTS" not in upper, statement
        assert "ACCOUNTING_" not in upper, statement
        if " ORDER BY " in upper:
            assert upper.split(" ORDER BY ", 1)[1].strip() == "EXCHANGE_FILLS.ID", statement
        if " WHERE " in upper:
            where = upper.split(" WHERE ", 1)[1].split(" ORDER BY ", 1)[0]
            for column in ("EXECUTED_AT", "INGESTED_AT", "FEE_AMOUNT", "QUANTITY", "PRICE"):
                assert column not in where, f"{column} is compared in SQL: {statement}"


async def test_the_read_writes_nothing(factory: async_sessionmaker[AsyncSession]) -> None:
    """No snapshot before, none after: the read does not recompute and leaves no row."""
    user_id = await plant_two_venues(factory)
    before = await snapshot_tables(factory)

    await first_trades(factory, user_id)

    assert before == {"header": [], "positions": [], "lots": [], "warnings": []}
    assert await snapshot_tables(factory) == before


def runtime_imports(source: str) -> list[str]:
    """Every module a file imports when it runs: `if TYPE_CHECKING:` blocks are left out."""
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append(node.module or "")
    return found


def test_the_accounting_service_cannot_write_sql_of_its_own() -> None:
    """Nothing of SQLAlchemy is imported at runtime, so no statement can be built here.

    The module's one query-shaped need is met by a repository method. A `select`, a `func` or
    a `text` imported into it is the first step of a `MIN(executed_at)`, and this is where
    that step fails.
    """
    source = Path(accounting_module.__file__).read_text(encoding="utf-8")
    imports = runtime_imports(source)

    assert "portfolio.repositories.exchanges" in imports, "the scan read the module's imports"
    assert [name for name in imports if name.split(".")[0] == "sqlalchemy"] == []


def test_the_import_scan_tells_a_runtime_import_from_a_typing_one() -> None:
    """The control on `runtime_imports`: it reports the first and not the second."""
    source = (
        "from typing import TYPE_CHECKING\n"
        "import sqlalchemy.sql\n"
        "from sqlalchemy import func\n"
        "if TYPE_CHECKING:\n"
        "    from sqlalchemy.ext.asyncio import AsyncSession\n"
    )

    assert runtime_imports(source) == ["typing", "sqlalchemy.sql", "sqlalchemy"]
