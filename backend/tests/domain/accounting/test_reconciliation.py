"""Criterion 5 of #104 (spec 025): `reconcile`, held to an oracle that shares none of its code.

`reconcile(history, wallets, exchanges, *, cash_assets)` compares two quantities per asset:
what the replay says is held, and what the wallets and the venues actually hold. The spec
fixes the rule in four sentences, and each has its tests here, named after it:

* **the assets** are the union of the three mappings, minus the cash assets, minus any asset
  whose history and held quantities are both zero, sorted by asset;
* **`held` is `wallet + exchange`, and `difference` is `held - history`**, exactly;
* **`match`** when `|difference| * 100 <= RECONCILIATION_TOLERANCE_PCT * max(history, held)`,
  compared exactly -- no division, no rounding; otherwise **`history_short`** when the
  difference is positive and **`history_over`** when it is negative;
* **a negative input quantity raises `ValueError`.**

## The oracle

`expected_rows` below is the same rule written in `fractions.Fraction`, which has no
precision, no context and no rounding mode: a `Fraction` built from a `Decimal` is that
decimal exactly, and every operation on it is exact at any length. It imports nothing from
`portfolio.domain`, and it was written from the spec before the implementation existed. A
disagreement between the two is therefore either a place where `reconcile` rounds, or a
place where it reads the spec differently, and both are what this module is for.

Every expectation in an example test is worked out by hand beside the literal, never by
calling `reconcile` or `money`.

## What the examples pin that a property would only probably find

* The tolerance boundary **exactly at one percent on both sides**, and one unit at eighteen
  places either side of it. `99` against `100` is a match; `98.999999999999999999` is not.
* That the tolerance is relative to **the larger** of the two quantities: relative to the
  smaller, to the history alone or to the balances alone, one of the two exact-boundary rows
  flips.
* **Exactness where arithmetic at the interpreter's 28 digits rounds**, and where the
  application's own 38 does: a difference whose last digit decides the status, at 38
  significant digits, and a held quantity whose sum needs 39.

## Example budgets

`deadline=None` everywhere, as in `test_invariants.py`. 300 examples for the oracle agreement,
which checks every field of every row; 150 for the narrower properties. The reachability
searches are derandomized and stop at their first hit. The module runs in a few seconds.
"""

from __future__ import annotations

import dataclasses
import decimal
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Final

import pytest
from hypothesis import HealthCheck, Phase, find, given, settings
from hypothesis import strategies as st

