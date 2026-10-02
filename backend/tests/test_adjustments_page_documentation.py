"""Criterion 18 of #111 (spec 027): the documents name the page as the way to enter adjustments.

Checked as substance rather than as prose, the way `tests/test_reconciliation_documentation.py`
and `tests/test_unmatched_proceeds_documentation.py` check theirs. The wording is free to
change. What cannot change without failing a test here is what the spec says each document
must state:

* **`docs/accounting.md`, "Recording what the history does not show"**: the Adjustments page
  is how an adjustment is entered, edited and deleted. `/api/docs` and the console line for
  the delete are still there, as the alternative, **after** the page.
* **`docs/operations.md`, section 15**: the same, in the part on recording an opening
  balance; the new endpoint, `GET /api/accounting/first-trades`, in the endpoint table beside
  the four it joins; and the troubleshooting row about deleting from `/api/docs` points to
  the page **first**.

* **Ruling R6: no row sends the owner to the API where the page does the job.** Every
  troubleshooting row whose remedy is about an adjustment names the page, none of them
  prescribes a `PUT`, and the `UnconvertibleAdjustmentError` row of section 15 names the
  page too.
* **Ruling R8: the offered date is not for every gap.** Where `docs/accounting.md` says how
  to date an opening balance, it names the endpoint the page's date comes from, says that
  date is per asset across every venue and fits the coins already held then, that coins
  acquired later carry their own date, and that nothing warns when it is got wrong.
* **Ruling R11: the documentation says where an adjustment's id is.** The page shows none,
  so the `UnconvertibleAdjustmentError` row sends the owner to `GET
  /api/accounting/adjustments` in `/api/docs` to find the one the log names.

Each check reads one section or one table row, so a mention of the page somewhere else in
either document would not satisfy it.

Until #111 both documents said the owner enters adjustments through the API, by way of
`/api/docs` and a line pasted into the browser console. That was the whole procedure, and it
is what the first test of each document would have failed on.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
ACCOUNTING_DOC: Final = REPO_ROOT / "docs" / "accounting.md"
OPERATIONS_DOC: Final = REPO_ROOT / "docs" / "operations.md"

RECORDING_HEADING: Final = "## Recording what the history does not show"
SNAPSHOT_HEADING: Final = "## 15. The cost-basis snapshot: recomputing it, and reading it"
OPENING_BALANCE_HEADING: Final = (
    "### Recording an opening balance, or any acquisition the history does not show"
)
TROUBLESHOOTING_HEADING: Final = "## Troubleshooting"
DATING_HEADING: Final = "### Dating an opening balance"

PAGE: Final = "Adjustments page"
SWAGGER: Final = "/api/docs"
#: The start of the one line that deletes an adjustment without the page.
CONSOLE_DELETE: Final = "await fetch('/api/accounting/adjustments/<id>', {method: 'DELETE'"

ENDPOINT_TABLE_HEADER: Final = "| Method | Path | Does |"
FIRST_TRADES_PATH: Final = "/api/accounting/first-trades"
#: The four rows the table had before #111, as (method, path).
ADJUSTMENT_ENDPOINTS: Final = [
    ("GET", "/api/accounting/adjustments"),
    ("POST", "/api/accounting/adjustments"),
    ("PUT", "/api/accounting/adjustments/{id}"),
    ("DELETE", "/api/accounting/adjustments/{id}"),
]

#: What a paragraph has to say the page does. Stems, so `enters`, `Enter it` and `entered`
#: all satisfy the first: the sentence is the author's, the three verbs are the spec's.
PAGE_DOES: Final = {
    "enter": re.compile(r"\b(enter|record)", re.IGNORECASE),
    "edit": re.compile(r"\bedit", re.IGNORECASE),
    "delete": re.compile(r"\bdelet", re.IGNORECASE),
}


def read(path: Path) -> str:
    assert path.is_file(), f"{path} does not exist"
    return path.read_text(encoding="utf-8")


def section(text: str, heading: str) -> str:
    """One section as written, from its heading to the next heading of its level or above."""
    assert text.count(heading + "\n") == 1, f"{heading!r} appears {text.count(heading)} times"
    start = text.index(heading + "\n")
    depth = len(heading.split(" ", 1)[0])
    following = re.search(rf"^#{{1,{depth}}} ", text[start + len(heading) :], re.MULTILINE)
    end = start + len(heading) + following.start() if following else len(text)
    return text[start:end]


def paragraphs(text: str) -> list[str]:
    """The blocks a blank line separates, each folded onto one line."""
    return [" ".join(block.split()) for block in re.split(r"\n\s*\n", text) if block.strip()]


def cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def recording_sections() -> dict[str, str]:
    """The part of each document that says how an adjustment is entered."""
    return {
        "accounting": section(read(ACCOUNTING_DOC), RECORDING_HEADING),
        "operations": section(
            section(read(OPERATIONS_DOC), SNAPSHOT_HEADING), OPENING_BALANCE_HEADING
        ),
    }


DOCUMENTS: Final = ("accounting", "operations")


# --------------------------------------------------------------------------------------
# Both documents: the page is the way, and the API is the alternative after it
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("document", DOCUMENTS)
def test_the_document_names_the_adjustments_page(document: str) -> None:
    assert PAGE in recording_sections()[document]


@pytest.mark.parametrize("document", DOCUMENTS)
def test_the_page_is_how_an_adjustment_is_entered_edited_and_deleted(document: str) -> None:
    """One paragraph that names the page says all three, so none is left to the console."""
    naming = [block for block in paragraphs(recording_sections()[document]) if PAGE in block]
    assert naming, "no paragraph names the page"

    said = [
        sorted(verb for verb, pattern in PAGE_DOES.items() if pattern.search(block))
        for block in naming
    ]

    assert sorted(PAGE_DOES) in said, f"no paragraph naming the page says all three: {said}"


@pytest.mark.parametrize("document", DOCUMENTS)
def test_the_api_documentation_and_the_console_line_are_still_there(document: str) -> None:
    """The alternative stays: an owner without the page can still do everything."""
    text = recording_sections()[document]

    assert SWAGGER in text
    assert text.count(CONSOLE_DELETE) == 1, "the console line for the delete"
    assert "403" in text, "why Swagger UI cannot send the delete"


@pytest.mark.parametrize("document", DOCUMENTS)
def test_the_page_comes_before_the_alternative(document: str) -> None:
    """A reader meets the page first, then `/api/docs`, then the console line."""
    text = recording_sections()[document]

    page, swagger, console = text.index(PAGE), text.index(SWAGGER), text.index(CONSOLE_DELETE)

    assert page < swagger < console


def test_the_accounting_document_no_longer_says_the_api_is_how_adjustments_are_entered() -> None:
    """The sentence #111 replaced. Left in beside the new one, the document would name two
    ways as *the* way."""
    text = " ".join(section(read(ACCOUNTING_DOC), RECORDING_HEADING).split())

    assert "enters adjustments through the authenticated API" not in text


# --------------------------------------------------------------------------------------
# docs/operations.md: the endpoint table
# --------------------------------------------------------------------------------------


def endpoint_rows() -> list[list[str]]:
    """The rows of section 15's endpoint table, as their cells."""
    lines = recording_sections()["operations"].splitlines()
    assert lines.count(ENDPOINT_TABLE_HEADER) == 1, "the endpoint table's header"
    start = lines.index(ENDPOINT_TABLE_HEADER) + 2
    rows: list[list[str]] = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        rows.append(cells(line))
    return rows


