"""Spec 029 (#22), criterion 11 and ruling R7: the documents say what the backups are and do.

Checked as substance rather than as prose, the way `tests/test_reconciliation_documentation.py`
checks its documents: the wording is free to change, and what cannot change without failing a
test here is

* **why, where, and what a copy holds**: `docs/deployment.md`, *Scheduled backups*, names the
  90-day window and what is entered by hand, says a copy holds the owner's complete financial
  data, says it is not the deployment's backup, and says it does not protect against losing
  the device;
* **the settings, against the code**: every row of the settings table names a real variable
  and its real default, and the four that `secrets.env` may change are named, with the fifth
  said to be fixed by the compose file (R7);
* **every state, every error kind and every field** the code serves or logs has its row, and
  `unreadable` comes first, as it does in the code (R4);
* **the restore procedure, in order**: choose, stop, restore through `run --rm --no-deps`,
  start, check -- with the empty-volume variant's `up -d` (R7) and the damaged-database
  paragraph (R3);
* **every refusal the table lists is a message the code can print**: each fragment of the
  table's "Message begins" column is found in the source that raises it, so a reworded
  message fails here rather than leaving the operator a row that matches nothing;
* **the lines the documents show are the lines the command prints**: the safety-copy line,
  the no-safety-copy line and the moved-aside line are the CLI's own sentences.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from portfolio.config import MINIMUM_BACKUP_INTERVAL_MINUTES, Settings
from portfolio.db.backup import RestoreRefusal
from portfolio.domain.backups import BackupErrorKind
from portfolio.services.backup import BackupState

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DEPLOYMENT_DOC: Final = REPO_ROOT / "docs" / "deployment.md"
OPERATIONS_DOC: Final = REPO_ROOT / "docs" / "operations.md"
SOURCE: Final = REPO_ROOT / "backend" / "src" / "portfolio"

SCHEDULED_HEADING: Final = "## Scheduled backups"
SECTION_17: Final = "## 17. Backups: where they are, how they stand, and restoring one"
RESTORING_HEADING: Final = "### Restoring one"
VARIABLES: Final = (
    "PORTFOLIO_BACKUP_ENABLED",
    "PORTFOLIO_BACKUP_INTERVAL_MINUTES",
    "PORTFOLIO_BACKUP_DIR",
    "PORTFOLIO_BACKUP_KEEP_DAILY",
    "PORTFOLIO_BACKUP_KEEP_WEEKLY",
)


def read(path: Path) -> str:
    assert path.is_file(), f"{path} does not exist"
    return path.read_text(encoding="utf-8")


def raw_section(text: str, heading: str) -> str:
    """One section, from its heading to the next heading of its level or above, as written."""
    assert text.count(heading + "\n") == 1, f"{heading!r} appears {text.count(heading)} times"
    start = text.index(heading + "\n")
    depth = len(heading.split(" ", 1)[0])
    following = re.search(rf"^#{{1,{depth}}} ", text[start + len(heading) :], re.MULTILINE)
    end = start + len(heading) + following.start() if following else len(text)
    return text[start:end]


def section(text: str, heading: str) -> str:
    """`raw_section` on one line, so a phrase the document wraps is still found."""
    return " ".join(raw_section(text, heading).split())


def table_rows(text: str, first_column: str) -> list[list[str]]:
    """The rows of the Markdown table whose header's first cell is `first_column`."""
    lines = text.splitlines()
    header = next(
        number
        for number, line in enumerate(lines)
        if line.startswith("|") and line.split("|")[1].strip() == first_column
    )
    rows = []
    for line in lines[header + 2 :]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def deployment() -> str:
    return section(read(DEPLOYMENT_DOC), SCHEDULED_HEADING)


def operations() -> str:
    return section(read(OPERATIONS_DOC), SECTION_17)


def operations_raw() -> str:
    return raw_section(read(OPERATIONS_DOC), SECTION_17)


# --------------------------------------------------------------------------------------
# docs/deployment.md: why, where, what a copy holds, and what it does not protect against
# --------------------------------------------------------------------------------------


def test_deployment_says_why_the_copies_matter() -> None:
    text = deployment()

    assert "90 days" in text
    assert "wallets" in text
    assert "manual adjustments" in text


def test_deployment_says_a_copy_holds_the_owners_financial_data() -> None:
    text = deployment()

    assert "A copy holds the owner's complete financial data" in text
    assert "password hash" in text


