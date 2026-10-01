"""Criterion 9 of #104 (spec 025): the three documents say what the holdings check does.

Checked as substance rather than as prose, the way `tests/providers/test_documentation.py`
checks `docs/providers.md`. The wording is free to change. What cannot change without
failing a test here:

* **`docs/accounting.md` explains the check and what each direction means**, and its worked
  examples are *evaluated*: every row of its table is run through `reconcile`, and through
  the same rule written in `fractions.Fraction`, so an example somebody wrote from memory
  cannot disagree with the code. Its sample response is parsed and held to the shape the
  endpoint serves.
* **`docs/providers.md` records both endpoints, their sources, and what is not established**:
  the two things the spec says were designed around rather than known.
* **`docs/operations.md` says only the spot account is read**, and names each log event with
  exactly the fields the sync writes -- which the sync tests pin from the other side.
* **Both say which readings are compared (R9)**: a reading more than twenty-four hours old,
  a venue whose last read failed and one whose fill sync is not `ok` add nothing, and each of
  the four reasons is explained, in the order the service tests them.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

import pytest

from portfolio.domain.accounting import reconcile
from portfolio.services.reconciliation import MAX_READING_AGE_HOURS, NotComparedReason

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
ACCOUNTING_DOC: Final = REPO_ROOT / "docs" / "accounting.md"
PROVIDERS_DOC: Final = REPO_ROOT / "docs" / "providers.md"
OPERATIONS_DOC: Final = REPO_ROOT / "docs" / "operations.md"

CHECK_HEADING: Final = "## Checking the history against the balances held"
EXAMPLES_HEADER: Final = "| History | Wallets | Exchanges | Held | Difference | Status |"
STATUSES: Final = ("match", "history_short", "history_over")
#: R9's four reasons, written out in the order the service tests them.
REASONS: Final = ("read_failed", "never_read", "sync_failed", "out_of_date")


def read(path: Path) -> str:
    assert path.is_file(), f"{path} does not exist"
    return path.read_text(encoding="utf-8")


def section(text: str, heading: str) -> str:
    """The text of one `##` section, from its heading to the next heading of that level."""
    assert text.count(heading + "\n") == 1, f"{heading!r} appears {text.count(heading)} times"
    start = text.index(heading + "\n")
    level = heading.split(" ", 1)[0]
    following = re.search(rf"^{re.escape(level)} ", text[start + len(heading) :], re.MULTILINE)
    end = start + len(heading) + following.start() if following else len(text)
    return text[start:end]


def cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def example_rows() -> list[list[str]]:
    """The rows of the document's worked-example table, as their cells."""
    lines = section(read(ACCOUNTING_DOC), CHECK_HEADING).splitlines()
    assert EXAMPLES_HEADER in lines, "the example table's header changed or went missing"
    start = lines.index(EXAMPLES_HEADER) + 2
    rows: list[list[str]] = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        rows.append(cells(line))
    return rows


def expected_status(history: Fraction, held: Fraction) -> str:
    """The spec's rule in exact rationals: the oracle `test_reconciliation.py` uses."""
    difference = held - history
    if abs(difference) * 100 <= max(history, held):
        return "match"
    return "history_short" if difference > 0 else "history_over"


# --------------------------------------------------------------------------------------
# docs/accounting.md
# --------------------------------------------------------------------------------------


def test_the_accounting_document_has_the_section_and_names_the_endpoint() -> None:
    text = section(read(ACCOUNTING_DOC), CHECK_HEADING)

    assert "GET /api/accounting/reconciliation" in text
    assert "docs/specs/025-holdings-reconciliation.md" in text
    assert "portfolio.domain.accounting.reconcile" in text