def test_the_endpoint_table_lists_the_new_endpoint() -> None:
    rows = [row for row in endpoint_rows() if row[1].strip("`") == FIRST_TRADES_PATH]

    assert len(rows) == 1, f"{len(rows)} rows list {FIRST_TRADES_PATH}"
    method, _path, does = rows[0]
    assert method.strip("`") == "GET"
    assert does, "the row says what the endpoint does"


def test_the_endpoint_table_still_lists_the_four_adjustment_endpoints() -> None:
    """The new row was added to the table, not swapped for one of the others."""
    listed = [(row[0].strip("`"), row[1].strip("`")) for row in endpoint_rows()]

    assert [entry for entry in listed if entry in ADJUSTMENT_ENDPOINTS] == ADJUSTMENT_ENDPOINTS
    assert all(len(row) == 3 for row in endpoint_rows())


# --------------------------------------------------------------------------------------
# docs/operations.md: troubleshooting
# --------------------------------------------------------------------------------------


def delete_from_swagger_row() -> list[str]:
    """The troubleshooting row about a delete from `/api/docs` being refused."""
    lines = section(read(OPERATIONS_DOC), TROUBLESHOOTING_HEADING).splitlines()
    rows = [
        cells(line)
        for line in lines
        if line.startswith("|") and SWAGGER in cells(line)[0] and "403" in cells(line)[0]
    ]
    assert len(rows) == 1, f"{len(rows)} troubleshooting rows are about a 403 from {SWAGGER}"
    return rows[0]