def test_deployment_says_where_the_copies_are() -> None:
    text = deployment()

    assert "`/app/backups`" in text
    assert "`PORTFOLIO_BACKUP_DIR`" in text
    assert "removing the data volume does not remove the copies" in text


def test_deployment_says_they_are_not_the_deployments_backup() -> None:
    text = section(read(DEPLOYMENT_DOC), "### Not the deployment's backup, and why both exist")

    assert "`prod/backup/`" in text
    assert "Neither replaces the other" in text


def test_deployment_says_they_do_not_protect_against_losing_the_device() -> None:
    text = section(read(DEPLOYMENT_DOC), "### What they do not protect against")

    assert "do not protect against losing the storage device" in text


def test_deployment_states_the_r5_rotation() -> None:
    assert (
        "Rotation keeps every copy on the 7 most recent days that have one, and the newest copy "
        "of each of the 4 most recent ISO weeks that have one." in deployment()
    )


def test_deployment_forbids_a_merge_during_a_restore() -> None:
    assert "Do not merge to `main` while a restore is in progress." in deployment()


# --------------------------------------------------------------------------------------
# docs/operations.md: the settings, against the code
# --------------------------------------------------------------------------------------


def test_the_settings_table_names_each_variable_with_its_real_default() -> None:
    defaults = Settings()
    expected = {
        "PORTFOLIO_BACKUP_ENABLED": "`true`" if defaults.backup_enabled else "`false`",
        "PORTFOLIO_BACKUP_INTERVAL_MINUTES": f"`{defaults.backup_interval_minutes}`",
        "PORTFOLIO_BACKUP_DIR": f"`./{defaults.backup_dir.as_posix()}`",
        "PORTFOLIO_BACKUP_KEEP_DAILY": f"`{defaults.backup_keep_daily}`",
        "PORTFOLIO_BACKUP_KEEP_WEEKLY": f"`{defaults.backup_keep_weekly}`",
    }

    rows = table_rows(operations_raw(), "Variable")

    assert {row[0].strip("`"): row[1] for row in rows} == expected


def settings_row(variable: str) -> str:
    """The description cell of one variable's row in the settings table."""
    rows = {row[0].strip("`"): row[2] for row in table_rows(operations_raw(), "Variable")}
    return " ".join(rows[variable].split())


def test_the_interval_row_states_the_r14_floor_and_the_copies_it_keeps() -> None:
    """R14: the floor is the validator's, and the count is computed here from the defaults
    rather than restated: `keep_daily x 1440 / interval`, at the default and hourly."""
    defaults = Settings()
    row = settings_row("PORTFOLIO_BACKUP_INTERVAL_MINUTES")
    per_day = 1440

    assert f"at least {MINIMUM_BACKUP_INTERVAL_MINUTES}" in row
    assert "`KEEP_DAILY \N{MULTIPLICATION SIGN} 1440 / interval`" in row
    at_default = defaults.backup_keep_daily * per_day // defaults.backup_interval_minutes
    hourly = defaults.backup_keep_daily * per_day // MINIMUM_BACKUP_INTERVAL_MINUTES
    assert (at_default, hourly) == (7, 168)
    assert f"{at_default} at the default, {hourly} hourly" in row


def test_the_directory_row_names_the_directory_the_image_sets() -> None:
    """R10 and R15: read from the `Dockerfile`, so the two cannot drift apart."""
    dockerfile = read(REPO_ROOT / "Dockerfile")
    (image_value,) = re.findall(r"^\s*PORTFOLIO_BACKUP_DIR=(\S+)", dockerfile, re.MULTILINE)

    assert image_value == "/app/backups"
    assert f"The image sets `{image_value}`" in settings_row("PORTFOLIO_BACKUP_DIR")


def test_the_four_secrets_variables_are_named_and_the_directory_is_not_one() -> None:
    """R7: `environment:` overrides `env_file:`, so the directory cannot be set there."""
    text = operations()

    assert (
        "Four of them can be changed in `secrets.env`: `PORTFOLIO_BACKUP_ENABLED`, "
        "`PORTFOLIO_BACKUP_INTERVAL_MINUTES`, `PORTFOLIO_BACKUP_KEEP_DAILY` and "
        "`PORTFOLIO_BACKUP_KEEP_WEEKLY`."
    ) in text.replace("**", "")
    assert "`PORTFOLIO_BACKUP_DIR` cannot be changed there" in text
    assert "overrides `env_file:`" in text


def test_operations_says_every_copy_holds_financial_data_and_not_the_device() -> None:
    text = operations()

    assert "Every copy holds the owner's complete financial data" in text
    assert "do not protect against losing the storage device" in text