def test_the_accounting_document_explains_each_direction() -> None:
    """Each status has a row saying when it applies and what it means, and the two
    directions are said not to be symmetric: one is a finding and the other is not."""
    text = section(read(ACCOUNTING_DOC), CHECK_HEADING)
    rows = {
        cells(line)[0].strip("`"): cells(line)
        for line in text.splitlines()
        if line.startswith("| `") and len(cells(line)) == 3
    }

    assert set(rows) >= set(STATUSES), sorted(rows)
    assert "A finding" in rows["history_short"][2]
    assert "Not a finding" in rows["history_over"][2]
    assert "lower bound" in text
    assert "spot" in text, "only the spot account is read, and the document says so"
    assert "max(history_quantity, held_quantity)" in text
    assert "one percent" in text.lower()
    assert "opening balance" in text.lower(), "how a history_short is resolved"


def test_the_accounting_documents_examples_are_what_reconcile_answers() -> None:
    """Every row of the table, evaluated: the held quantity, the difference and the status.

    An example worked by hand is the first thing to go stale when a rule moves. Each row is
    run through `reconcile` and, independently, through the rule in exact rationals.
    """
    rows = example_rows()
    assert len(rows) >= 6, "the table lost rows"

    for history, wallets, exchanges, held, difference, status_cell in rows:
        (row,) = reconcile(
            {"KAS": Decimal(history)}, {"KAS": Decimal(wallets)}, {"KAS": Decimal(exchanges)}
        )
        documented = re.match(r"`([a-z_]+)`", status_cell)
        assert documented is not None, status_cell

        assert row.held_quantity == Decimal(held), (history, wallets, exchanges)
        assert row.difference == Decimal(difference), (history, wallets, exchanges)
        assert difference.startswith(("+", "-")), "the difference is written with its sign"
        assert row.status.value == documented.group(1), (history, wallets, exchanges)
        assert documented.group(1) == expected_status(
            Fraction(Decimal(history)), Fraction(Decimal(held))
        )


def test_the_examples_cover_every_status_and_the_exact_boundary() -> None:
    """A table of six `history_short` rows would evaluate correctly and teach nothing."""
    rows = example_rows()
    statuses = {re.match(r"`([a-z_]+)`", row[5]).group(1) for row in rows}  # type: ignore[union-attr]
    on_the_boundary = [
        row
        for row in rows
        if abs(Fraction(Decimal(row[4]))) * 100
        == max(Fraction(Decimal(row[0])), Fraction(Decimal(row[3])))
    ]

    assert statuses == set(STATUSES)
    assert on_the_boundary, "no example sits exactly at one percent"
    assert any(Decimal(row[0]) == 0 for row in rows), "no example has nothing in the history"


def sample_response() -> dict[str, Any]:
    text = section(read(ACCOUNTING_DOC), CHECK_HEADING)
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert len(blocks) == 1, "the section has exactly one sample response"
    parsed: dict[str, Any] = json.loads(blocks[0])
    return parsed


def test_the_accounting_documents_sample_response_has_the_shape_that_is_served() -> None:
    """The keys the endpoint tests pin, every quantity a string, and a row `reconcile` gives."""
    sample = sample_response()

    assert set(sample) == {
        "computed_at",
        "tolerance_pct",
        "assets",
        "max_reading_age_hours",
        "last_recompute",
        "exchanges",
        "wallets",
    }
    assert sample["tolerance_pct"] == "1"
    assert sample["max_reading_age_hours"] == MAX_READING_AGE_HOURS == 24
    assert set(sample["last_recompute"]) == {"at", "outcome", "error"}
    (asset,) = sample["assets"]
    assert set(asset) == {
        "asset",
        "history_quantity",
        "wallet_quantity",
        "exchange_quantity",
        "held_quantity",
        "difference",
        "status",
    }
    quantities = {name: value for name, value in asset.items() if name not in ("asset", "status")}
    for name, value in quantities.items():
        assert isinstance(value, str), f"{name} is written as a JSON number"
        assert re.fullmatch(r"-?\d+\.\d{18}", value), f"{name} = {value}"
    (row,) = reconcile(
        {asset["asset"]: Decimal(asset["history_quantity"])},
        {asset["asset"]: Decimal(asset["wallet_quantity"])},
        {asset["asset"]: Decimal(asset["exchange_quantity"])},
    )
    assert str(row.held_quantity) == asset["held_quantity"]
    assert str(row.difference) == asset["difference"]
    assert row.status.value == asset["status"]
    for exchange in sample["exchanges"]:
        assert set(exchange) == {
            "exchange_key",
            "balances_read_at",
            "balances_error",
            "not_compared_reason",
        }
    assert set(sample["wallets"]) == {"compared", "stale", "unread", "oldest_observed_at"}