def test_the_troubleshooting_row_points_to_the_page_first() -> None:
    """Whoever hits the 403 is told about the page before the console."""
    symptom, cause = delete_from_swagger_row()

    assert re.search(r"\bdelet", symptom, re.IGNORECASE), symptom
    assert PAGE in cause
    assert "console" in cause, "the alternative is still named"
    assert cause.index(PAGE) < cause.index("console")


def troubleshooting_rows() -> list[list[str]]:
    """Every row of the troubleshooting table, as `[symptom, likely cause]`."""
    lines = section(read(OPERATIONS_DOC), TROUBLESHOOTING_HEADING).splitlines()
    rows = [cells(line) for line in lines if line.startswith("|")][2:]
    assert all(len(row) == 2 for row in rows), "a row with a stray column"
    return rows


def test_every_troubleshooting_remedy_about_an_adjustment_names_the_page() -> None:
    """Ruling R6: the document does not send the owner to the API in one row and to the page
    in the next. Three rows have a remedy that is an adjustment: record one, re-date one,
    delete one."""
    about_adjustments = [
        (symptom, cause)
        for symptom, cause in troubleshooting_rows()
        if re.search(r"\badjustment", cause, re.IGNORECASE)
    ]

    assert len(about_adjustments) >= 3, [symptom for symptom, _cause in about_adjustments]
    assert [symptom for symptom, cause in about_adjustments if PAGE not in cause] == []


def test_no_troubleshooting_row_prescribes_a_put() -> None:
    """Ruling R6, from the other side: the verb the page replaced is in no remedy."""
    rows = troubleshooting_rows()

    assert len(rows) > 40, "the control: the table was read"
    assert [symptom for symptom, cause in rows if "`PUT`" in cause] == []


def test_the_unconvertible_adjustment_row_sends_the_owner_to_the_page() -> None:
    """Section 15's table of recompute errors: the one a stored adjustment causes is fixed
    on the page, where the adjustment is corrected or deleted."""
    lines = section(read(OPERATIONS_DOC), SNAPSHOT_HEADING).splitlines()
    rows = [cells(line) for line in lines if line.startswith("| `UnconvertibleAdjustmentError` |")]
    assert len(rows) == 1, f"{len(rows)} rows explain `UnconvertibleAdjustmentError`"
    _error, _meaning, remedy = rows[0]

    assert PAGE in remedy
    assert "`adjustment_id`" in remedy, "which adjustment: the log line still says"
    assert "`PUT`" not in remedy