def test_operations_states_the_r5_rotation_and_a_failed_rotation() -> None:
    text = operations()

    assert "every copy on the 7 most recent UTC days that have a copy" in text
    assert "the copy just taken has still been kept" in text
    assert "`kept`" in text


def test_operations_says_the_copy_writes_only_after_an_unclean_stop() -> None:
    """R7: the copy is read-only towards the live database except for R1's release."""
    assert (
        "It reads the live database without writing to it, with one exception: after an "
        "unclean stop" in operations()
    )


# --------------------------------------------------------------------------------------
# Every state, every kind, every field
# --------------------------------------------------------------------------------------


def test_every_state_has_a_row_and_unreadable_comes_first() -> None:
    rows = table_rows(operations_raw(), "`state`")
    states = [row[0].strip("`") for row in rows]

    assert sorted(states) == sorted(state.value for state in BackupState)
    assert states[0] == BackupState.UNREADABLE.value


def test_ok_is_also_served_before_the_first_attempt() -> None:
    """R7, and what `test_backup_service.py` pins on the code's side."""
    (ok,) = [row for row in table_rows(operations_raw(), "`state`") if row[0] == "`ok`"]

    assert "has not attempted one since the process started" in ok[1]


def test_every_error_kind_has_a_row() -> None:
    rows = table_rows(operations_raw(), "`error_kind`")

    assert sorted(row[0].strip("`") for row in rows) == sorted(
        kind.value for kind in BackupErrorKind
    )


def test_the_log_events_list_the_fields_the_service_logs() -> None:
    rows = {row[0]: row[1] for row in table_rows(operations_raw(), "Event")}

    assert rows["`backup_completed`"] == "`name`, `bytes`, `duration_ms`, `deleted`"
    assert rows["`backup_failed`"] == "`error_kind`, `error_type`, and `kept` when rotation failed"


def test_the_json_example_has_exactly_the_five_fields_served() -> None:
    example = re.search(r"```json\n\s*(\{.*?\})\n\s*```", operations_raw(), re.DOTALL)
    assert example is not None

    fields = re.findall(r'"(\w+)":', example.group(1))

    assert fields == ["state", "latest_at", "count", "last_attempt_at", "last_error_kind"]


def test_the_health_page_shows_unknown_while_the_directory_cannot_be_read() -> None:
    assert '"unknown" for both while the directory cannot be read' in operations()


# --------------------------------------------------------------------------------------
# The restore procedure
# --------------------------------------------------------------------------------------


def test_the_restore_steps_come_in_order_through_compose_sh() -> None:
    text = section(read(OPERATIONS_DOC), RESTORING_HEADING)
    steps = [
        "compose.sh exec app python -m portfolio list-backups",
        "compose.sh stop app",
        "compose.sh run --rm --no-deps app python -m portfolio restore-backup portfolio-",
        "compose.sh start app",
        "compose.sh ps",
    ]

    positions = [text.find(step) for step in steps]

    assert -1 not in positions, dict(zip(steps, positions, strict=True))
    assert positions == sorted(positions)


def test_an_empty_data_volume_is_brought_up_rather_than_started() -> None:
    """R7: there is no container to start on a new volume."""
    text = section(read(OPERATIONS_DOC), RESTORING_HEADING)

    assert "Onto a new, empty data volume" in text
    assert "compose.sh up -d app` rather than `start` it" in text


def test_the_damaged_database_paragraph_says_where_it_goes_and_what_it_holds() -> None:
    """R3, and the owner's data in a file nothing rotates."""
    text = section(read(OPERATIONS_DOC), RESTORING_HEADING)

    assert "Over a damaged database." in text
    assert "/app/data/portfolio.db.damaged-<UTC stamp>" in text
    assert "The moved file holds the owner's financial data" in text
    assert "compose.sh exec app rm /app/data/portfolio.db.damaged-" in text


def joined_source(*paths: Path) -> str:
    """The source on one line, with adjacent string literals joined as Python joins them."""
    flat = " ".join(" ".join(read(path).split()) for path in paths)
    return re.sub(r'"\s+f?"', "", flat)


