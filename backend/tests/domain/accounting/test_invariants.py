"""Criterion 9: the invariants I1-I8 hold for any valid event history, not just the ones written.

Histories come from `strategies.py`: five assets, every event kind, every fee position,
amounts at up to 18 places, and deliberately colliding timestamps. Each invariant is its
own test, named after it, so a failure says which property broke; `test_the_engine_agrees_
with_the_oracle` then checks every field of every result against the `Fraction` oracle,
which is the check that catches a digit no invariant constrains.

## Example budgets

The CI pytest step is already about 150 s and the per-test ceiling is 30 s, so the budgets
are chosen, not defaulted. Measured on a developer machine, this module runs in about 25 s:

* **200** for oracle agreement and for conservation (I8), about 3 s each. They are the two
  strongest checks -- one compares every field, the other balances the books exactly --
  so they get the most examples.
* **200** for oracle agreement over few-unit histories, about 3 s, and **50** for I8 over
  them. These exist because a mutation sweep showed ordinary histories almost never put a
  split on a half-unit tie. They kill a complement re-rounded in a disposal or a sale on
  every run measured, and one re-rounded in a swap on two runs of three; the example tests
  in `test_replay.py` kill all three deterministically.
* **60** for the prefix tests (I1, I2, I4), about 1.2 s each. Each example replays every
  prefix of a history of up to 16 events, so it is really up to 17 examples.
* **50** for the rest (I3, I5, I6, I7 and the ambient context), under 1 s each, which
  replay two to four times per example and are narrower claims the oracle agreement
  already constrains.
* The reachability searches are `derandomize`d and stop at the first hit, so they cost
  what they cost on every run -- about 3 s together.

Everything runs with `deadline=None`: one slow example on a loaded CI runner is not a
defect, and the 30 s per-test ceiling is what ends a genuine hang.
"""

from __future__ import annotations

import decimal
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Final

import pytest
from hypothesis import HealthCheck, Phase, find, given, settings
from hypothesis import strategies as st

from portfolio.domain.accounting import (
    Adjustment,
    ConflictingEventError,
    EventKey,
    NegativeInventory,
    Trade,
    Transfer,
    UnattributedFee,
    replay,
)
from portfolio.domain.exchanges import FillSide
from portfolio.domain.money import multiply, subtract
from tests.domain.accounting import oracle
from tests.domain.accounting.strategies import (
    CASH,
    CONFIG,
    NON_CASH,
    START,
    amounts,
    histories,
    units_below,
)
from tests.domain.accounting.support import (
    comparable,
    engine_to_json,
    oracle_replay,
    position,
    to_oracle,
)

if TYPE_CHECKING:
    from portfolio.domain.accounting import AccountingResult
    from portfolio.domain.accounting.events import AccountingEvent

STRONG: Final = settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
PREFIX: Final = settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
STANDARD: Final = settings(
    max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)

#: One unit at 18 places, the grid every amount lives on.
ONE_UNIT: Final = Fraction(1, 10**18)
#: Half a unit at 18 places: the most a correctly rounded quotient can be off by.
HALF_UNIT: Final = Fraction(5, 10**19)
CEILING: Final = Fraction(10) ** 20


def in_replay_order(events: list[AccountingEvent]) -> list[AccountingEvent]:
    """The engine's own order, computed independently: `(occurred_at, source, id, kind)`."""
    return sorted(events, key=lambda event: oracle.replay_order(to_oracle(event)))


def prefixes(events: list[AccountingEvent]) -> list[tuple[list[AccountingEvent], AccountingResult]]:
    """Every prefix of the history in replay order, from empty to whole, with its result."""
    ordered = in_replay_order(events)
    return [(ordered[:size], replay(ordered[:size], CONFIG)) for size in range(len(ordered) + 1)]


def later_key(external_id: str, source: str = "bitget") -> EventKey:
    """A key after every event `histories` can generate."""
    return EventKey(START.replace(month=4), source, external_id)


