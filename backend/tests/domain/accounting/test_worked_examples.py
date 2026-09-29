"""Criterion 11: every worked example in `docs/accounting.md` is a test, so the two cannot drift.

Two bindings, because either alone leaves a gap:

* **The document's own tables are read and compared with the engine.** Each example's
  result table (`| quantity | 1.5 |`, `| | BTC | KAS |` ...) is parsed out of the Markdown
  and every cell is checked against the engine's position. Editing a figure in the document
  without the engine agreeing is therefore a red test, and so is a table row this module
  does not know how to check.
* **The prose figures are asserted here by hand**: the warnings, the flags the text names,
  `unallocated_costs`, and example 6's per-sale basis, which is in a table of its own shape.

Each test is named after its example, and `test_the_document_has_exactly_these_examples`
pins the list of headings, so an example added to the document without a test here fails.
Every example is also replayed through the `Fraction` oracle, so the document is checked
against the independent reading of the spec too.

Times are 2026-01-01 UTC, as the document says. Sources and ids are this module's choice:
`bitget` and `e1`... for fills, `manual` and `m1`... for adjustments.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from portfolio.domain.accounting import NegativeInventory, UnattributedFee, replay
from tests.domain.accounting import oracle
from tests.domain.accounting.support import (
    CONFIG,
    adjust,
    buy,
    comparable,
    engine_to_json,
    flag_names,
    key,
    move,
    oracle_replay,
    position,
    sell,
)

if TYPE_CHECKING:
    from portfolio.domain.accounting import AccountingResult
    from portfolio.domain.accounting.events import AccountingEvent

DOCUMENT: Final = Path(__file__).resolve().parents[4] / "docs" / "accounting.md"

EXAMPLES: Final = {
    1: "Two buys and a partial sale",
    2: "A fee in the quote, and a fee in the base",
    3: "A fee in a third asset, carried at cost",
    4: "A fee in a third asset nobody bought",
    5: "Selling more than the history holds",
    6: "Emptying a pool takes the rounding residue with it",
    7: "A crypto-to-crypto swap carries the cost over",
    8: "An opening balance without a cost",
    9: "An opening balance with a cost resolves the shortfall",
    10: "A transfer changes nothing",
    11: "A stablecoin conversion",
}

#: The result-table row labels the document uses, and the position field each one is.
ROW_FIELDS: Final = {
    "quantity": "quantity",
    "cost basis": "cost_basis",
    "average cost": "average_cost",
    "realized P&L": "realized_pnl",
    "unmatched proceeds": "unmatched_proceeds",
    "unknown-basis quantity": "unknown_basis_quantity",
    "flags": "flags",
}


# --------------------------------------------------------------------------------------
# Reading the document
# --------------------------------------------------------------------------------------


def sections() -> dict[int, tuple[str, str]]:
    """`{number: (title, body)}` for each `### N. Title` under `## Worked examples`."""
    text = DOCUMENT.read_text(encoding="utf-8")
    worked = text.split("## Worked examples", 1)[1].split("\n## ", 1)[0]
    found: dict[int, tuple[str, str]] = {}
    for match in re.finditer(r"^### (\d+)\. (.+)$", worked, flags=re.MULTILINE):
        start = match.end()
        following = re.search(r"^### ", worked[start:], flags=re.MULTILINE)
        body = worked[start : start + following.start()] if following else worked[start:]
        found[int(match.group(1))] = (match.group(2).strip(), body)
    return found


def cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def position_tables(body: str) -> dict[str, dict[str, str]]:
    """`{asset: {row label: cell}}` from every result table in one example's body.

    A result table is either `| BTC | |` (one asset, its values in the second column) or
    `| | BTC | KAS |` (one asset per column). Event tables (`| Time | Event |`) and example
    6's `| Sale | ... |` table have neither header, and are left to the prose assertions.
    """
    tables: dict[str, dict[str, str]] = {}
    lines = body.splitlines()
    index = 0
    while index < len(lines):
        if not lines[index].startswith("|"):
            index += 1
            continue
        block = []
        while index < len(lines) and lines[index].startswith("|"):
            block.append(cells(lines[index]))
            index += 1
        header, rows = block[0], block[2:]
        if header[0] == "":
            assets = header[1:]
        elif len(header) == 2 and header[1] == "":
            assets = [header[0]]
        else:
            continue
        for row in rows:
            label, values = row[0], row[1:]
            assert label in ROW_FIELDS, f"a result row this module cannot check: {label!r}"
            for asset, value in zip(assets, values, strict=True):
                tables.setdefault(asset, {})[label] = value
    return tables


def parse_cell(label: str, value: str) -> object:
    if label == "flags":
        return set(re.findall(r"`([A-Z_]+)`", value))
    if value == "—":
        return None
    return Decimal(value.replace(",", ""))


def assert_matches_tables(label: str, body: str, result: AccountingResult) -> int:
    """Every cell of `body`'s result tables equals the engine's figure. Returns the count."""
    checked = 0
    for asset, rows in position_tables(body).items():
        found = position(result, asset)
        for row, value in rows.items():
            expected = parse_cell(row, value)
            actual: object = (
                flag_names(found) if row == "flags" else getattr(found, ROW_FIELDS[row])
            )
            assert actual == expected, f"{label}, {asset}, {row}: {actual} != {value}"
            checked += 1
    return checked


def assert_matches_document(number: int, result: AccountingResult) -> int:
    """Example `number`'s tables, as committed, against the engine's result."""
    return assert_matches_tables(f"example {number}", sections()[number][1], result)


def replayed(*events: AccountingEvent) -> AccountingResult:
    """The engine's result, after checking the oracle reads the example the same way."""
    result = replay(list(events), CONFIG)
    assert engine_to_json(result) == oracle.result_to_json(oracle_replay(events))
    return result