def test_the_lines_shown_are_the_lines_the_command_prints() -> None:
    """The CLI's own sentences, read from its source rather than restated here."""
    cli = joined_source(SOURCE / "cli.py")
    text = section(read(OPERATIONS_DOC), RESTORING_HEADING)

    for shown, printed in [
        (
            "The database as it was before is in the safety copy portfolio-",
            "The database as it was before is in the safety copy {result.safety_copy}.",
        ),
        (
            "There was no database to copy first, so no safety copy was taken.",
            "There was no database to copy first, so no safety copy was taken.",
        ),
        (
            "The live database opened but did not pass its own check, so no safety copy was "
            "taken: it was moved aside to /app/data/portfolio.db.damaged-",
            "The live database opened but did not pass its own check, so no safety copy was "
            "taken: it was moved aside to {result.damaged}.",
        ),
        (
            "Keep it until the restore is checked, then delete it.",
            "{result.damaged}. Keep it until the restore is checked, then delete it.",
        ),
        ("Rows per table after the restore:", "Rows per table after the restore:"),
    ]:
        assert shown in text, shown
        assert printed in cli, printed


def _message_rows() -> list[list[str]]:
    return table_rows(operations_raw(), "Message begins")


def source_fragments(cell: str) -> list[str]:
    """The literal text of a "Message begins" cell, split at each `...` it elides.

    A word that starts with `'` or `-` beside an elision continues the elided value (the
    name's quotes in `'...'`, the file's suffix in `... -wal`), so it is not source text.
    """
    fragments = []
    for part in cell.strip("`").split("..."):
        words = part.split()
        while words and words[0][0] in "'-":
            words = words[1:]
        while words and words[-1][0] in "'-":
            words = words[:-1]
        if words:
            fragments.append(" ".join(words))
    return fragments


def test_the_fragments_drop_only_what_continues_an_elision() -> None:
    assert source_fragments("`Refusing to restore: '...' is not the name of a backup`") == [
        "Refusing to restore:",
        "is not the name of a backup",
    ]
    assert source_fragments("`Refusing to restore: ... -wal exists, so it is open`") == [
        "Refusing to restore:",
        "exists, so it is open",
    ]
    assert source_fragments("`moved aside to ..., but ... cannot be removed`") == [
        "moved aside to",
        ", but",
        "cannot be removed",
    ]


@pytest.mark.parametrize(
    "row",
    [row for row in _message_rows() if row[0].startswith("`")],
    ids=lambda row: row[0][:60],
)
def test_every_refusal_in_the_table_is_a_message_the_code_prints(row: list[str]) -> None:
    """Each fragment between the `...` of the "Message begins" cell is in the source."""
    source = joined_source(SOURCE / "db" / "backup.py", SOURCE / "services" / "backup.py")
    fragments = source_fragments(row[0])

    assert fragments, row
    for fragment in fragments:
        assert fragment in source, fragment


def test_the_refusal_table_has_a_row_for_each_r3_outcome() -> None:
    cells = [row[0] for row in _message_rows()]

    assert any("could be taken, so nothing was changed" in cell for cell in cells)
    assert any("the live database passes its own check" in cell for cell in cells)
    assert any("appeared during the restore" in cell for cell in cells)
    assert any("moving it aside" in cell for cell in cells)
    assert any("cannot be removed" in cell for cell in cells)


def test_the_refusal_table_has_a_row_for_each_r12_to_r16_outcome() -> None:
    """Each row's fragments are checked against the source by the test above; this one checks
    that the rows exist at all, so a refusal added to the code has its row."""
    rows = {row[0]: " ".join(row[1].split()) for row in _message_rows()}

    def why(start: str) -> str:
        (cause,) = [cause for cell, cause in rows.items() if cell.startswith(f"`{start}")]
        return cause

    assert "it was not moved aside. Nothing was changed." in why(
        "Refusing to restore: the live database ... cannot be read"
    )
    assert "(`not_self_contained`)" in why("Refusing to restore: ... is not a self-contained")
    assert "(`leftover_journal`)" in why("Refusing to restore: there is no database at")
    assert "Nothing was changed." in why(
        "Refusing to restore: the live database ... did not pass its own check, and"
    )
    assert "could not be moved with it" in why(
        "The damaged live database was moved aside to ..., but moving"
    )
    assert "is at the name the message gives" in why(
        "The damaged live database was moved aside to ..., but the move could not be made"
    )
    assert "the file is where it was" in why("The live database ... did not pass its own check")


def test_every_refusal_reason_is_named_where_its_row_explains_it() -> None:
    """The two reasons R13 and R16 added are named in their rows, as the operator would see
    them in an error report; the older three are in the spec's own table."""
    text = " ".join(" ".join(row[1:]) for row in _message_rows())

    for reason in (RestoreRefusal.NOT_SELF_CONTAINED, RestoreRefusal.LEFTOVER_JOURNAL):
        assert f"(`{reason.value}`)" in text, reason