def reason_rows(text: str) -> dict[str, list[str]]:
    """The rows of a table whose first cell is one of R9's four reasons, by reason."""
    return {
        cells(line)[0].strip("`"): cells(line)
        for line in text.splitlines()
        if line.startswith("| `") and cells(line)[0].strip("`") in REASONS
    }


def test_the_accounting_document_says_a_reading_is_compared_only_while_it_is_current() -> None:
    """R9, in the document the owner reads: an out-of-date reading is left out, why, and
    what "current" means for a venue and for a wallet."""
    text = section(read(ACCOUNTING_DOC), CHECK_HEADING)

    assert "A reading that is out of date" in text
    assert "A venue's reading is current" in text
    assert "A wallet's reading is current" in text
    assert "at most 24 hours old" in text
    assert "`max_reading_age_hours`" in text
    for count in ("`compared`", "`stale`", "`unread`"):
        assert count in text, count
    assert "`last_recompute`" in text


def test_the_accounting_document_explains_each_reason_a_venue_is_not_compared() -> None:
    """All four, in the order the service tests them: the first that applies is the answer."""
    text = section(read(ACCOUNTING_DOC), CHECK_HEADING)
    rows = reason_rows(text)

    assert list(rows) == [reason.value for reason in NotComparedReason] == list(REASONS)
    assert "first of these that applies" in text
    assert all(len(row) == 2 and row[1] for row in rows.values())


def test_the_operations_document_explains_each_reason_and_what_to_do_about_it() -> None:
    rows = reason_rows(read(OPERATIONS_DOC))

    assert list(rows) == list(REASONS)
    assert all(len(row) == 3 and row[1] and row[2] for row in rows.values()), rows


def test_the_operations_document_states_the_twenty_four_hour_rule() -> None:
    text = read(OPERATIONS_DOC)

    assert "24-hour rule" in text
    assert "at most **24 hours** old" in text
    assert "`max_reading_age_hours`" in text
    assert "`not_compared_reason`" in text
    for count in ("`compared`", "`stale`", "`unread`"):
        assert count in text, count
    assert "`last_recompute`" in text


def test_both_documents_state_the_window_between_two_readings_as_it_is() -> None:
    """R10: the gap that remains is minutes while both syncs run, and up to twenty-four
    hours when a source has stopped being read without a recorded failure.

    R9 bounded the stale-reading cases; it did not remove them. A document that only says
    "minutes" tells the owner a double count clears itself by the next sync, and one whose
    source has silently stopped does not.
    """
    accounting = section(read(ACCOUNTING_DOC), CHECK_HEADING)
    operations = read(OPERATIONS_DOC)

    assert "Minutes, while both syncs are running" in accounting
    assert "Up to 24 hours, when a source has stopped being read" in accounting
    assert "until both sources have been read again" in accounting
    assert "minutes apart" not in accounting, "the gap is called minutes without its bound"
    assert "**up to 24 hours** apart" in operations
    assert "taken minutes apart" not in operations
    assert "until both sources have been read again" in operations


def test_the_accounting_document_says_what_no_snapshot_means() -> None:
    text = section(read(ACCOUNTING_DOC), CHECK_HEADING)

    assert "`null`" in text
    assert "`assets` is\n  empty" in text or "`assets` is empty" in text