# --------------------------------------------------------------------------------------
# I1: no negative inventory, a warning naming the asset and the moment, never a raise
# --------------------------------------------------------------------------------------


@PREFIX
@given(events=histories())
def test_i1_no_quantity_is_ever_negative_after_any_prefix(events: list[AccountingEvent]) -> None:
    """Checked after every prefix, as the criterion asks, not only at the end.

    `replay` returning at all is the "never crashes" half: any exception fails the example.
    """
    for _, result in prefixes(events):
        for found in result.positions:
            assert found.quantity >= 0, found
            assert found.unknown_basis_quantity >= 0, found
            assert found.quantity - found.unknown_basis_quantity >= 0, found


@PREFIX
@given(events=histories())
def test_i1_every_shortfall_is_named_with_its_asset_and_moment(
    events: list[AccountingEvent],
) -> None:
    """Each `NegativeInventory` is keyed by the event that fell short, in an asset it moved.

    And warnings only accumulate: a prefix's warnings are the start of the next prefix's.
    """
    previous: tuple[object, ...] = ()
    for prefix, result in prefixes(events):
        assert result.warnings[: len(previous)] == previous
        previous = result.warnings
        by_key = {event.key: event for event in prefix}
        for warning in result.warnings:
            assert warning.key in by_key, warning
            source = by_key[warning.key]
            assert isinstance(source, Trade), warning
            legs = {source.base_asset, source.quote_asset, source.fee_asset}
            if isinstance(warning, NegativeInventory):
                assert warning.asset in legs, warning
                assert warning.asset not in CASH, warning
                assert warning.shortfall > 0, warning
                assert "HISTORY_INCOMPLETE" in {
                    flag.name for flag in position(result, warning.asset).flags
                }
            else:
                assert isinstance(warning, UnattributedFee), warning
                assert warning.fee_asset == source.fee_asset
                assert warning.quantity > 0, warning


# --------------------------------------------------------------------------------------
# I2: the average is the correctly rounded quotient of the basis and the known quantity
# --------------------------------------------------------------------------------------


@PREFIX
@given(events=histories())
def test_i2_average_is_correctly_rounded_quotient(events: list[AccountingEvent]) -> None:
    """`|multiply(average, Qk) - C| <= Qk x 5E-19` after every prefix (criterion 3).

    The product is computed with `money.multiply`, which is exact, and compared in
    `Fraction`, so the check itself cannot round. Where R1 leaves the average out, the
    quotient must really be too wide to hold: at least `10**20 - 5E-19`.
    """
    for _, result in prefixes(events):
        for found in result.positions:
            known = Fraction(found.quantity) - Fraction(found.unknown_basis_quantity)
            basis = Fraction(found.cost_basis)
            if known == 0:
                assert found.average_cost is None, found
                continue
            if found.average_cost is None:
                assert abs(basis / known) >= CEILING - HALF_UNIT, found
                continue
            known_decimal = subtract(found.quantity, found.unknown_basis_quantity)
            product = Fraction(multiply(found.average_cost, known_decimal))
            assert abs(product - basis) <= known * HALF_UNIT, found


# --------------------------------------------------------------------------------------
# I3: identical output for identical input, whatever the order
# --------------------------------------------------------------------------------------


@STANDARD
@given(events=histories())
def test_i3_replaying_twice_gives_identical_output(events: list[AccountingEvent]) -> None:
    first = replay(events, CONFIG)
    second = replay(events, CONFIG)

    assert first == second
    assert first.input_fingerprint == second.input_fingerprint


@STANDARD
@given(events=histories(), data=st.data())
def test_i3_any_permutation_gives_identical_output(
    events: list[AccountingEvent], data: st.DataObject
) -> None:
    """The input's order is not part of the input: replay sorts it."""
    shuffled = data.draw(st.permutations(events))

    original = replay(events, CONFIG)
    permuted = replay(shuffled, CONFIG)

    assert permuted == original
    assert permuted.input_fingerprint == original.input_fingerprint
    assert engine_to_json(permuted) == engine_to_json(original)