def test_the_unconvertible_adjustment_row_says_where_an_adjustments_id_is() -> None:
    """Ruling R11: the log names an id and the page shows none, so the row says where the
    ids are listed, and that the page is then used by asset and date."""
    lines = section(read(OPERATIONS_DOC), SNAPSHOT_HEADING).splitlines()
    rows = [cells(line) for line in lines if line.startswith("| `UnconvertibleAdjustmentError` |")]
    assert len(rows) == 1, f"{len(rows)} rows explain `UnconvertibleAdjustmentError`"
    remedy = rows[0][2]

    assert "`GET /api/accounting/adjustments`" in remedy
    assert f"`{SWAGGER}`" in remedy
    assert "`id`" in remedy
    assert re.search(r"does not show ids|shows no ids", remedy), "the page shows no ids"
    assert remedy.index("`GET /api/accounting/adjustments`") < remedy.rindex(PAGE)


# --------------------------------------------------------------------------------------
# docs/accounting.md: which coins the offered date is for (ruling R8)
# --------------------------------------------------------------------------------------


def dating_section() -> str:
    """The section on dating one, folded onto one line so that a wrapped phrase is found."""
    recording = section(read(ACCOUNTING_DOC), RECORDING_HEADING)
    return " ".join(section(recording, DATING_HEADING).split())


def test_the_dating_section_names_where_the_pages_date_comes_from() -> None:
    text = dating_section()

    assert PAGE in text
    assert f"`GET {FIRST_TRADES_PATH}`" in text


def test_the_dating_section_says_which_coins_the_offered_date_is_for() -> None:
    """Per asset across every venue, so right for coins already held at that instant, and
    wrong for coins acquired after it: those carry the date they were acquired."""
    text = dating_section()

    assert re.search(r"per asset", text, re.IGNORECASE)
    assert "every venue" in text
    assert "already held" in text
    assert re.search(r"acquired later", text, re.IGNORECASE)
    assert re.search(r"date they were acquired", text)


def test_the_dating_section_says_what_a_date_too_early_does_and_that_nothing_warns() -> None:
    """The reason the page offers the date and never fills it in: a wrong one changes the
    realized P&L of sales it had no part in, and no flag, warning or check shows it."""
    text = dating_section()

    assert "weighted average" in text
    assert "too early" in text
    assert "realized P&L" in text
    assert re.search(r"nothing warns", text, re.IGNORECASE)
    assert re.search(r"no warning", text, re.IGNORECASE)


# --------------------------------------------------------------------------------------
# The helpers
# --------------------------------------------------------------------------------------


def test_the_section_helper_stops_at_the_next_heading_of_its_level_or_above() -> None:
    """The control on `section`: a `###` section ends at a `##`, and a `##` at a `##`."""
    document = "# T\n\n## A\n\none\n\n### B\n\ntwo\nlines\n\n## C\n\nthree\n\n### D\n\nfour\n"

    assert section(document, "### B") == "### B\n\ntwo\nlines\n\n"
    assert section(document, "## A") == "## A\n\none\n\n### B\n\ntwo\nlines\n\n"
    assert section(document, "### D") == "### D\n\nfour\n"


def test_the_paragraph_helper_splits_on_blank_lines_and_folds_wrapped_ones() -> None:
    """The control on `paragraphs`: a phrase the document wraps is still one phrase."""
    assert paragraphs("the Adjustments\npage is\n\nsecond  block\n\n\n") == [
        "the Adjustments page is",
        "second block",
    ]


def test_the_checks_fail_on_the_text_both_documents_had_before() -> None:
    """The control on the checks: the old paragraph names no page and puts the API first.

    Without it, a pattern loose enough to match any paragraph about adjustments would pass on
    the document #111 set out to change.
    """
    before = (
        "The owner enters adjustments through the authenticated API under "
        "`/api/accounting/adjustments`. While signed in, `/api/docs` works for listing, "
        "creating and replacing them. It cannot delete one."
    )

    assert PAGE not in before
    assert all(pattern.search(before) for name, pattern in PAGE_DOES.items() if name != "edit")
    assert not PAGE_DOES["edit"].search(before)