import portfolio.domain.accounting as accounting_package
from portfolio.domain.accounting import (
    DEFAULT_CASH_ASSETS,
    RECONCILIATION_TOLERANCE_PCT,
    AssetReconciliation,
    ReconciliationStatus,
    reconcile,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

# --------------------------------------------------------------------------------------
# The oracle: the spec's rule in exact rationals
# --------------------------------------------------------------------------------------

MATCH: Final = "match"
HISTORY_SHORT: Final = "history_short"
HISTORY_OVER: Final = "history_over"

ORACLE_CASH: Final = frozenset({"USDC", "USDT"})
"""Spelled here, not imported: an oracle that read `DEFAULT_CASH_ASSETS` would agree with
whatever that constant became."""

ORACLE_TOLERANCE_PCT: Final = 1
"""One percent, the spec's number, as the integer it is."""

SCALE: Final = 18
UNIT: Final = Fraction(1, 10**SCALE)


@dataclass(frozen=True)
class Expected:
    """One row the spec says `reconcile` returns, in exact rationals."""

    asset: str
    history: Fraction
    wallet: Fraction
    exchange: Fraction
    held: Fraction
    difference: Fraction
    status: str


def expected_status(history: Fraction, held: Fraction) -> str:
    """The spec's comparison: multiply both sides, never divide."""
    difference = held - history
    if abs(difference) * 100 <= ORACLE_TOLERANCE_PCT * max(history, held):
        return MATCH
    return HISTORY_SHORT if difference > 0 else HISTORY_OVER


def expected_rows(
    history: Mapping[str, Decimal],
    wallets: Mapping[str, Decimal],
    exchanges: Mapping[str, Decimal],
    cash: frozenset[str] = ORACLE_CASH,
) -> list[Expected]:
    """The union, minus cash, minus both-zero, sorted, with each row's exact figures."""
    rows: list[Expected] = []
    for asset in sorted(set(history) | set(wallets) | set(exchanges)):
        if asset in cash:
            continue
        in_history = Fraction(history.get(asset, Decimal(0)))
        in_wallets = Fraction(wallets.get(asset, Decimal(0)))
        on_exchanges = Fraction(exchanges.get(asset, Decimal(0)))
        held = in_wallets + on_exchanges
        if in_history == 0 and held == 0:
            continue
        rows.append(
            Expected(
                asset=asset,
                history=in_history,
                wallet=in_wallets,
                exchange=on_exchanges,
                held=held,
                difference=held - in_history,
                status=expected_status(in_history, held),
            )
        )
    return rows


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def amounts(values: Mapping[str, str]) -> dict[str, Decimal]:
    return {asset: Decimal(text) for asset, text in values.items()}


def run(
    history: Mapping[str, str] | None = None,
    wallets: Mapping[str, str] | None = None,
    exchanges: Mapping[str, str] | None = None,
) -> tuple[AssetReconciliation, ...]:
    return reconcile(amounts(history or {}), amounts(wallets or {}), amounts(exchanges or {}))


def only(rows: tuple[AssetReconciliation, ...]) -> AssetReconciliation:
    assert len(rows) == 1, rows
    return rows[0]


def assert_exact(found: object, expected: str) -> None:
    """Equal, a `Decimal`, and carried at eighteen places: `0 == Decimal(0)` proves none of it."""
    assert isinstance(found, Decimal), f"{found!r} is not a Decimal"
    assert found == Decimal(expected), f"{found} != {expected}"
    assert found.as_tuple().exponent == -SCALE, f"{found!r} is not at {SCALE} places"


def figures(row: AssetReconciliation) -> tuple[Decimal, ...]:
    return (
        row.history_quantity,
        row.wallet_quantity,
        row.exchange_quantity,
        row.held_quantity,
        row.difference,
    )


def assert_agrees(row: AssetReconciliation, expected: Expected) -> None:
    """Every field of one row against the oracle's, each compared as an exact rational."""
    assert row.asset == expected.asset
    for name, found, wanted in (
        ("history_quantity", row.history_quantity, expected.history),
        ("wallet_quantity", row.wallet_quantity, expected.wallet),
        ("exchange_quantity", row.exchange_quantity, expected.exchange),
        ("held_quantity", row.held_quantity, expected.held),
        ("difference", row.difference, expected.difference),
    ):
        assert isinstance(found, Decimal), f"{name} of {row.asset}: {found!r} is not a Decimal"
        assert Fraction(found) == wanted, f"{name} of {row.asset}: {found} != {wanted}"
    assert isinstance(row.status, ReconciliationStatus)
    assert row.status.value == expected.status, (
        f"{row.asset}: {row.status.value} != {expected.status} "
        f"(history {expected.history}, held {expected.held})"
    )


def assert_agrees_with_the_oracle(
    history: Mapping[str, Decimal],
    wallets: Mapping[str, Decimal],
    exchanges: Mapping[str, Decimal],
) -> tuple[AssetReconciliation, ...]:
    found = reconcile(history, wallets, exchanges)
    wanted = expected_rows(history, wallets, exchanges)
    assert [row.asset for row in found] == [row.asset for row in wanted]
    for row, expected in zip(found, wanted, strict=True):
        assert_agrees(row, expected)
    return found


# --------------------------------------------------------------------------------------
# The contract: names, values, shape
# --------------------------------------------------------------------------------------


def test_the_tolerance_is_one_percent_as_a_decimal() -> None:
    assert isinstance(RECONCILIATION_TOLERANCE_PCT, Decimal)
    assert Decimal(1) == RECONCILIATION_TOLERANCE_PCT
    assert str(RECONCILIATION_TOLERANCE_PCT) == "1", "the endpoint serves this as the string '1'"


def test_the_three_statuses_and_their_wire_forms() -> None:
    assert {status.name: status.value for status in ReconciliationStatus} == {
        "MATCH": "match",
        "HISTORY_SHORT": "history_short",
        "HISTORY_OVER": "history_over",
    }
    assert str(ReconciliationStatus.HISTORY_SHORT) == "history_short"


def test_the_names_are_exported_from_the_accounting_package() -> None:
    for name in (
        "RECONCILIATION_TOLERANCE_PCT",
        "AssetReconciliation",
        "ReconciliationStatus",
        "reconcile",
    ):
        assert name in accounting_package.__all__, name


def test_the_default_cash_assets_are_the_engines() -> None:
    """The default is the engine's set, which holds no quantity for either symbol."""
    assert frozenset({"USDC", "USDT"}) == DEFAULT_CASH_ASSETS

    rows = run(history={"USDT": "5", "USDC": "6"}, exchanges={"USDT": "50", "USDC": "60"})

    assert rows == ()


def test_a_row_is_frozen_and_carries_exactly_the_specs_fields() -> None:
    row = only(run(history={"BTC": "1"}, wallets={"BTC": "1"}))

    assert [field.name for field in dataclasses.fields(row)] == [
        "asset",
        "history_quantity",
        "wallet_quantity",
        "exchange_quantity",
        "held_quantity",
        "difference",
        "status",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.status = ReconciliationStatus.HISTORY_SHORT  # type: ignore[misc]


def test_the_result_is_a_tuple_and_empty_inputs_give_an_empty_one() -> None:
    assert reconcile({}, {}, {}) == ()
    assert isinstance(run(history={"BTC": "1"}), tuple)


# --------------------------------------------------------------------------------------
# The spec's own example, by hand
# --------------------------------------------------------------------------------------


def test_the_specs_example_row_by_hand() -> None:
    """0.5 BTC in the history, 0.7 in wallets and 0.3 on exchanges.

    held = 0.7 + 0.3 = 1.0; difference = 1.0 - 0.5 = 0.5; 0.5 x 100 = 50 > 1 x max(0.5, 1.0),
    and the difference is positive: `history_short`.
    """
    row = only(run(history={"BTC": "0.5"}, wallets={"BTC": "0.7"}, exchanges={"BTC": "0.3"}))

    assert row.asset == "BTC"
    assert_exact(row.history_quantity, "0.5")
    assert_exact(row.wallet_quantity, "0.7")
    assert_exact(row.exchange_quantity, "0.3")
    assert_exact(row.held_quantity, "1.0")
    assert_exact(row.difference, "0.5")
    assert row.status is ReconciliationStatus.HISTORY_SHORT
    assert str(row.held_quantity) == "1.000000000000000000", "the endpoint's wire form"
    assert str(row.difference) == "0.500000000000000000"


def test_held_is_wallets_plus_exchanges_and_the_difference_is_held_minus_history() -> None:
    """0.1 + 0.2 is 0.3 exactly, which binary floating point cannot say; 0.3 - 0.25 = 0.05."""
    row = only(run(history={"KAS": "0.25"}, wallets={"KAS": "0.1"}, exchanges={"KAS": "0.2"}))

    assert_exact(row.held_quantity, "0.3")
    assert_exact(row.difference, "0.05")


def test_the_difference_is_negative_when_the_history_holds_more() -> None:
    """2 in the history, 0.5 held: difference -1.5, and its sign is what names the status."""
    row = only(run(history={"ETH": "2"}, wallets={"ETH": "0.5"}))

    assert_exact(row.difference, "-1.5")
    assert row.status is ReconciliationStatus.HISTORY_OVER


def test_equal_quantities_match_with_a_zero_difference() -> None:
    row = only(run(history={"BTC": "1.25"}, wallets={"BTC": "1"}, exchanges={"BTC": "0.25"}))

    assert_exact(row.difference, "0")
    assert not row.difference.is_signed(), "a zero difference is 0, never -0"
    assert row.status is ReconciliationStatus.MATCH


# --------------------------------------------------------------------------------------
# The tolerance boundary, exactly at one percent and one unit either side of it
# --------------------------------------------------------------------------------------

ONE_UNIT_UNDER_99: Final = "98.999999999999999999"
ONE_UNIT_OVER_99: Final = "99.000000000000000001"


@pytest.mark.parametrize(
    ("history", "held", "status"),
    [
        # held is the larger side: the tolerance is 1% of held = 1, so the history may be
        # 99 and not one unit less.
        pytest.param("99", "100", MATCH, id="held larger, exactly 1%"),
        pytest.param(ONE_UNIT_UNDER_99, "100", HISTORY_SHORT, id="held larger, one unit past"),
        pytest.param(ONE_UNIT_OVER_99, "100", MATCH, id="held larger, one unit inside"),
        # history is the larger side: the same boundary, mirrored.
        pytest.param("100", "99", MATCH, id="history larger, exactly 1%"),
        pytest.param("100", ONE_UNIT_UNDER_99, HISTORY_OVER, id="history larger, one unit past"),
        pytest.param("100", ONE_UNIT_OVER_99, MATCH, id="history larger, one unit inside"),
        # 100 against 101: 1 x 100 = 100 <= 101. Relative to the smaller side it would be
        # 100 <= 100, still a match; the two exact rows above are the ones that tell the
        # larger side from the smaller.
        pytest.param("100", "101", MATCH, id="just under 1% of the larger"),
        pytest.param("101", "100", MATCH, id="just under 1% of the larger, mirrored"),
        # two percent is out on either side: a tolerance doubled would pass these.
        pytest.param("98", "100", HISTORY_SHORT, id="2% short"),
        pytest.param("100", "98", HISTORY_OVER, id="2% over"),
        pytest.param("98.5", "100", HISTORY_SHORT, id="1.5% short"),
        pytest.param("100", "98.5", HISTORY_OVER, id="1.5% over"),
        # the smallest quantities there are: 99 and 100 units at eighteen places.
        pytest.param("99E-18", "100E-18", MATCH, id="dust, exactly 1%"),
        pytest.param("98E-18", "100E-18", HISTORY_SHORT, id="dust, 2%"),
        pytest.param("100E-18", "99E-18", MATCH, id="dust, exactly 1%, mirrored"),
        pytest.param("100E-18", "98E-18", HISTORY_OVER, id="dust, 2%, mirrored"),
        # one unit against two: half is not one percent.
        pytest.param("1E-18", "2E-18", HISTORY_SHORT, id="one unit against two"),
        pytest.param("2E-18", "1E-18", HISTORY_OVER, id="two units against one"),
        # nothing on one side is never a match, however little is on the other.
        pytest.param("0", "1E-18", HISTORY_SHORT, id="no history, one unit held"),
        pytest.param("1E-18", "0", HISTORY_OVER, id="one unit of history, nothing held"),
        pytest.param("0", "250", HISTORY_SHORT, id="no history at all"),
        pytest.param("250", "0", HISTORY_OVER, id="nothing held at all"),
    ],
)
def test_the_status_at_and_around_the_tolerance_boundary(
    history: str, held: str, status: str
) -> None:
    """Each row is checked with the balance in a wallet, on an exchange, and split in two."""
    half = Decimal(held) / 2 if Decimal(held).as_tuple().exponent == 0 else None
    splits: list[tuple[dict[str, str], dict[str, str]]] = [
        ({"BTC": held}, {}),
        ({}, {"BTC": held}),
        ({"BTC": "0"}, {"BTC": held}),
    ]
    if half is not None and half == half.to_integral_value():
        splits.append(({"BTC": str(half)}, {"BTC": str(half)}))

    for wallets, exchanges in splits:
        rows = run(history={"BTC": history}, wallets=wallets, exchanges=exchanges)
        assert only(rows).status.value == status, (history, wallets, exchanges)


def test_the_tolerance_is_relative_to_the_larger_quantity_not_the_smaller() -> None:
    """99 against 100: the difference is 1, which is 1% of 100 and 1.0101% of 99.

    Relative to the smaller quantity both rows would be out of tolerance. The spec says
    `max(history, held)`, so both are a match, in either direction.
    """
    short_side = only(run(history={"BTC": "99"}, exchanges={"BTC": "100"}))
    over_side = only(run(history={"BTC": "100"}, exchanges={"BTC": "99"}))

    assert short_side.status is ReconciliationStatus.MATCH
    assert over_side.status is ReconciliationStatus.MATCH
    assert_exact(short_side.difference, "1")
    assert_exact(over_side.difference, "-1")


def test_a_match_inside_the_tolerance_still_reports_its_difference() -> None:
    """`match` is a status, not a rounding: the half-percent gap is still on the row."""
    row = only(run(history={"KAS": "1000"}, wallets={"KAS": "995"}))

    assert row.status is ReconciliationStatus.MATCH
    assert_exact(row.difference, "-5")
    assert_exact(row.held_quantity, "995")


# --------------------------------------------------------------------------------------
# Exactness: no rounding, at any length, in any ambient context
# --------------------------------------------------------------------------------------

LARGEST: Final = "99999999999999999999.999999999999999999"
"""10**38 - 1 units at eighteen places: the largest amount a `NumericText(18)` column holds."""


def test_the_boundary_is_exact_at_38_significant_digits() -> None:
    """history = 10**38 - 1 units. One percent of it is 10**36 - 0.01 units.

    A held quantity 10**36 - 1 units below it differs by (10**36 - 1) x 100 = 10**38 - 100
    units-percent, which is <= 10**38 - 1: a match. One unit further, the difference is
    10**36 units, and 10**38 > 10**38 - 1: `history_over`. The two held quantities differ in
    their thirty-eighth digit, and nothing shorter than that can tell them apart.

    10**38 - 1 - (10**36 - 1) = 99 x 10**36 units = 99000000000000000000.000000000000000000.
    """
    inside = only(run(history={"BTC": LARGEST}, wallets={"BTC": "99000000000000000000"}))
    outside = only(
        run(
            history={"BTC": LARGEST},
            wallets={"BTC": "98999999999999999999.999999999999999999"},
        )
    )

    assert inside.status is ReconciliationStatus.MATCH
    assert_exact(inside.difference, "-999999999999999999.999999999999999999")
    assert outside.status is ReconciliationStatus.HISTORY_OVER
    assert_exact(outside.difference, "-1000000000000000000")


def test_a_difference_in_the_last_of_38_digits_is_kept() -> None:
    """Two quantities of 38 digits that differ by one unit: the difference is one unit.

    At the interpreter's default of 28 digits the subtraction gives zero.
    """
    row = only(
        run(
            history={"BTC": "12345678901234567890.123456789012345678"},
            exchanges={"BTC": "12345678901234567890.123456789012345679"},
        )
    )

    assert_exact(row.difference, "0.000000000000000001")
    assert row.status is ReconciliationStatus.MATCH


def test_a_held_quantity_past_38_digits_is_summed_exactly() -> None:
    """Each side fits a column; their sum needs 39 digits, and gets them.

    60000000000000000000.000000000000000001 + 60000000000000000000.000000000000000001
    = 120000000000000000000.000000000000000002. A sum rounded to the application's 38
    digits loses the final 2.
    """
    amount = "60000000000000000000.000000000000000001"
    row = only(run(wallets={"KAS": amount}, exchanges={"KAS": amount}))

    assert row.held_quantity == Decimal("120000000000000000000.000000000000000002")
    assert row.difference == Decimal("120000000000000000000.000000000000000002")
    assert row.status is ReconciliationStatus.HISTORY_SHORT


@pytest.mark.parametrize("precision", [1, 6, 28])
def test_the_answer_does_not_depend_on_the_ambient_decimal_context(precision: int) -> None:
    """Inside a context of 1, 6 or 28 digits, rounding down, the rows are the same rows.

    28 is the interpreter's default, the precision a thread that never imported `money`
    would compute at. The inputs are the 38-digit boundary pair, so any operation that used
    the ambient context would change a figure or flip a status.
    """
    history = amounts({"BTC": LARGEST, "KAS": "12345678901234567890.123456789012345678"})
    wallets = amounts({"BTC": "98999999999999999999.999999999999999999"})
    exchanges = amounts({"KAS": "12345678901234567890.123456789012345679"})
    outside = reconcile(history, wallets, exchanges)

    with decimal.localcontext() as context:
        context.prec = precision
        context.rounding = decimal.ROUND_DOWN
        inside = reconcile(history, wallets, exchanges)

    assert inside == outside
    assert [figures(row) for row in inside] == [figures(row) for row in outside]
    assert [row.status.value for row in inside] == [HISTORY_OVER, MATCH]
    assert_exact(inside[1].difference, "0.000000000000000001")


def test_inputs_at_fewer_places_come_back_at_eighteen() -> None:
    """A wallet balance arrives at its chain's decimals; the row carries it at eighteen."""
    row = only(run(history={"BTC": "1"}, wallets={"BTC": "0.50000000"}, exchanges={"BTC": "5E-1"}))

    for figure in figures(row):
        assert figure.as_tuple().exponent == -SCALE, figure
    assert_exact(row.wallet_quantity, "0.5")
    assert_exact(row.exchange_quantity, "0.5")
    assert_exact(row.held_quantity, "1")


# --------------------------------------------------------------------------------------
# The assets: the union, minus cash, minus both-zero, sorted
# --------------------------------------------------------------------------------------


def test_the_assets_are_the_union_of_the_three_mappings() -> None:
    """One asset known to each mapping alone; the sides it is absent from are zero."""
    rows = run(history={"BTC": "1"}, wallets={"KAS": "2"}, exchanges={"ETH": "3"})

    assert [row.asset for row in rows] == ["BTC", "ETH", "KAS"]
    by_asset = {row.asset: row for row in rows}

    assert_exact(by_asset["BTC"].history_quantity, "1")
    assert_exact(by_asset["BTC"].wallet_quantity, "0")
    assert_exact(by_asset["BTC"].exchange_quantity, "0")
    assert_exact(by_asset["BTC"].held_quantity, "0")
    assert_exact(by_asset["BTC"].difference, "-1")
    assert by_asset["BTC"].status is ReconciliationStatus.HISTORY_OVER

    assert_exact(by_asset["KAS"].history_quantity, "0")
    assert_exact(by_asset["KAS"].wallet_quantity, "2")
    assert_exact(by_asset["KAS"].held_quantity, "2")
    assert_exact(by_asset["KAS"].difference, "2")
    assert by_asset["KAS"].status is ReconciliationStatus.HISTORY_SHORT

    assert_exact(by_asset["ETH"].exchange_quantity, "3")
    assert_exact(by_asset["ETH"].held_quantity, "3")
    assert by_asset["ETH"].status is ReconciliationStatus.HISTORY_SHORT


def test_an_asset_is_one_row_however_many_mappings_name_it() -> None:
    rows = run(history={"BTC": "1"}, wallets={"BTC": "0.4"}, exchanges={"BTC": "0.6"})

    assert [row.asset for row in rows] == ["BTC"]


def test_names_are_compared_as_given_and_not_folded() -> None:
    """`btc` and `BTC` are two assets here: the providers agree the spelling, not the domain."""
    rows = run(history={"BTC": "1"}, exchanges={"btc": "1"})

    assert [row.asset for row in rows] == ["BTC", "btc"]
    assert [row.status.value for row in rows] == [HISTORY_OVER, HISTORY_SHORT]


@pytest.mark.parametrize("cash", ["USDT", "USDC"])
@pytest.mark.parametrize("side", ["history", "wallets", "exchanges", "all"])
def test_a_cash_asset_is_never_a_row(cash: str, side: str) -> None:
    """The engine keeps no quantity for the unit of account, so a balance of it is no finding.

    Left in, every stablecoin balance on a venue would be reported as `history_short`.
    """
    quantities = {cash: "1000", "BTC": "1"}
    rows = reconcile(
        amounts(quantities if side in ("history", "all") else {"BTC": "1"}),
        amounts(quantities if side in ("wallets", "all") else {}),
        amounts(quantities if side in ("exchanges", "all") else {}),
    )

    assert [row.asset for row in rows] == ["BTC"]


def test_the_cash_assets_are_the_ones_passed_not_only_the_default() -> None:
    """With `EUR` as the only cash asset, `EUR` is left out and `USDT` is an asset like any."""
    rows = reconcile(
        {},
        {},
        amounts({"EUR": "10", "USDT": "20", "BTC": "1"}),
        cash_assets=frozenset({"EUR"}),
    )

    assert [row.asset for row in rows] == ["BTC", "USDT"]
    assert [row.status.value for row in rows] == [HISTORY_SHORT, HISTORY_SHORT]


def test_cash_assets_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        reconcile({}, {}, {}, frozenset({"EUR"}))  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("history", "wallets", "exchanges"),
    [
        pytest.param({"BTC": "0"}, {}, {}, id="zero in the history alone"),
        pytest.param({}, {"BTC": "0"}, {}, id="zero in the wallets alone"),
        pytest.param({}, {}, {"BTC": "0"}, id="zero on the exchanges alone"),
        pytest.param({"BTC": "0"}, {"BTC": "0"}, {"BTC": "0"}, id="zero everywhere"),
        pytest.param({"BTC": "0E-18"}, {"BTC": "0.00"}, {"BTC": "0E+3"}, id="zeros of any shape"),
    ],
)
def test_an_asset_with_nothing_in_the_history_and_nothing_held_is_omitted(
    history: dict[str, str], wallets: dict[str, str], exchanges: dict[str, str]
) -> None:
    """A position sold out years ago, on a venue that still lists a zero: not a row."""
    rows = run(
        history={**history, "KAS": "5"},
        wallets={**wallets, "KAS": "5"},
        exchanges=exchanges,
    )

    assert [row.asset for row in rows] == ["KAS"]


def test_an_asset_with_zero_on_one_side_only_is_kept() -> None:
    """Both-zero is the only omission: zero history with a balance, or the reverse, is a row."""
    rows = run(history={"AAA": "0", "BBB": "1"}, wallets={"AAA": "1", "BBB": "0"})

    assert [(row.asset, row.status.value) for row in rows] == [
        ("AAA", HISTORY_SHORT),
        ("BBB", HISTORY_OVER),
    ]


def test_the_rows_are_sorted_by_asset_whatever_order_the_mappings_are_in() -> None:
    """Sorted as text: digits before capitals before lower case."""
    rows = run(
        history={"ZEC": "1", "kas": "1"},
        wallets={"ETH": "1", "1INCH": "1"},
        exchanges={"BTC": "1", "AAVE": "1"},
    )

    assert [row.asset for row in rows] == ["1INCH", "AAVE", "BTC", "ETH", "ZEC", "kas"]


def test_the_inputs_are_not_modified() -> None:
    history = amounts({"BTC": "1", "USDT": "5"})
    wallets = amounts({"BTC": "0.5"})
    exchanges = amounts({"KAS": "0"})
    before = (dict(history), dict(wallets), dict(exchanges))

    reconcile(history, wallets, exchanges)

    assert (history, wallets, exchanges) == before


# --------------------------------------------------------------------------------------
# A negative quantity is refused, and the refusal names nothing
# --------------------------------------------------------------------------------------

SENTINEL_ASSET: Final = "ZZSENTINEL"
SENTINEL_AMOUNT: Final = "-7.31337"


@pytest.mark.parametrize("side", ["history", "wallets", "exchanges"])
@pytest.mark.parametrize("amount", ["-1", "-1E-18", SENTINEL_AMOUNT])
def test_a_negative_quantity_raises_value_error(side: str, amount: str) -> None:
    """No quantity held, and no quantity in a history, is below zero: a caller's bug."""
    mappings: dict[str, dict[str, Decimal]] = {
        "history": amounts({"BTC": "1"}),
        "wallets": amounts({"BTC": "1"}),
        "exchanges": amounts({"BTC": "1"}),
    }
    mappings[side][SENTINEL_ASSET] = Decimal(amount)

    with pytest.raises(ValueError, match=r".") as raised:
        reconcile(mappings["history"], mappings["wallets"], mappings["exchanges"])

    assert not isinstance(raised.value, decimal.DecimalException)


@pytest.mark.parametrize("side", ["history", "wallets", "exchanges"])
def test_the_refusal_carries_neither_the_asset_nor_the_amount(side: str) -> None:
    """Spec 025: no log line or message carries an asset name or an amount.

    A `ValueError` raised under a request is logged with its traceback, so its text is a
    log line. It may say which side was negative; it may not say which asset or how much.
    """
    mappings: dict[str, dict[str, Decimal]] = {"history": {}, "wallets": {}, "exchanges": {}}
    mappings[side][SENTINEL_ASSET] = Decimal(SENTINEL_AMOUNT)

    with pytest.raises(ValueError, match=r".") as raised:
        reconcile(mappings["history"], mappings["wallets"], mappings["exchanges"])

    text = f"{raised.value} {raised.value!r} {raised.value.args!r}"
    assert SENTINEL_ASSET not in text
    assert "7.31337" not in text
    assert "731337" not in text


@pytest.mark.parametrize("side", ["history", "wallets", "exchanges"])
@pytest.mark.parametrize(
    "quantity",
    [
        pytest.param(1.5, id="float"),
        pytest.param(1, id="int"),
        pytest.param("1", id="str"),
        pytest.param(True, id="bool"),
        pytest.param(None, id="None"),
    ],
)
def test_a_quantity_that_is_not_a_decimal_raises_type_error(side: str, quantity: object) -> None:
    """Money is never a float, and never anything that would have to be converted to compare.

    The mappings are typed `Mapping[str, Decimal]`; this is what happens to a caller that
    ignored the type, and it is a refusal before any arithmetic, not a `Decimal(1.5)`.
    """
    mappings: dict[str, dict[str, object]] = {"history": {}, "wallets": {}, "exchanges": {}}
    mappings[side]["BTC"] = quantity

    with pytest.raises(TypeError):
        reconcile(
            mappings["history"],  # type: ignore[arg-type]
            mappings["wallets"],  # type: ignore[arg-type]
            mappings["exchanges"],  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("side", ["history", "wallets", "exchanges"])
@pytest.mark.parametrize("quantity", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_a_quantity_that_is_not_finite_raises_value_error(side: str, quantity: str) -> None:
    """A NaN compares false with everything, so it would otherwise fall through to a status."""
    mappings: dict[str, dict[str, Decimal]] = {"history": {}, "wallets": {}, "exchanges": {}}
    mappings[side]["BTC"] = Decimal(quantity)

    with pytest.raises(ValueError, match=r".") as raised:
        reconcile(mappings["history"], mappings["wallets"], mappings["exchanges"])

    assert not isinstance(raised.value, decimal.DecimalException)


def test_a_cash_asset_is_validated_like_any_other() -> None:
    """Leaving an asset out of the rows does not make a negative balance of it acceptable."""
    with pytest.raises(ValueError, match=r"."):
        run(exchanges={"USDT": "-1"})


def test_a_negative_zero_is_a_zero() -> None:
    """`-0` is not below zero. It is omitted with the other zeros, and never shown as `-0`."""
    assert run(history={"BTC": "-0"}, wallets={"BTC": "-0.00"}) == ()

    row = only(run(history={"BTC": "-0"}, wallets={"BTC": "2"}, exchanges={"BTC": "-0"}))

    assert_exact(row.history_quantity, "0")
    assert_exact(row.exchange_quantity, "0")
    for figure in figures(row):
        assert not figure.is_signed(), figure


def test_a_quantity_finer_than_eighteen_places_is_compared_exactly_not_rounded() -> None:
    """No caller produces one, and if one ever does, the comparison still does not round.

    100 against 98.9999999999999999995 -- half a unit at eighteen places under 99. Rounded
    half to even to eighteen places the balance would become 99 exactly, and a match; taken
    as it is, the difference is past one percent and the row is `history_over`.
    """
    row = only(run(history={"BTC": "100"}, wallets={"BTC": "98.9999999999999999995"}))

    assert row.status is ReconciliationStatus.HISTORY_OVER
    assert row.difference == Decimal("-1.0000000000000000005")


# --------------------------------------------------------------------------------------
# Hypothesis: any quantities a caller can produce
# --------------------------------------------------------------------------------------

UNIVERSE: Final = ("BTC", "KAS", "ETH", "BGB", "1INCH", "btc", "USDT", "USDC")
NON_CASH: Final = tuple(asset for asset in UNIVERSE if asset not in ORACLE_CASH)
MAX_UNITS: Final = 10**38 - 1
"""The largest amount a stored quantity can be: 20 digits before the point, 18 after."""

SHAPES: Final = (
    "independent",
    "equal",
    "boundary",
    "near",
    "nothing",
    "history only",
    "held only",
)

STRONG: Final = settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
STANDARD: Final = settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
FIND_SETTINGS: Final = settings(
    max_examples=3000,
    deadline=None,
    derandomize=True,
    database=None,
    phases=[Phase.generate],
    suppress_health_check=list(HealthCheck),
)

Scenario = tuple[dict[str, Decimal], dict[str, Decimal], dict[str, Decimal]]


def from_units(units: int, *, compact: bool = False) -> Decimal:
    """`units` at eighteen places, exactly. `compact` spells the same value without its zeros."""
    value = Decimal(f"{units}E-18")
    return value.normalize(decimal.Context(prec=60)) if compact else value


@st.composite
def unit_counts(draw: st.DrawFn) -> int:
    """A quantity in units of 1E-18: dust, round numbers, ordinary amounts, and the ceiling."""
    kind = draw(st.sampled_from(["ordinary"] * 5 + ["dust", "whole", "huge", "largest"]))
    if kind == "dust":
        return draw(st.integers(min_value=1, max_value=200))
    if kind == "whole":
        return draw(st.integers(min_value=1, max_value=10**6)) * 10**SCALE
    if kind == "huge":
        return draw(st.integers(min_value=10**30, max_value=MAX_UNITS))
    if kind == "largest":
        return MAX_UNITS
    return draw(st.integers(min_value=1, max_value=10**26))


@st.composite
def pairs(draw: st.DrawFn) -> tuple[int, int]:
    """`(history, held)` in units, drawn so that every status and the boundary are common.

    Independent quantities almost never fall within one percent of each other, so most
    shapes are built around the comparison: equal, exactly on the boundary and one or two
    units either side of it, and a few units apart.
    """
    shape = draw(st.sampled_from(SHAPES))
    if shape == "nothing":
        return 0, 0
    if shape == "history only":
        return draw(unit_counts()), 0
    if shape == "held only":
        return 0, draw(unit_counts())
    if shape == "equal":
        units = draw(unit_counts())
        return units, units
    if shape == "boundary":
        # larger = 100k units, smaller = 99k + delta: delta = 0 is exactly one percent.
        k = draw(st.integers(min_value=1, max_value=MAX_UNITS // 100))
        delta = draw(st.sampled_from([-2, -1, 0, 0, 1, 2]))
        larger, smaller = 100 * k, max(99 * k + delta, 0)
        return (smaller, larger) if draw(st.booleans()) else (larger, smaller)
    if shape == "near":
        units = draw(unit_counts())
        other = units + draw(st.integers(min_value=-(units // 50) - 2, max_value=units // 50 + 2))
        return units, min(max(other, 0), MAX_UNITS)
    return draw(unit_counts()), draw(unit_counts())


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    """Three mappings over a small universe, with the held side split between two sources.

    A side holding nothing is sometimes an absent key and sometimes an explicit zero, and an
    amount is sometimes spelled without its trailing zeros: `reconcile` must not care.
    """
    history: dict[str, Decimal] = {}
    wallets: dict[str, Decimal] = {}
    exchanges: dict[str, Decimal] = {}
    for asset in draw(st.lists(st.sampled_from(UNIVERSE), unique=True, max_size=len(UNIVERSE))):
        in_history, held = draw(pairs())
        in_wallets = draw(st.integers(min_value=0, max_value=held))
        on_exchanges = held - in_wallets
        for mapping, units in (
            (history, in_history),
            (wallets, in_wallets),
            (exchanges, on_exchanges),
        ):
            if units or draw(st.booleans()):
                mapping[asset] = from_units(units, compact=draw(st.booleans()))
    return history, wallets, exchanges


@STRONG
@given(scenarios())
def test_every_row_agrees_with_the_oracle(scenario: Scenario) -> None:
    """Every field of every row, and which rows there are, against the `Fraction` rule."""
    assert_agrees_with_the_oracle(*scenario)


@STANDARD
@given(scenarios())
def test_held_minus_history_is_the_difference(scenario: Scenario) -> None:
    """The test plan's first property, stated on the row alone: the three sums balance."""
    for row in reconcile(*scenario):
        held = Fraction(row.wallet_quantity) + Fraction(row.exchange_quantity)
        assert Fraction(row.held_quantity) == held
        assert Fraction(row.difference) == held - Fraction(row.history_quantity)


@STANDARD
@given(scenarios())
def test_the_statuses_partition_the_rows(scenario: Scenario) -> None:
    """Exactly one status holds for a row, and it is decided by the tolerance and the sign.

    `match` exactly when the difference is within one percent of the larger quantity;
    outside it, `history_short` exactly when held exceeds the history and `history_over`
    exactly when the history exceeds held. A row outside the tolerance never has a zero
    difference, so the three cases cover every row and no two overlap.
    """
    for row in reconcile(*scenario):
        history = Fraction(row.history_quantity)
        held = Fraction(row.held_quantity)
        within = abs(held - history) * 100 <= max(history, held)
        claims = {
            MATCH: within,
            HISTORY_SHORT: not within and held > history,
            HISTORY_OVER: not within and held < history,
        }
        assert sum(claims.values()) == 1, row
        assert claims[row.status.value], row


@STANDARD
@given(scenarios())
def test_the_rows_are_sorted_distinct_and_exactly_the_assets_that_qualify(
    scenario: Scenario,
) -> None:
    """Sorted by asset, one row per asset, no cash asset, and no row holding nothing at all."""
    history, wallets, exchanges = scenario
    rows = reconcile(history, wallets, exchanges)
    assets = [row.asset for row in rows]

    assert assets == sorted(assets)
    assert len(set(assets)) == len(assets)
    assert not ORACLE_CASH & set(assets)
    for row in rows:
        assert row.history_quantity != 0 or row.held_quantity != 0, row
    present = {
        asset
        for mapping in (history, wallets, exchanges)
        for asset, quantity in mapping.items()
        if quantity != 0 and asset not in ORACLE_CASH
    }
    assert set(assets) == present


@STANDARD
@given(scenarios())
def test_every_figure_is_a_decimal_at_eighteen_places_and_none_is_negative_but_the_difference(
    scenario: Scenario,
) -> None:
    for row in reconcile(*scenario):
        for figure in figures(row):
            assert isinstance(figure, Decimal)
            assert figure.is_finite()
            assert figure.as_tuple().exponent == -SCALE, figure
        assert row.history_quantity >= 0
        assert row.wallet_quantity >= 0
        assert row.exchange_quantity >= 0
        assert row.held_quantity >= 0


@STANDARD
@given(scenarios())
def test_where_a_balance_is_held_does_not_change_the_comparison(scenario: Scenario) -> None:
    """Swapping the wallets and the exchanges swaps two columns and nothing else."""
    history, wallets, exchanges = scenario
    straight = reconcile(history, wallets, exchanges)
    swapped = reconcile(history, exchanges, wallets)

    assert [
        (row.asset, row.history_quantity, row.held_quantity, row.difference, row.status)
        for row in straight
    ] == [
        (row.asset, row.history_quantity, row.held_quantity, row.difference, row.status)
        for row in swapped
    ]
    assert [row.wallet_quantity for row in straight] == [row.exchange_quantity for row in swapped]


@STANDARD
@given(scenarios(), st.sampled_from([1, 9, 28]))
def test_the_rows_are_the_same_inside_any_decimal_context(
    scenario: Scenario, precision: int
) -> None:
    outside = reconcile(*scenario)

    with decimal.localcontext() as context:
        context.prec = precision
        context.rounding = decimal.ROUND_UP
        inside = reconcile(*scenario)

    assert inside == outside
    assert [figures(row) for row in inside] == [figures(row) for row in outside]


@STANDARD
@given(scenarios(), st.sampled_from(NON_CASH), st.integers(min_value=1, max_value=MAX_UNITS))
def test_a_negative_quantity_anywhere_is_refused(
    scenario: Scenario, asset: str, units: int
) -> None:
    for index in range(3):
        mappings = [dict(mapping) for mapping in scenario]
        mappings[index][asset] = from_units(units).copy_negate()

        with pytest.raises(ValueError, match=r"."):
            reconcile(mappings[0], mappings[1], mappings[2])


# --------------------------------------------------------------------------------------
# The strategy reaches what it claims to
# --------------------------------------------------------------------------------------


def _has_status(status: str) -> Callable[[Scenario], bool]:
    def condition(scenario: Scenario) -> bool:
        return any(row.status == status for row in expected_rows(*scenario))

    return condition


def _on_the_boundary(scenario: Scenario) -> bool:
    """Some row sits exactly at one percent: a difference that is not zero and still matches."""
    return any(
        row.difference != 0 and abs(row.difference) * 100 == max(row.history, row.held)
        for row in expected_rows(*scenario)
    )


def _one_unit_past_the_boundary(scenario: Scenario) -> bool:
    return any(
        0 < abs(row.difference) * 100 - max(row.history, row.held) <= 100 * UNIT
        for row in expected_rows(*scenario)
    )


def _needs_more_than_28_digits(scenario: Scenario) -> bool:
    return any(
        len(quantity.as_tuple().digits) > 28
        for mapping in scenario
        for quantity in mapping.values()
    )


def _omits_a_both_zero_asset(scenario: Scenario) -> bool:
    kept = {row.asset for row in expected_rows(*scenario)}
    named = {asset for mapping in scenario for asset in mapping}
    return bool(named - kept - ORACLE_CASH)


def _names_a_cash_asset(scenario: Scenario) -> bool:
    return any(
        asset in ORACLE_CASH and quantity != 0
        for mapping in scenario
        for asset, quantity in mapping.items()
    )


def _held_in_both_sources(scenario: Scenario) -> bool:
    return any(row.wallet > 0 and row.exchange > 0 for row in expected_rows(*scenario))


REACHABLE: Final[dict[str, Callable[[Scenario], bool]]] = {
    "a match": _has_status(MATCH),
    "a history_short row": _has_status(HISTORY_SHORT),
    "a history_over row": _has_status(HISTORY_OVER),
    "a row exactly on the boundary": _on_the_boundary,
    "a row one unit past the boundary": _one_unit_past_the_boundary,
    "a quantity of more than 28 digits": _needs_more_than_28_digits,
    "an asset omitted for holding nothing": _omits_a_both_zero_asset,
    "a cash asset with a balance": _names_a_cash_asset,
    "a balance split between wallets and exchanges": _held_in_both_sources,
}


@pytest.mark.parametrize("label", sorted(REACHABLE))
def test_the_strategy_reaches(label: str) -> None:
    """A property over scenarios that never contain the case proves nothing about the case.

    `find` returns the first scenario that has it and raises if 3000 do not, so an edit to
    the strategy that quietly stopped generating boundary rows fails here.
    """
    find(scenarios(), REACHABLE[label], settings=FIND_SETTINGS)


def test_the_find_harness_can_fail() -> None:
    """The control: a condition no scenario meets is reported, not silently passed."""
    from hypothesis.errors import NoSuchExample

    with pytest.raises(NoSuchExample):
        find(
            scenarios(),
            lambda scenario: any(row.asset == "NOSUCH" for row in expected_rows(*scenario)),
            settings=settings(FIND_SETTINGS, max_examples=50),
        )


# --------------------------------------------------------------------------------------
# The oracle is checked against the spec's rows too
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("history", "held", "status"),
    [
        ("99", "100", MATCH),
        (ONE_UNIT_UNDER_99, "100", HISTORY_SHORT),
        ("100", "99", MATCH),
        ("100", ONE_UNIT_UNDER_99, HISTORY_OVER),
        ("0", "1E-18", HISTORY_SHORT),
        ("1E-18", "0", HISTORY_OVER),
        ("5", "5", MATCH),
    ],
)
def test_the_oracle_itself_gives_the_hand_worked_answers(
    history: str, held: str, status: str
) -> None:
    """An oracle nobody checked is a second implementation, not a second opinion."""
    assert expected_status(Fraction(Decimal(history)), Fraction(Decimal(held))) == status


def test_the_oracle_shares_nothing_with_the_code_under_test() -> None:
    """Its cash set and its tolerance are spelled here, and agree with the constants today."""
    assert ORACLE_CASH == DEFAULT_CASH_ASSETS
    assert Fraction(RECONCILIATION_TOLERANCE_PCT) == ORACLE_TOLERANCE_PCT