# --------------------------------------------------------------------------------------
# I4: no known quantity, no basis
# --------------------------------------------------------------------------------------


@PREFIX
@given(events=histories())
def test_i4_zero_known_quantity_forces_zero_basis_after_every_prefix(
    events: list[AccountingEvent],
) -> None:
    for _, result in prefixes(events):
        for found in result.positions:
            if found.quantity - found.unknown_basis_quantity == 0:
                assert found.cost_basis == 0, found
                assert found.average_cost is None, found
            if found.quantity == 0:
                assert found.unknown_basis_quantity == 0, found


# --------------------------------------------------------------------------------------
# I5: fees raise basis, reduce proceeds, and are carried or flagged -- never dropped
# --------------------------------------------------------------------------------------


@st.composite
def fee_in_a_final_buy(draw: st.DrawFn) -> tuple[list[AccountingEvent], Trade, Trade, Decimal]:
    """A history, then a buy of X for cash after it, twice: with a cash fee, and with it + d.

    The fee is in the cash given (folded into the given leg) or in the other cash asset (a
    third-asset leg). Both are "a buy fee" to the criterion, and both are worth exactly
    their amount. A rebate in the cash given is bounded by what is given, as `Trade` is.
    """
    history = draw(histories())
    asset = draw(st.sampled_from(NON_CASH))
    cash = draw(st.sampled_from(CASH))
    fee_asset = draw(st.sampled_from(CASH))
    quote_quantity = draw(amounts())
    choices = [st.just(Decimal(0)), amounts()]
    if fee_asset == cash:
        rebate = units_below(draw, quote_quantity)
        if rebate is not None:
            choices.append(st.just(rebate.copy_negate()))
    else:
        choices.append(amounts().map(Decimal.copy_negate))
    fee = draw(st.one_of(choices))
    raise_by = draw(amounts(maximum=1000))
    quantity = draw(amounts())

    def final(fee_amount: Decimal) -> Trade:
        return Trade(
            key=later_key("final"),
            base_asset=asset,
            quote_asset=cash,
            side=FillSide.BUY,
            quantity=quantity,
            quote_quantity=quote_quantity,
            fee_amount=fee_amount,
            fee_asset=fee_asset,
        )

    raised = oracle.to_decimal(Fraction(fee) + Fraction(raise_by))
    return history, final(fee), final(raised), raise_by


@STANDARD
@given(case=fee_in_a_final_buy())
def test_i5_fee_delta_a_cash_fee_on_a_buy_raises_the_basis_by_exactly_d(
    case: tuple[list[AccountingEvent], Trade, Trade, Decimal],
) -> None:
    """Raising a buy's cash fee by `d` raises the asset's basis by exactly `d`, and nothing else."""
    history, low_buy, high_buy, raise_by = case
    asset = low_buy.base_asset

    low = replay([*history, low_buy], CONFIG)
    high = replay([*history, high_buy], CONFIG)

    low_x, high_x = position(low, asset), position(high, asset)
    assert Fraction(high_x.cost_basis) - Fraction(low_x.cost_basis) == Fraction(raise_by)
    assert (high_x.quantity, high_x.realized_pnl, high_x.unmatched_proceeds) == (
        low_x.quantity,
        low_x.realized_pnl,
        low_x.unmatched_proceeds,
    )
    assert [found for found in high.positions if found.asset != asset] == [
        found for found in low.positions if found.asset != asset
    ]
    assert high.unallocated_costs == low.unallocated_costs
    assert high.warnings == low.warnings


