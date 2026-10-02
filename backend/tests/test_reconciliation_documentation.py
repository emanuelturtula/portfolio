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
* **Both state the wallet rule of spec 028 (#116)**: a wallet whose chain failed in the last
  balance sync that finished is left out at once and the check names the chain; what still
  ages out at twenty-four hours with nothing naming the source, as residuals; the two windows
  the rule leaves; and the two fields it added. Neither still says #116 will do it. After the
  review (rulings R2 and R5): the rule's exception, a later sync that has already read the
  wallet; where the reason for a failure always is, the run log; and what to check when a
  wallet stays left out, the balance timer.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

import pytest

from portfolio.config import Settings
from portfolio.domain.accounting import reconcile
from portfolio.services import reconciliation as reconciliation_module
from portfolio.services.reconciliation import (
    MAX_READING_AGE_HOURS,
    NotComparedReason,
    WalletNotComparedReason,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
ACCOUNTING_DOC: Final = REPO_ROOT / "docs" / "accounting.md"
PROVIDERS_DOC: Final = REPO_ROOT / "docs" / "providers.md"
OPERATIONS_DOC: Final = REPO_ROOT / "docs" / "operations.md"

CHECK_HEADING: Final = "## Checking the history against the balances held"
EXAMPLES_HEADER: Final = "| History | Wallets | Exchanges | Held | Difference | Status |"
STATUSES: Final = ("match", "history_short", "history_over")
#: R9's four reasons, written out in the order the service tests them.
REASONS: Final = ("read_failed", "never_read", "sync_failed", "out_of_date")
#: The three counts of wallets left out, in the order spec 028 tests them.
WALLET_COUNTS: Final = ("chain_failed", "unread", "stale")
OPERATIONS_HEADING: Final = (
    "## 16. The holdings check: which balances are compared, and what a failed read means"
)


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


def flat(text: str) -> str:
    """The text with every run of whitespace one space, so a sentence is found wherever its
    lines happen to break."""
    return " ".join(text.split())


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
    assert list(sample["wallets"]) == [
        "compared",
        "stale",
        "unread",
        "chain_failed",
        "failed_chains",
        "oldest_observed_at",
    ]


def test_the_accounting_documents_sample_shows_a_failed_chain_the_endpoint_could_serve() -> None:
    """The sample is the one place the owner sees the two fields filled in, so it shows a chain
    that failed, and it is a document the service can produce: the entries are sorted, none
    is zero, and they add up to `chain_failed`."""
    wallets = sample_response()["wallets"]
    entries = wallets["failed_chains"]

    assert entries, "the sample shows no failed chain"
    for entry in entries:
        assert list(entry) == ["chain_key", "wallets"]
        assert isinstance(entry["chain_key"], str)
        assert type(entry["wallets"]) is int
        assert entry["wallets"] > 0
    keys = [entry["chain_key"] for entry in entries]
    assert keys == sorted(set(keys))
    assert set(keys) <= {"bitcoin", "kaspa"}
    assert type(wallets["chain_failed"]) is int
    assert wallets["chain_failed"] == sum(entry["wallets"] for entry in entries)
    assert wallets["compared"] > 0
    assert wallets["oldest_observed_at"] is not None, "a compared wallet has a reading"


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


# --------------------------------------------------------------------------------------
# Spec 028 (#116): a wallet whose chain failed, in both documents
# --------------------------------------------------------------------------------------


def wallet_count_rows(text: str) -> dict[str, list[str]]:
    """The rows of a table whose first cell is one of the three counts of wallets left out."""
    return {
        cells(line)[0].strip("`"): cells(line)
        for line in text.splitlines()
        if line.startswith("| `") and cells(line)[0].strip("`") in WALLET_COUNTS
    }


def test_the_accounting_document_states_the_wallet_rule_and_that_the_chain_is_named() -> None:
    """Criterion 12: the rule, in the document the owner reads."""
    text = flat(section(read(ACCOUNTING_DOC), CHECK_HEADING))

    assert (
        "**A wallet's reading is current** when its chain did not fail in the last balance "
        "sync that finished, and the reading is at most 24 hours old."
    ) in text
    assert (
        "When the chain did fail in that sync, the reading is current only if a later sync has "
        "already stored it, and it is at most 24 hours old."
    ) in text, "the short form of the rule carries its exception (R5)"
    assert "A wallet whose chain failed is left out at once, and the check names the chain" in text
    assert "docs/specs/028-wallet-chain-failed.md" in text
    assert "whatever its last reading says, unless a later sync has already read it." in text
    assert "`failed_chains` lists each such chain with the number of wallets it left out" in text
    assert "one notice per chain" in text
    assert "a wallet whose chain could not be read" in text, "among what the check does not see"


def test_the_accounting_document_says_where_the_reason_for_a_failure_always_is() -> None:
    """Ruling R2. The check gives no reason, and the document used to send the owner to the
    Value section's wallet rows for it. Those rows do not show it after a later sync was
    interrupted, nor for a wallet never read. The run log always has it."""
    text = flat(section(read(ACCOUNTING_DOC), CHECK_HEADING))

    assert "The check does not say why the chain failed. The run log always does:" in text
    assert (
        "`GET /api/balances/runs` carries the failed chain's `error_kind` and `detail` in the "
        "last sync that finished"
    ) in text
    assert "The Value section's wallet rows usually show the reason too" in text
    assert "but not after a later sync was interrupted" in text
    assert "and not for a wallet that was never read" in text
    assert "the Value section's wallet rows do." not in text, "the claim R2 found false"
    assert "wallet rows already say why" not in text


def test_the_accounting_document_says_which_sync_decides_and_which_reading_is_kept() -> None:
    """The three things that make the rule one a reader can apply: what "finished" is, that
    a later sync's reading is kept, and that a chain with no result did not fail."""
    text = flat(section(read(ACCOUNTING_DOC), CHECK_HEADING))

    assert (
        "**The last balance sync that finished** is the newest one that ended as `success`, "
        "`partial` or `failed`."
    ) in text
    assert "A sync still in progress, and one that was interrupted, record no result per" in text
    assert "so the one before them still stands" in text
    assert "**A reading stored by a later sync is kept.**" in text
    assert "because no wallet was active on it then, did not fail" in text
    assert "Nor has any chain before the first sync finishes" in text


def test_the_accounting_document_counts_the_wallets_left_out_in_the_order_they_are_tested() -> None:
    """`chain_failed`, `unread`, `stale`: the first that applies, as the service decides it."""
    text = section(read(ACCOUNTING_DOC), CHECK_HEADING)
    rows = wallet_count_rows(text)

    assert list(rows) == [reason.value for reason in WalletNotComparedReason]
    assert list(rows) == list(WALLET_COUNTS)
    assert all(len(row) == 2 and row[1] for row in rows.values()), rows
    assert "counted under the first of these that applies" in flat(text)
    assert "The four counts add up to the active wallets." in text
    assert "and not as `unread`" in rows["chain_failed"][1]
    assert "however recent it is" in rows["chain_failed"][1]
    assert "the chain is not known to have failed" in rows["stale"][1]


def test_the_accounting_document_lists_the_residuals_and_the_remaining_window() -> None:
    """What still ages out at twenty-four hours with nothing naming the source, item by item,
    and the one window the chain rule leaves: a failure between two syncs."""
    text = section(read(ACCOUNTING_DOC), CHECK_HEADING)
    after = text.split("These are the residuals:", 1)
    assert len(after) == 2, "the residuals are not introduced as residuals"
    items = [
        flat(item)
        for item in re.findall(r"^  - (.*?)(?=^  - |^- |\Z)", after[1], re.DOTALL | re.MULTILINE)
    ][:4]

    assert len(items) == 4, items
    assert "a wallet, when the balance timer is switched off" in items[0]
    assert "or when no balance sync finishes" in items[0]
    assert items[1] == "a venue whose credentials were removed after a read;"
    assert items[2] == "a venue, when the exchange timer is switched off;"
    assert "a venue whose balance read failed when the failure could not be recorded" in items[3]
    assert "nothing names the source until then" in flat(text)

    window = flat(text)
    assert (
        "**One balance interval, when a chain starts failing between two balance syncs.**"
    ) in window
    assert "The failure is not known until the next sync finishes" in window
    assert "fifteen minutes by default" in window


def test_the_accounting_document_states_the_window_of_a_chain_that_was_not_attempted() -> None:
    """Ruling R5's second window, which `test_reconciliation_chain_failed_timelines.py` pins as
    built: a chain's only wallets archived while the last sync ran, and restored afterwards."""
    text = flat(section(read(ACCOUNTING_DOC), CHECK_HEADING))

    assert (
        "**One balance interval, when a chain's only wallets were archived while the last sync "
        "ran and were restored afterwards.**"
    ) in text
    assert "A sync does not attempt a chain with no active wallet" in text
    assert "a chain with no result did not fail" in text
    assert "The restored wallets' previous readings are then compared" in text
    assert "even if the sync before recorded the chain as failed" in text
    assert "That lasts until the next sync finishes" in text
    assert text.count("**One balance interval, when") == 2, "two windows, each with its bound"


def test_the_accounting_document_documents_the_two_new_fields() -> None:
    text = flat(section(read(ACCOUNTING_DOC), CHECK_HEADING))

    assert (
        "`wallets.compared`, `wallets.stale`, `wallets.unread` and `wallets.chain_failed` add up "
        "to the active wallets."
    ) in text
    assert "`wallets.chain_failed` is the number of wallets left out because" in text
    assert "`wallets.failed_chains` names those chains, sorted by `chain_key`" in text
    assert "Only a chain with at least one wallet left out is listed" in text
    assert "`wallets` is never zero and the entries add up to `chain_failed`" in text
    assert "With no such chain the list is empty." in text


def test_the_operations_document_states_the_wallet_rule() -> None:
    text = flat(section(read(OPERATIONS_DOC), OPERATIONS_HEADING))

    assert (
        "**A wallet** is compared when its chain did not fail in the last balance run that "
        "finished, and its latest reading is at most 24 hours old."
    ) in text
    assert (
        "When the chain did fail in that run, the wallet is compared only if a later run has "
        "already read it, and that reading is at most 24 hours old."
    ) in text, "the short form of the rule carries its exception (R5)"
    assert "**A wallet whose chain failed is left out at once**, without waiting for the limit" in (
        text
    )
    assert "however recent it is, unless a later run has already stored it." in text
    assert "docs/specs/028-wallet-chain-failed.md" in text
    assert "whose `status` is `success`, `partial` or `failed`" in text
    assert "A `running` or `interrupted` run records no chain" in text
    assert "the run before it still stands" in text
    assert "is kept and compared" in text
    assert "A chain with no entry in that run did not fail" in text
    assert "The dashboard shows one notice per chain." in text


def test_the_operations_document_explains_each_wallet_count_and_what_to_do_about_it() -> None:
    text = section(read(OPERATIONS_DOC), OPERATIONS_HEADING)
    rows = wallet_count_rows(text)

    assert list(rows) == list(WALLET_COUNTS)
    assert all(len(row) == 3 and row[1] and row[2] for row in rows.values()), rows
    assert "`wallets` has four counts that add up to the active wallets" in flat(text)
    assert "`failed_chains` names the chain" in rows["chain_failed"][2]
    assert "`GET /api/balances/runs`" in rows["chain_failed"][2]
    assert "`error_kind`" in rows["chain_failed"][2], "the reason is in the run log (R2)"
    assert (
        "If no run follows, check the balance timer (`PORTFOLIO_BALANCE_SYNC_ENABLED`): with it "
        "off no run comes, and the wallet stays left out."
    ) in rows["chain_failed"][2], "a chain_failed wallet never turns stale (R5)"
    assert "`PORTFOLIO_BALANCE_SYNC_ENABLED`" in rows["stale"][2]
    described = flat(text)
    assert "`failed_chains` lists the chains behind `chain_failed`, sorted by `chain_key`" in (
        described
    )
    assert "so `wallets` is never zero" in described
    assert "the list is empty when no wallet is left out this way" in described
    assert "An entry does not say why the chain failed: the run log does." in described


def test_the_operations_document_lists_the_residuals_and_the_remaining_window() -> None:
    text = section(read(OPERATIONS_DOC), OPERATIONS_HEADING)
    after = text.split("These are the residuals:", 1)
    assert len(after) == 2, "the residuals are not introduced as residuals"
    block = after[1].split("\n\n", 2)[1]
    items = [flat(item) for item in re.split(r"^- ", block, flags=re.MULTILINE) if item.strip()]

    assert len(items) == 4, items
    assert "a wallet, when the balance timer is switched off" in items[0]
    assert "`PORTFOLIO_BALANCE_SYNC_ENABLED=false`" in items[0]
    assert "or when no balance run finishes" in items[0]
    assert items[1] == "a venue whose credentials were removed after a read;"
    assert "a venue, when the exchange timer is switched off" in items[2]
    assert "`PORTFOLIO_EXCHANGE_SYNC_ENABLED=false`" in items[2]
    assert "`exchange_balances_failure_not_recorded`" in items[3]

    assert (
        "The chain rule leaves two windows of its own, each bounded by one balance interval "
        "(`PORTFOLIO_BALANCE_SYNC_INTERVAL_MINUTES`, fifteen minutes by default) while the "
        "balance timer runs:"
    ) in flat(text)
    introduced = text.split("The chain rule leaves two windows of its own", 1)[1]
    windows = [
        flat(item)
        for item in re.split(r"^- ", introduced.split("\n\n", 2)[1], flags=re.MULTILINE)
        if item.strip()
    ]

    assert len(windows) == 2, windows
    assert (
        "A chain that starts failing **between** two balance runs is not known to have failed "
        "until the next run finishes"
    ) in windows[0]
    assert "for up to one balance interval" in windows[0]
    assert "A run does not attempt a chain with no active wallet" in windows[1]
    assert "a chain with no entry did not fail" in windows[1]
    assert "archived while the last run ran and were restored afterwards" in windows[1]
    assert "even if the run before has the chain as `failed`" in windows[1]
    assert "That lasts until the next run finishes." in windows[1]


def test_the_settings_the_operations_document_names_for_the_rule_exist() -> None:
    """Three variables are named in the new text. Each is a setting, and the default the
    document states for the interval is the default."""
    text = section(read(OPERATIONS_DOC), OPERATIONS_HEADING)
    fields = Settings.model_fields

    for variable in (
        "PORTFOLIO_BALANCE_SYNC_ENABLED",
        "PORTFOLIO_EXCHANGE_SYNC_ENABLED",
        "PORTFOLIO_BALANCE_SYNC_INTERVAL_MINUTES",
    ):
        assert variable in text, variable
        assert variable.removeprefix("PORTFOLIO_").lower() in fields, variable
    assert fields["balance_sync_interval_minutes"].default == 15


def test_the_operations_document_has_a_troubleshooting_row_for_a_failed_chain() -> None:
    rows = [
        cells(line)
        for line in read(OPERATIONS_DOC).splitlines()
        if line.startswith("| The holdings check says the last balance sync")
    ]

    assert len(rows) == 1, rows
    symptom, action = rows[0]
    assert symptom == (
        "The holdings check says the last balance sync that finished could not read a chain, "
        "and leaves its wallets out"
    ), "the symptom is the notice's own words (R1)"
    assert "`wallets.failed_chains`" in action
    assert "`/api/balances/runs`" in action
    assert "`error_kind`" in action
    assert "if none follows, check `PORTFOLIO_BALANCE_SYNC_ENABLED`" in action


def test_no_document_quotes_the_notice_without_the_sync_that_finished() -> None:
    """Ruling R1. The notice is "The last balance sync that finished could not read ...". A
    document that quotes it as "the last balance sync could not read" describes a sentence
    the dashboard does not show, and names a different sync from the one the rule judges by."""
    for name, text in (
        ("docs/accounting.md", read(ACCOUNTING_DOC)),
        ("docs/operations.md", read(OPERATIONS_DOC)),
    ):
        assert not re.search(r"last\s+balance\s+(sync|run)\s+could\s+not\s+read", text), name


def test_nothing_still_says_the_issue_will_do_it() -> None:
    """Criterion 12's last clause. The two documents and the service's own docstrings said a
    wallet whose chain is failing would be left out by #116. It is, now."""
    module = read(Path(reconciliation_module.__file__))

    for name, text in (
        ("docs/accounting.md", read(ACCOUNTING_DOC)),
        ("docs/operations.md", read(OPERATIONS_DOC)),
        ("services/reconciliation.py", module),
    ):
        assert "#116 will" not in text, name
        assert not re.search(r"#116\s+will", text), name
        assert "#116" not in text, f"{name} still points at the issue"
    assert "028" in module, "the service says which spec its wallet rule is"


def test_the_service_states_the_read_order_the_exception_and_both_windows() -> None:
    """The service's own statement of the rule, kept in step with the documents: the run is
    read before the snapshots (R3), the reason is in the run log (R2), and the rule leaves two
    windows (R5). `test_reconciliation_chain_failed_timelines.py` pins the behaviour; this
    pins that the module still says so where the next reader of the code will look."""
    module = flat(read(Path(reconciliation_module.__file__)))

    assert "The run first, then the snapshots, in that order (spec 028, R3)." in module
    assert "**The chain rule leaves two windows of its own**" in module
    assert "`error_kind` and `detail` in the latest finished run" in module
    assert "wallet rows already say why" not in module, "the claim R2 found false"


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