# --------------------------------------------------------------------------------------
# Off the host, and back
# --------------------------------------------------------------------------------------


def test_copying_one_off_the_host_warns_about_the_financial_data() -> None:
    text = section(read(OPERATIONS_DOC), "### Copying one off the host")

    assert "The copy holds the owner's complete financial data." in text
    assert "compose.sh cp app:/app/backups/portfolio-" in text
    assert "chmod 600" in text


def test_bringing_one_back_gives_it_to_the_containers_user() -> None:
    """R7: `docker cp` creates files as root, so a root one-off container hands it over."""
    text = section(read(OPERATIONS_DOC), "### Bringing a copy back onto the host")

    assert "run --rm --no-deps -u root" in text
    assert "chown app:app /app/backups/portfolio-" in text
    assert "This command has not been run on the Pi yet" in text


# --------------------------------------------------------------------------------------
# Troubleshooting
# --------------------------------------------------------------------------------------


def troubleshooting() -> dict[str, str]:
    """The Troubleshooting table, symptom to likely cause."""
    rows = table_rows(raw_section(read(OPERATIONS_DOC), "## Troubleshooting"), "Symptom")
    return {row[0]: row[1] for row in rows}


def test_every_error_kind_and_warning_state_has_a_troubleshooting_row() -> None:
    symptoms = " ".join(troubleshooting())

    for kind in BackupErrorKind:
        assert f"`last_error_kind` is `{kind.value}`" in symptoms, kind
    assert "(`unreadable`)" in symptoms
    assert "(`stale`)" in symptoms


def test_the_unreadable_row_points_at_the_volume_and_the_variable() -> None:
    (cause,) = [
        cause for symptom, cause in troubleshooting().items() if "(`unreadable`)" in symptom
    ]

    assert "`/app/backups`" in cause
    assert "`backups` volume" in cause
    assert "`PORTFOLIO_BACKUP_DIR`" in cause


def test_the_storage_error_row_says_what_kept_means() -> None:
    cause = troubleshooting()["`last_error_kind` is `storage_error`"]

    assert "If the `backup_failed` line has `kept`, the copy was kept" in cause


def test_the_moved_aside_row_says_where_and_to_keep_it() -> None:
    symptom = (
        "`restore-backup` says the live database did not pass its own check and was moved aside"
    )
    cause = troubleshooting()[symptom]

    assert "`/app/data/portfolio.db.damaged-<stamp>`" in cause
    assert "keep it until the restore is checked, then delete it" in cause


def test_the_no_safety_copy_row_says_nothing_was_changed() -> None:
    symptom = "`restore-backup` refuses because no safety copy of the live database could be taken"
    cause = troubleshooting()[symptom]

    assert "the safety copy failed while the live database is sound" in cause
    assert "Nothing was changed." in cause


def test_the_r12_to_r16_refusals_have_troubleshooting_rows() -> None:
    rows = troubleshooting()

    assert (
        "The interval is below 60"
        in rows[
            "Container refuses to start naming `PORTFOLIO_BACKUP_INTERVAL_MINUTES`, `_KEEP_DAILY` "
            "or `_KEEP_WEEKLY`"
        ]
    )
    assert (
        "not damage, so it was not moved aside. Nothing was changed."
        in rows["`restore-backup` refuses because the live database cannot be read"]
    )
    assert (
        "Make it one file with `sqlite3` off the host"
        in rows["`restore-backup` says the backup is not a self-contained copy"]
    )
    assert (
        "Nothing was changed"
        in rows["`restore-backup` says there is no database, but a `-journal` lies beside its path"]
    )


def test_the_damaged_paragraph_says_a_journal_goes_with_it_when_sqlite_left_one() -> None:
    """R15's journal move, and R1's read-write open that often uses the journal first."""
    text = section(read(OPERATIONS_DOC), RESTORING_HEADING)

    assert "A `portfolio.db-journal` beside it goes with it" in text
    assert "So a `-journal` moves only when SQLite has not already used it." in text
    assert "**Only a file that opens is judged damaged.**" in text


def test_the_variables_are_the_settings_fields() -> None:
    """The table's five names are the five fields `Settings` reads, by its env prefix."""
    fields = {name for name in Settings.model_fields if name.startswith("backup_")}

    assert {f"PORTFOLIO_{name.upper()}" for name in fields} == set(VARIABLES)