@st.composite
def fee_in_a_final_sale(draw: st.DrawFn) -> tuple[list[AccountingEvent], Trade, Trade, Decimal]:
    """A history, then a sale of X for cash after it, twice: with a cash fee, and with it + d."""
    history = draw(histories())
    asset = draw(st.sampled_from(NON_CASH))
    cash = draw(st.sampled_from(CASH))
    fee_asset = draw(st.sampled_from(CASH))  # the received asset, or the third cash asset
    quantity = draw(amounts())
    proceeds = draw(amounts())
    if fee_asset == cash:
        # received - fee - d must stay above zero: draw fee + d below the proceeds.
        total = units_below(draw, proceeds)
        if total is None:  # one unit of proceeds leaves no room: use the third cash asset
            fee_asset = CASH[1] if cash == CASH[0] else CASH[0]
            fee, raise_by = Decimal(0), draw(amounts(maximum=1000))
        else:
            fee_units = draw(st.integers(min_value=0, max_value=int(Fraction(total) * 10**18) - 1))
            fee = Decimal(f"{fee_units}E-18")
            raise_by = oracle.to_decimal(Fraction(total) - Fraction(fee))
    else:
        fee = draw(st.one_of(st.just(Decimal(0)), amounts(maximum=1000)))
        raise_by = draw(amounts(maximum=1000))

    def final(fee_amount: Decimal) -> Trade:
        return Trade(
            key=later_key("final"),
            base_asset=asset,
            quote_asset=cash,
            side=FillSide.SELL,
            quantity=quantity,
            quote_quantity=proceeds,
            fee_amount=fee_amount,
            fee_asset=fee_asset if fee_amount != 0 else None,
        )

    raised = oracle.to_decimal(Fraction(fee) + Fraction(raise_by))
    return history, final(fee), final(raised), raise_by


@STANDARD
@given(case=fee_in_a_final_sale())
def test_i5_fee_delta_a_cash_fee_on_a_sale_lowers_the_proceeds_by_exactly_d(
    case: tuple[list[AccountingEvent], Trade, Trade, Decimal],
) -> None:
    """Raising a sale's cash fee by `d` lowers realized plus unmatched by exactly `d`.

    When the sale was fully covered by known-cost units, all of `d` comes off realized P&L.
    Otherwise the proceeds are split by a rounded share, and only the sum is exact.
    """
    history, low_sale, high_sale, raise_by = case
    asset = low_sale.base_asset

    before = replay(history, CONFIG)
    low = replay([*history, low_sale], CONFIG)
    high = replay([*history, high_sale], CONFIG)

    low_x, high_x = position(low, asset), position(high, asset)
    delta_realized = Fraction(high_x.realized_pnl) - Fraction(low_x.realized_pnl)
    delta_unmatched = Fraction(high_x.unmatched_proceeds) - Fraction(low_x.unmatched_proceeds)
    assert delta_realized + delta_unmatched == -Fraction(raise_by)
    assert high_x.cost_basis == low_x.cost_basis

    held = [found for found in before.positions if found.asset == asset]
    fully_known = (
        bool(held)
        and held[0].unknown_basis_quantity == 0
        and (held[0].quantity >= low_sale.quantity)
    )
    if fully_known:
        assert delta_realized == -Fraction(raise_by)
        assert delta_unmatched == 0


@st.composite
def non_cash_fee_on_a_final_buy(draw: st.DrawFn) -> tuple[list[AccountingEvent], Trade]:
    history = draw(histories())
    asset, fee_asset = draw(st.permutations(NON_CASH))[:2]
    final = Trade(
        key=later_key("final"),
        base_asset=asset,
        quote_asset=draw(st.sampled_from(CASH)),
        side=FillSide.BUY,
        quantity=draw(amounts()),
        quote_quantity=draw(amounts()),
        fee_amount=draw(amounts(maximum=1000)),
        fee_asset=fee_asset,
    )
    return history, final