def test_the_document_has_exactly_these_examples() -> None:
    """A new example in the document needs a test here; a renamed one needs this list edited."""
    assert {number: title for number, (title, _) in sections().items()} == EXAMPLES


def test_every_example_has_a_test_named_after_it() -> None:
    names = [name for name in globals() if name.startswith("test_example_")]

    assert sorted(int(name.split("_")[2]) for name in names) == list(EXAMPLES)


# --------------------------------------------------------------------------------------
# The examples
# --------------------------------------------------------------------------------------


def test_example_01_two_buys_and_a_partial_sale() -> None:
    result = replayed(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        buy(key(11, "e2"), "BTC", "USDT", "1", "40000"),
        sell(key(12, "e3"), "BTC", "USDT", "0.5", "25000"),
    )

    assert assert_matches_document(1, result) == 4
    assert position(result, "BTC").average_cost == Decimal("35000")
    assert result.warnings == ()


def test_example_02_a_fee_in_the_quote_and_a_fee_in_the_base() -> None:
    result = replayed(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000", "30", "USDT"),
        buy(key(11, "e2"), "KAS", "USDT", "1000", "100", "1", "KAS"),
        sell(key(12, "e3"), "BTC", "USDT", "1", "33000", "33", "USDT"),
    )

    assert assert_matches_document(2, result) == 8
    # The prose: 30,030 went out for the BTC; proceeds were 32,967.
    assert result.lots[0].cost_basis == Decimal("30030")
    assert position(result, "BTC").realized_pnl == Decimal("32967") - Decimal("30030")


def test_example_03_a_fee_in_a_third_asset_carried_at_cost() -> None:
    result = replayed(
        buy(key(10, "e1"), "BGB", "USDT", "10", "10"),
        buy(key(11, "e2"), "BTC", "USDT", "1", "30000", "2", "BGB"),
    )

    assert assert_matches_document(3, result) == 8
    assert result.warnings == ()


def test_example_04_a_fee_in_a_third_asset_nobody_bought() -> None:
    result = replayed(buy(key(11, "e2"), "BTC", "USDT", "1", "30000", "2", "BGB"))

    assert assert_matches_document(4, result) == 6
    assert result.warnings == (
        NegativeInventory(key(11, "e2"), "BGB", Decimal("2")),
        UnattributedFee(key(11, "e2"), "BGB", Decimal("2"), "BTC"),
    )


def test_example_05_selling_more_than_the_history_holds() -> None:
    result = replayed(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        sell(key(12, "e2"), "BTC", "USDT", "1.5", "60000"),
    )

    assert assert_matches_document(5, result) == 4
    assert result.warnings == (NegativeInventory(key(12, "e2"), "BTC", Decimal("0.5")),)
    assert flag_names(position(result, "BTC")) == {"HISTORY_INCOMPLETE"}