# --------------------------------------------------------------------------------------
# docs/providers.md
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "needle",
    [
        pytest.param("GET /api/v2/spot/account/assets", id="bitget endpoint"),
        pytest.param("GET /openApi/spot/v1/account/balance", id="bingx endpoint"),
        pytest.param("legacy-docs/classic/spot/account/Get-Account-Assets", id="bitget source"),
        pytest.param("Query Assets", id="bingx source"),
        pytest.param("assetType=hold_only", id="bitget query"),
        pytest.param("timestamp=<ms>&signature=<hex>", id="bingx query"),
        pytest.param("`exchange_balances`", id="the label"),
        pytest.param("available + frozen + locked", id="bitget total"),
        pytest.param("free + locked", id="bingx total"),
        pytest.param("`fetch_balances`", id="the protocol member"),
        pytest.param("`assemble_balances", id="the assembler"),
    ],
)
def test_the_provider_document_records_both_balance_endpoints(needle: str) -> None:
    assert needle in read(PROVIDERS_DOC), f"docs/providers.md does not mention {needle}"


@pytest.mark.parametrize(
    "needle",
    [
        pytest.param("limitAvailable", id="bitget: limitAvailable overlap"),
        pytest.param("Unified Trading Account", id="bitget: UTA"),
        pytest.param("/openApi/fund/v1/account/balance", id="bingx: the fund account"),
        pytest.param("Nobody has called this endpoint", id="neither was called with a real key"),
        pytest.param("not stated on the page", id="bitget: the key permission"),
    ],
)
def test_the_provider_document_says_what_is_not_established(needle: str) -> None:
    """What was designed around rather than known is labelled as that, not written as fact."""
    text = read(PROVIDERS_DOC)

    assert needle in text, f"docs/providers.md does not mention {needle}"
    assert "Not documented, and designed around" in text
    assert "Not established, and designed around" in text


def test_the_provider_document_says_only_the_spot_account_is_read() -> None:
    assert "Only the spot account is read" in read(PROVIDERS_DOC)


def test_the_provider_document_counts_nine_labels() -> None:
    """The label set grew by one, and the sentence that counts it was updated with it."""
    text = read(PROVIDERS_DOC)

    assert "exactly nine" in text
    assert "exactly eight" not in text


# --------------------------------------------------------------------------------------
# docs/operations.md
# --------------------------------------------------------------------------------------


def test_the_operations_document_says_only_the_spot_account_is_read() -> None:
    text = read(OPERATIONS_DOC)

    assert "Only the spot account of each venue is read" in text
    for account in ("Earn", "futures", "margin", "funding"):
        assert account in text, f"{account} accounts are not said to be left out"


def test_the_operations_document_says_where_a_failed_read_shows_and_where_it_does_not() -> None:
    text = read(OPERATIONS_DOC)

    assert "GET /api/accounting/reconciliation" in text
    assert "`balances_read_at`" in text
    assert "`balances_error`" in text
    assert "Not in the account's status, and not in the run log" in text
    assert '`not_compared_reason: "read_failed"`' in text
    assert "are not used" in text, "a kept reading is kept, and is not compared (R9)"
    assert "None of these fields carries an asset or an amount" in text
    assert "the only place a venue's balances are served" in text


#: Each event the sync logs about a balance read, and exactly the fields it carries.
#: `tests/services/test_exchange_sync_balances.py` pins the same four from the sync's side.
LOG_EVENTS: Final = {
    "exchange_balances_read": ["exchange_key", "assets"],
    "exchange_balances_read_failed": ["exchange_key", "error_kind", "error_type"],
    "exchange_balances_read_skipped": ["exchange_key", "reason"],
    "exchange_balances_failure_not_recorded": ["exchange_key", "error_type"],
}


@pytest.mark.parametrize("name", sorted(LOG_EVENTS))
def test_the_operations_document_lists_each_log_event_with_its_fields(name: str) -> None:
    rows = [
        cells(line)
        for line in read(OPERATIONS_DOC).splitlines()
        if line.startswith(f"| `{name}` |")
    ]

    assert len(rows) == 1, f"{name} has {len(rows)} rows in the log table"
    assert re.findall(r"`([a-z_]+)`", rows[0][1]) == LOG_EVENTS[name]