@STANDARD
@given(case=non_cash_fee_on_a_final_buy())
def test_i5_a_third_asset_fee_is_carried_at_cost_or_flagged_never_dropped(
    case: tuple[list[AccountingEvent], Trade],
) -> None:
    """Every unit of a non-cash fee either leaves the fee asset's known pool, carrying its
    cost into the asset bought, or is reported in an `UnattributedFee`. Nothing else.
    """
    history, final = case
    asset, fee_asset = final.base_asset, final.fee_asset
    assert fee_asset is not None

    before = replay(history, CONFIG)
    after = replay([*history, final], CONFIG)

    def known_and_basis(result: AccountingResult, symbol: str) -> tuple[Fraction, Fraction]:
        for found in result.positions:
            if found.asset == symbol:
                known = Fraction(found.quantity) - Fraction(found.unknown_basis_quantity)
                return known, Fraction(found.cost_basis)
        return Fraction(0), Fraction(0)

    fee_known_before, fee_basis_before = known_and_basis(before, fee_asset)
    fee_known_after, fee_basis_after = known_and_basis(after, fee_asset)
    _, asset_basis_before = known_and_basis(before, asset)
    _, asset_basis_after = known_and_basis(after, asset)
    new_warnings = after.warnings[len(before.warnings) :]
    unattributed = sum(
        (
            Fraction(warning.quantity)
            for warning in new_warnings
            if isinstance(warning, UnattributedFee)
        ),
        Fraction(0),
    )

    carried = fee_basis_before - fee_basis_after
    assert asset_basis_after - asset_basis_before == Fraction(final.quote_quantity) + carried
    assert (fee_known_before - fee_known_after) + unattributed == Fraction(final.fee_amount)
    if unattributed:
        assert "UNATTRIBUTED_FEE" in {flag.name for flag in position(after, asset).flags}
        (fee_warning,) = [w for w in new_warnings if isinstance(w, UnattributedFee)]
        assert (fee_warning.fee_asset, fee_warning.charged_to) == (fee_asset, asset)


# --------------------------------------------------------------------------------------
# I6: re-ingesting changes nothing; a conflicting re-ingest is refused
# --------------------------------------------------------------------------------------


@STANDARD
@given(events=histories(), data=st.data())
def test_i6_duplicated_events_change_nothing(
    events: list[AccountingEvent], data: st.DataObject
) -> None:
    """Events repeated any number of times, in any order: an equal result, fingerprint and all."""
    repeats = data.draw(st.lists(st.sampled_from(events), max_size=10)) if events else []
    noisy = data.draw(st.permutations([*events, *repeats]))

    assert replay(noisy, CONFIG) == replay(events, CONFIG)


@STANDARD
@given(events=histories())
def test_i6_the_whole_log_twice_changes_nothing(events: list[AccountingEvent]) -> None:
    once = replay(events, CONFIG)
    twice = replay([*events, *events], CONFIG)

    assert twice == once
    assert twice.event_count == len(events)


def _nudged(event: AccountingEvent) -> AccountingEvent:
    """The same identity with one unit more quantity: a conflicting re-ingest."""
    more = oracle.to_decimal(Fraction(event.quantity) + ONE_UNIT)
    if isinstance(event, Trade):
        return Trade(
            key=event.key,
            base_asset=event.base_asset,
            quote_asset=event.quote_asset,
            side=event.side,
            quantity=more,
            quote_quantity=event.quote_quantity,
            fee_amount=event.fee_amount,
            fee_asset=event.fee_asset,
        )
    if isinstance(event, Adjustment):
        return Adjustment(
            key=event.key, asset=event.asset, quantity=more, unit_cost=event.unit_cost
        )
    return Transfer(
        key=event.key,
        asset=event.asset,
        quantity=more,
        from_location=event.from_location,
        to_location=event.to_location,
    )