def test_example_06_emptying_a_pool_takes_the_rounding_residue_with_it() -> None:
    """The per-sale table, by prefix: basis given up and left, and the realized total."""
    events: list[AccountingEvent] = [
        buy(key(10, "e1"), "KAS", "USDT", "3", "1"),
        sell(key(11, "e2"), "KAS", "USDT", "1", "0.5"),
        sell(key(12, "e3"), "KAS", "USDT", "1", "0.5"),
        sell(key(13, "e4"), "KAS", "USDT", "1", "0.5"),
    ]
    basis_left = [position(replayed(*events[: size + 2]), "KAS").cost_basis for size in range(3)]
    realized = [position(replayed(*events[: size + 2]), "KAS").realized_pnl for size in range(3)]

    assert basis_left == [
        Decimal("0.666666666666666667"),
        Decimal("0.333333333333333333"),
        Decimal("0"),
    ]
    given_up = [Decimal(1) - basis_left[0], basis_left[0] - basis_left[1], basis_left[1]]
    assert given_up == [
        Decimal("0.333333333333333333"),
        Decimal("0.333333333333333334"),
        Decimal("0.333333333333333333"),
    ]
    assert sum(given_up) == 1
    per_sale = [realized[0], realized[1] - realized[0], realized[2] - realized[1]]
    assert per_sale == [
        Decimal("0.166666666666666667"),
        Decimal("0.166666666666666666"),
        Decimal("0.166666666666666667"),
    ]
    final = position(replayed(*events), "KAS")
    assert (final.quantity, final.cost_basis, final.realized_pnl) == (
        Decimal(0),
        Decimal(0),
        Decimal("0.5"),
    )


def test_example_07_a_crypto_to_crypto_swap_carries_the_cost_over() -> None:
    result = replayed(
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        buy(key(11, "e2"), "KAS", "BTC", "100000", "0.5"),
    )

    assert assert_matches_document(7, result) == 8


def test_example_08_an_opening_balance_without_a_cost() -> None:
    events: list[AccountingEvent] = [
        adjust(key(9, "m1", "manual"), "BTC", "2", None),
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        sell(key(12, "e2"), "BTC", "USDT", "1.5", "60000"),
    ]
    before_sale = position(replayed(*events[:2]), "BTC")
    result = replayed(*events)

    assert assert_matches_document(8, result) == 7
    # "Before the sale": 3 held, 2 of unknown cost, an average of 30,000 and not 10,000.
    assert before_sale.quantity == 3
    assert before_sale.unknown_basis_quantity == 2
    assert before_sale.average_cost == Decimal("30000")
    assert flag_names(before_sale) == {"UNKNOWN_BASIS"}
    assert result.warnings == ()


def test_example_09_an_opening_balance_with_a_cost_resolves_the_shortfall() -> None:
    result = replayed(
        adjust(key(9, "m1", "manual"), "BTC", "0.5", "25000"),
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        sell(key(12, "e2"), "BTC", "USDT", "1.5", "60000"),
    )

    assert assert_matches_document(9, result) == 3
    # "There is no warning and no flag."
    assert result.warnings == ()
    assert flag_names(position(result, "BTC")) == set()


def test_example_10_a_transfer_changes_nothing() -> None:
    """Example 1 with a transfer at 11:00: every figure and warning equal, count and digest not."""
    example_1: list[AccountingEvent] = [
        buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
        buy(key(11, "e2"), "BTC", "USDT", "1", "40000"),
        sell(key(12, "e3"), "BTC", "USDT", "0.5", "25000"),
    ]
    transfer = move(key(11, "w1"), "BTC", "0.5", "bitget", "wallet")

    without = replayed(*example_1)
    with_transfer = replayed(*example_1, transfer)

    assert comparable(with_transfer) == comparable(without)
    assert with_transfer.event_count == without.event_count + 1
    assert with_transfer.input_fingerprint != without.input_fingerprint
    assert assert_matches_document(1, with_transfer) == 4


def test_example_11_a_stablecoin_conversion() -> None:
    result = replayed(buy(key(10, "e1"), "USDC", "USDT", "100", "100", "0.1", "USDT"))

    assert result.positions == ()
    assert result.unallocated_costs == Decimal("0.1")
    assert position_tables(sections()[11][1]) == {}


def test_a_figure_edited_in_the_document_would_fail() -> None:
    """The control on the binding: example 1's body with 7,500 changed to 7,600 does not pass.

    Without it, a parser that silently read no tables would let every test above pass
    while comparing nothing -- the cell counts they assert are the other half of that.
    """
    body = sections()[1][1]
    assert "| realized P&L | 7,500 |" in body
    result = replay(
        [
            buy(key(10, "e1"), "BTC", "USDT", "1", "30000"),
            buy(key(11, "e2"), "BTC", "USDT", "1", "40000"),
            sell(key(12, "e3"), "BTC", "USDT", "0.5", "25000"),
        ],
        CONFIG,
    )

    with pytest.raises(AssertionError, match="realized P&L"):
        assert_matches_tables("edited", body.replace("7,500", "7,600"), result)


def test_an_unknown_row_in_a_result_table_is_refused() -> None:
    """A row nobody taught this module to check fails loudly rather than being skipped."""
    body = "\n".join(["| BTC | |", "|---|---|", "| market value | 1 |", ""])

    with pytest.raises(AssertionError, match="market value"):
        position_tables(body)
