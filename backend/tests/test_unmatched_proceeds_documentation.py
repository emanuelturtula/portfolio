"""Criterion 11 of #108 (spec 026): the two documents say what the unmatched total covers.

Checked as substance rather than as prose, the way `tests/test_reconciliation_documentation.py`
checks the holdings check's documents. The wording is free to change. What cannot change
without failing a test here:

* **`docs/accounting.md` says it where `unmatched_proceeds` is defined** -- the row of the
  pool's fields -- that `GET /api/accounting/positions` also serves its total over every
  position, and that the dashboard shows that total beside realized P&L.
* **`docs/accounting.md` says it again where the endpoint's totals are described**: the
  totals leave positions out, and `totals.unmatched_proceeds` is named as one that does not.
* **`docs/operations.md` says it in the section on reading the positions**: the figure covers
  every position, as `realized_pnl` does.
* **Both say the figure is signed**, because that is the one thing about it a reader would
  not guess: proceeds are net of fees. `tests/domain/accounting/test_replay.py` drives the
  engine to a negative one, so the sentence is a fact and not a caution.

A paragraph that mentioned the field somewhere else in either document would not satisfy
these: each check reads one section, or one table row.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
ACCOUNTING_DOC: Final = REPO_ROOT / "docs" / "accounting.md"
OPERATIONS_DOC: Final = REPO_ROOT / "docs" / "operations.md"

MODEL_HEADING: Final = "## The model in one page"
SERVED_HEADING: Final = "## Where the figures are stored and served"
READING_HEADING: Final = "### Reading the positions"

ENDPOINT: Final = "GET /api/accounting/positions"


def read(path: Path) -> str:
    assert path.is_file(), f"{path} does not exist"
    return path.read_text(encoding="utf-8")


def section(text: str, heading: str) -> str:
    """One section, from its heading to the next heading of its level or above, on one line.

    Line breaks are folded into spaces, so a phrase the document wraps is still found.
    """
    assert text.count(heading + "\n") == 1, f"{heading!r} appears {text.count(heading)} times"
    start = text.index(heading + "\n")
    depth = len(heading.split(" ", 1)[0])
    following = re.search(rf"^#{{1,{depth}}} ", text[start + len(heading) :], re.MULTILINE)
    end = start + len(heading) + following.start() if following else len(text)
    return " ".join(text[start:end].split())


def definition_row() -> str:
    """The row of the pool's field table that defines `unmatched_proceeds`."""
    lines = read(ACCOUNTING_DOC).splitlines()
    start = lines.index(MODEL_HEADING)
    rows = [line for line in lines[start:] if line.startswith("| `unmatched_proceeds` |")]
    assert len(rows) == 1, f"{len(rows)} rows define `unmatched_proceeds`"
    return rows[0]


# --------------------------------------------------------------------------------------
# docs/accounting.md
# --------------------------------------------------------------------------------------


def test_the_definition_says_the_endpoint_serves_the_total_over_every_position() -> None:
    row = definition_row()

    assert ENDPOINT in row
    assert re.search(r"\btotal\b", row), "the row does not mention a total"
    assert "every position" in row


def test_the_definition_says_the_dashboard_shows_it_beside_realized_pnl() -> None:
    row = definition_row()

    assert "dashboard" in row
    assert "beside realized P&L" in row


def test_the_definition_is_in_the_models_table_and_still_says_what_the_figure_is() -> None:
    """The sentence was added to the definition, not swapped for it."""
    row = definition_row()

    assert row in section_lines(MODEL_HEADING)
    assert "cost is unknown" in row
    assert "Kept out of realized P&L" in row


def section_lines(heading: str) -> list[str]:
    """The raw lines of one `##` section of `docs/accounting.md`."""
    lines = read(ACCOUNTING_DOC).splitlines()
    start = lines.index(heading)
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return lines[start:end]


def test_the_served_figures_name_the_two_totals_that_cover_every_position() -> None:
    """Where the document says the totals leave positions out, it says which two do not."""
    text = section(read(ACCOUNTING_DOC), SERVED_HEADING)

    assert ENDPOINT in text
    assert "`totals.unmatched_proceeds`" in text
    assert "`totals.realized_pnl`" in text
    covering = re.search(r"`totals\.realized_pnl` and `totals\.unmatched_proceeds`[^.]*\.", text)
    assert covering is not None, "the two totals are not named together"
    assert re.search(r"\*{0,2}every\*{0,2} position", covering.group(0)), covering.group(0)
    assert "held or closed" in covering.group(0)
    assert "left out" in covering.group(0)


def test_the_served_figures_say_when_the_dashboard_shows_them() -> None:
    text = section(read(ACCOUNTING_DOC), SERVED_HEADING)

    assert "dashboard" in text
    assert "beside realized P&L" in text
    assert "names the assets" in text


# --------------------------------------------------------------------------------------
# docs/operations.md
# --------------------------------------------------------------------------------------


def test_the_operations_document_says_the_total_covers_every_position_as_realized_pnl_does() -> (
    None
):
    text = section(read(OPERATIONS_DOC), READING_HEADING)

    assert "`totals.unmatched_proceeds`" in text
    assert "`totals.realized_pnl`" in text
    assert re.search(r"cover \*{0,2}every\*{0,2} position", text), "what the two totals cover"
    assert "held or closed" in text
    assert "excluded" in text


def test_the_operations_document_says_what_the_figure_is() -> None:
    """An operator reading the totals is told what the number is, not only what it spans."""
    text = section(read(OPERATIONS_DOC), READING_HEADING)

    assert "units with no known cost" in text
    assert "kept out of realized P&L" in text


# --------------------------------------------------------------------------------------
# Both: the figure is signed
# --------------------------------------------------------------------------------------


def test_both_documents_say_the_figure_is_signed_and_why() -> None:
    row = definition_row()
    served = section(read(ACCOUNTING_DOC), SERVED_HEADING)
    operations = section(read(OPERATIONS_DOC), READING_HEADING)

    for name, text in (("the definition", row), ("operations", operations)):
        assert "signed" in text, name
        assert re.search(r"net of (every )?fees?", text, re.IGNORECASE), name
        assert "third asset" in text, name
    assert "signed" in served


def test_the_section_helper_stops_at_the_next_heading_of_its_level_or_above() -> None:
    """The control on `section`: a `###` section ends at a `##`, and a `##` at a `##`."""
    document = "# T\n\n## A\n\none\n\n### B\n\ntwo\nlines\n\n## C\n\nthree\n\n### D\n\nfour\n"

    assert section(document, "### B") == "### B two lines"
    assert section(document, "## A") == "## A one ### B two lines"
    assert section(document, "### D") == "### D four"