@STANDARD
@given(events=histories(min_size=1), data=st.data())
def test_i6_a_conflicting_duplicate_is_refused_wherever_it_appears(
    events: list[AccountingEvent], data: st.DataObject
) -> None:
    victim = data.draw(st.sampled_from(events))
    conflicting = _nudged(victim)
    position_in_list = data.draw(st.integers(min_value=0, max_value=len(events)))
    tampered = [*events[:position_in_list], conflicting, *events[position_in_list:]]

    with pytest.raises(ConflictingEventError):
        replay(tampered, CONFIG)


# --------------------------------------------------------------------------------------
# I7: a transfer changes nothing
# --------------------------------------------------------------------------------------


@STANDARD
@given(events=histories(), data=st.data())
def test_i7_transfer_changes_nothing(events: list[AccountingEvent], data: st.DataObject) -> None:
    """A transfer of any asset, at any moment, anywhere in the input: positions, warnings,
    lots and unallocated costs are unchanged. Only the count and the fingerprint move.
    """
    minutes = data.draw(st.integers(min_value=0, max_value=45))
    moved = Transfer(
        key=EventKey(
            START.replace(minute=minutes % 60), data.draw(st.sampled_from(["bitget", "bingx"])), "w"
        ),
        asset=data.draw(st.sampled_from([*NON_CASH, *CASH, "SOL"])),
        quantity=data.draw(amounts()),
        from_location="bitget",
        to_location="cold-storage",
    )
    index = data.draw(st.integers(min_value=0, max_value=len(events)))

    without = replay(events, CONFIG)
    with_transfer = replay([*events[:index], moved, *events[index:]], CONFIG)

    assert comparable(with_transfer) == comparable(without)
    assert with_transfer.event_count == without.event_count + 1
    assert with_transfer.input_fingerprint != without.input_fingerprint


# --------------------------------------------------------------------------------------
# I8: conservation, exactly
# --------------------------------------------------------------------------------------


@STRONG
@given(events=histories())
def test_i8_conservation(events: list[AccountingEvent]) -> None:
    """`sum C - sum R - sum U + unallocated_costs == N + A`, with no tolerance (criterion 9).

    `N + A` is computed by `oracle.invested` from the events alone -- the cash the trades
    put in and the known cost of non-cash adjustments (R6) -- and never from a pool, so the
    two sides of the equation share no code.
    """
    result = replay(events, CONFIG)

    left = (
        sum((Fraction(found.cost_basis) for found in result.positions), Fraction(0))
        - sum((Fraction(found.realized_pnl) for found in result.positions), Fraction(0))
        - sum((Fraction(found.unmatched_proceeds) for found in result.positions), Fraction(0))
        + Fraction(result.unallocated_costs)
    )

    assert left == oracle.invested([to_oracle(event) for event in events], CASH)


# --------------------------------------------------------------------------------------
# The oracle, and the ambient context
# --------------------------------------------------------------------------------------


@STRONG
@given(events=histories())
def test_the_engine_agrees_with_the_oracle(events: list[AccountingEvent]) -> None:
    """Every field: positions, flags, averages, warnings in order, lots in order, costs, count."""
    assert engine_to_json(replay(events, CONFIG)) == oracle.result_to_json(oracle_replay(events))


@STANDARD
@given(events=histories())
def test_ambient_context_is_irrelevant(events: list[AccountingEvent]) -> None:
    """The spec's hostile context, and a stricter one that traps any inexact operation.

    `prec=6, rounding=ROUND_UP` changes the answer of anything that computes in the ambient
    context. Trapping `Inexact` and `Rounded` goes further and turns any such computation
    into an exception, even one whose answer happened to survive -- so a replay that passes
    under it touched the ambient context for nothing that rounds.
    """
    default = replay(events, CONFIG)
    with decimal.localcontext() as context:
        context.prec = 6
        context.rounding = decimal.ROUND_UP
        narrowed = replay(events, CONFIG)
    with decimal.localcontext() as context:
        context.prec = 6
        context.rounding = decimal.ROUND_UP
        context.traps[decimal.Inexact] = True
        context.traps[decimal.Rounded] = True
        trapped = replay(events, CONFIG)

    assert narrowed == default
    assert trapped == default
    assert engine_to_json(narrowed) == engine_to_json(default)


