"""`domain/exchanges.py`: the vocabulary a `CHECK` constraint mirrors, pinned by hand.

Two enums, and each one's values are pinned against a literal written here rather than
derived from the enum. A set derived from the thing it describes shrinks with it: dropping a
member would drop it from the expectation too, and the test would keep passing while the
`CHECK` constraint in `0006_exchanges` went on admitting a value the application no longer
knows. Adding a venue is a migration as well as an enum edit, and this is the test that
makes the enum half of that impossible to do quietly.

Pure: no fixtures, no I/O.
"""

from __future__ import annotations

import ast
from enum import StrEnum
from pathlib import Path
from typing import Final

from portfolio.domain.exchanges import ExchangeKey, FillSide

#: The two venues V1 imports from, as they are stored in `exchange_accounts.exchange_key`.
EXPECTED_EXCHANGE_KEYS: Final = {"BINGX": "bingx", "BITGET": "bitget"}

#: The two sides a fill can have, as they are stored in `exchange_fills.side`.
EXPECTED_FILL_SIDES: Final = {"BUY": "buy", "SELL": "sell"}

DOMAIN_MODULE: Final = (
    Path(__file__).resolve().parents[3]
    / "backend"
    / "src"
    / "portfolio"
    / "domain"
    / "exchanges.py"
)


def test_the_exchange_keys_are_the_pinned_set() -> None:
    """Names and values both, because the value is what is stored and the name is what is typed."""
    assert {member.name: member.value for member in ExchangeKey} == EXPECTED_EXCHANGE_KEYS


def test_the_fill_sides_are_the_pinned_set() -> None:
    assert {member.name: member.value for member in FillSide} == EXPECTED_FILL_SIDES


def test_both_enums_are_str_enums_so_the_stored_value_is_the_member() -> None:
    """A `StrEnum` member *is* its string, so the row, the `CHECK` and the code share one spelling.

    A plain `Enum` would store `ExchangeKey.BITGET` through `str()` as `"ExchangeKey.BITGET"`
    in any path that forgot `.value`, and the `CHECK` would then refuse a row the code
    believed was valid.
    """
    assert issubclass(ExchangeKey, StrEnum)
    assert issubclass(FillSide, StrEnum)
    assert ExchangeKey("bitget") is ExchangeKey.BITGET
    assert FillSide("sell") is FillSide.SELL
    assert f"{ExchangeKey.BINGX}" == "bingx"
    assert str(FillSide.BUY) == "buy"


def test_the_domain_module_imports_nothing_from_the_application() -> None:
    """`domain` imports nothing: CLAUDE.md rule 4, checked on this module's own source.

    `import-linter` enforces the layering; this is the narrower claim that the vocabulary
    module needs nothing but `enum` and typing machinery. The positive companion asserts the
    walk found the `enum` import, so an empty or unreadable file cannot pass.
    """
    tree = ast.parse(DOMAIN_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))

    assert "enum" in imported
    assert imported <= {"__future__", "enum", "typing"}, sorted(imported)