# --------------------------------------------------------------------------------------
# The strategy reaches what it claims to
# --------------------------------------------------------------------------------------

#: `derandomize` so the search is the same on every run: a reachability check that passes
#: or fails by luck would be a flaky test, not a guarantee.
FIND_SETTINGS: Final = settings(
    max_examples=2000,
    derandomize=True,
    phases=[Phase.generate],
    deadline=None,
    suppress_health_check=list(HealthCheck),
    database=None,
)


def _shape(event: Trade) -> str:
    received = event.base_asset if event.side is FillSide.BUY else event.quote_asset
    given = event.quote_asset if event.side is FillSide.BUY else event.base_asset
    return {
        (True, False): "buy",
        (False, True): "sale",
        (False, False): "swap",
        (True, True): "conversion",
    }[(given in CASH, received in CASH)]


def _fee_position(event: Trade) -> str:
    received = event.base_asset if event.side is FillSide.BUY else event.quote_asset
    given = event.quote_asset if event.side is FillSide.BUY else event.base_asset
    if event.fee_amount == 0:
        return "none" if event.fee_asset is None else "zero named"
    sign = "rebate" if event.fee_amount < 0 else "paid"
    if event.fee_asset == received:
        return f"received {sign}"
    if event.fee_asset == given:
        return f"given {sign}"
    kind = "cash" if event.fee_asset in CASH else "non-cash"
    return f"third {kind} {sign}"


def _trades(events: list[AccountingEvent]) -> list[Trade]:
    return [event for event in events if isinstance(event, Trade)]


def _swap_with_no_known_part(events: list[AccountingEvent]) -> bool:
    """A swap whose received lot is entirely of unknown cost: `known_in == 0`."""
    swaps = {to_oracle(t).key: t for t in _trades(events) if _shape(t) == "swap"}
    for lot in oracle_replay(events).lots:
        swap = swaps.get(lot.key)
        if swap is None:
            continue
        received = swap.base_asset if swap.side is FillSide.BUY else swap.quote_asset
        if lot.asset == received and lot.unknown_basis_quantity == lot.quantity:
            return True
    return False


REACHABLE: Final[dict[str, object]] = {
    **{
        f"a {shape}": (lambda events, shape=shape: any(_shape(t) == shape for t in _trades(events)))
        for shape in ("buy", "sale", "swap", "conversion")
    },
    **{
        f"a fee {where}": (
            lambda events, where=where: any(_fee_position(t) == where for t in _trades(events))
        )
        for where in (
            "none",
            "zero named",
            "received paid",
            "received rebate",
            "given paid",
            "given rebate",
            "third cash paid",
            "third cash rebate",
            "third non-cash paid",
            "third non-cash rebate",
        )
    },
    "an adjustment with a cost": lambda events: any(
        isinstance(e, Adjustment) and e.unit_cost is not None and e.asset in NON_CASH
        for e in events
    ),
    "an adjustment without one": lambda events: any(
        isinstance(e, Adjustment) and e.unit_cost is None for e in events
    ),
    "an adjustment of cash": lambda events: any(
        isinstance(e, Adjustment) and e.asset in CASH for e in events
    ),
    "a transfer": lambda events: any(isinstance(e, Transfer) for e in events),
    "an amount at 18 places": lambda events: any(
        e.quantity.as_tuple().exponent == -18 for e in events
    ),
    "a tie on the instant": lambda events: len({e.key.occurred_at for e in events}) < len(events),
    "a shortfall": lambda events: any(
        isinstance(w, oracle.NegativeInventory) for w in oracle_replay(events).warnings
    ),
    "an unattributed fee": lambda events: any(
        isinstance(w, oracle.UnattributedFee) for w in oracle_replay(events).warnings
    ),
    "unmatched proceeds": lambda events: any(
        p.unmatched_proceeds > 0 for p in oracle_replay(events).positions
    ),
    "an average R1 leaves out": lambda events: any(
        p.average_cost is None and p.quantity > p.unknown_basis_quantity
        for p in oracle_replay(events).positions
    ),
    "a swap with no known part (R5)": lambda events: _swap_with_no_known_part(events),
}


@pytest.mark.parametrize("label", list(REACHABLE))
def test_the_strategy_reaches(label: str) -> None:
    """Each shape, fee position and outcome the module docstring promises, found by search.

    `find` without shrinking returns the first history that has it, and raises if 2000
    histories do not: a strategy edit that quietly stopped generating swaps, or rebates in
    the given asset, would leave every property above green over nothing.
    """
    condition = REACHABLE[label]
    assert callable(condition)
    find(histories(min_size=1), condition, settings=FIND_SETTINGS)


def test_the_find_harness_can_fail() -> None:
    """The control: a condition no history meets is reported, not silently passed."""
    from hypothesis.errors import NoSuchExample

    with pytest.raises(NoSuchExample):
        find(
            histories(max_size=2),
            lambda events: any(_shape(t) == "a fifth shape" for t in _trades(events)),
            settings=settings(FIND_SETTINGS, max_examples=50),
        )


def test_a_history_with_all_four_shapes_agrees_with_the_oracle() -> None:
    """One history found by search, with a buy, a sale, a swap and a conversion in it."""
    events = find(
        histories(min_size=12),
        lambda candidate: len({_shape(t) for t in _trades(candidate)}) == 4,
        settings=FIND_SETTINGS,
    )
    result = replay(events, CONFIG)

    assert engine_to_json(result) == oracle.result_to_json(oracle_replay(events))
    assert result.event_count == len(events)


# --------------------------------------------------------------------------------------
# Histories of a few units, where every split can land on a tie
# --------------------------------------------------------------------------------------
#
# Ordinary histories almost never put a proportional share exactly on a half unit, so the
# checks above cannot tell a complement taken by subtraction from one rounded a second
# time. Histories whose every amount is 1 to 16 units at 18 places make those ties routine.


@STRONG
@given(events=histories(few_units=True))
def test_the_engine_agrees_with_the_oracle_where_splits_tie(
    events: list[AccountingEvent],
) -> None:
    assert engine_to_json(replay(events, CONFIG)) == oracle.result_to_json(oracle_replay(events))


@STANDARD
@given(events=histories(few_units=True))
def test_i8_conservation_where_splits_tie(events: list[AccountingEvent]) -> None:
    result = replay(events, CONFIG)

    left = (
        sum((Fraction(found.cost_basis) for found in result.positions), Fraction(0))
        - sum((Fraction(found.realized_pnl) for found in result.positions), Fraction(0))
        - sum((Fraction(found.unmatched_proceeds) for found in result.positions), Fraction(0))
        + Fraction(result.unallocated_costs)
    )

    assert left == oracle.invested([to_oracle(event) for event in events], CASH)


def test_the_few_units_mode_reaches_a_tie(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proven, not assumed: a few-units history in which the oracle divides onto a tie."""
    ties: list[Fraction] = []
    real_divide = oracle.divide

    def recording_divide(dividend: Fraction, divisor: Fraction) -> Fraction:
        if (dividend / divisor * oracle.UNIT) % 1 == Fraction(1, 2):
            ties.append(dividend / divisor)
        return real_divide(dividend, divisor)

    def has_a_tie(events: list[AccountingEvent]) -> bool:
        ties.clear()
        oracle_replay(events)
        return bool(ties)

    monkeypatch.setattr(oracle, "divide", recording_divide)
    find(histories(few_units=True, min_size=2), has_a_tie, settings=FIND_SETTINGS)
