"""The scripts deploy.py runs inside the container, run for real (spec 034, criterion 8).

A fake Docker proves the choreography; it cannot prove a script. So each script deploy.py
hands to ``python -c`` -- the snapshot, the revision reader and the stream writer -- is run
here with the host's Python, exactly as the container runs it, against real SQLite files
under a temporary directory (R9): a WAL database the application holds open, a ``-wal`` left
by an unclean close, a missing file, and bytes through stdin.

``run()``'s new ``input_file`` (R8) is exercised for real too, because a stdin that went
through text mode would corrupt a database on Windows and nothing faked would notice.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from deploy_harness import BACKUP_NAME, deploy

REVISION = "0011_extended_keys"
MIGRATED = "0012_candidate_migration"
TIMEOUT_SECONDS = 120
# Bytes a text-mode pipe would mangle: CR LF pairs, NUL, Ctrl-Z (end of file to Windows
# text mode), and every other byte value.
AWKWARD = b"".join(
    hashlib.sha256(str(n).encode()).digest() for n in range(4096)
) + bytes(range(256)) + b"\r\n\n\r\x00\x1a\xff\r\n"

# Leaves the database at argv[1] the way a killed application does: committed transactions
# in the -wal, never checkpointed, the connection never closed.
UNCLEAN_WRITER = """\
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA wal_autocheckpoint=0")
connection.execute("UPDATE alembic_version SET version_num = ?", (sys.argv[2],))
connection.execute("INSERT INTO wallets (label) VALUES ('written by the candidate')")
connection.commit()
sys.stdout.write("committed")
sys.stdout.flush()
os._exit(0)
"""


def run_script(
    script: str, *args: object, stdin: bytes = b""
) -> subprocess.CompletedProcess[bytes]:
    """``python -c script args...``, as ``docker exec`` or ``compose run`` runs it."""
    return subprocess.run(
        [sys.executable, "-c", script, *(str(arg) for arg in args)],
        input=stdin,
        capture_output=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def stdout(process: subprocess.CompletedProcess[bytes]) -> str:
    return process.stdout.decode("utf-8").strip()


def make_database(
    path: Path, versions: tuple[str, ...] | None = (REVISION,), *, wal: bool = True
) -> None:
    """A database shaped like the application's: ``alembic_version`` and one table."""
    connection = sqlite3.connect(path)
    try:
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        if versions is not None:
            connection.execute(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
            connection.executemany(
                "INSERT INTO alembic_version VALUES (?)", [(v,) for v in versions]
            )
        connection.execute("CREATE TABLE wallets (id INTEGER PRIMARY KEY, label TEXT)")
        connection.executemany(
            "INSERT INTO wallets (label) VALUES (?)", [("one",), ("two",), ("three",)]
        )
        connection.commit()
    finally:
        connection.close()


def journal_header(path: Path) -> bytes:
    """Bytes 18-19 of the header: 1,1 for a rollback-journal (DELETE) file, 2,2 for WAL."""
    return path.read_bytes()[18:20]


def sidecar(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def file_alone(path: Path, directory: Path) -> Path:
    """A copy of the main database file without any -wal: what the file itself holds."""
    directory.mkdir(exist_ok=True)
    alone = directory / path.name
    shutil.copyfile(path, alone)
    return alone


def query(path: Path, sql: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


class ScriptTextTests(unittest.TestCase):
    def test_the_scripts_compile_and_check_without_assert(self) -> None:
        # A check written as assert vanishes under python -O (or PYTHONOPTIMIZE).
        for script in (deploy.SNAPSHOT_SCRIPT, deploy.REVISION_SCRIPT, deploy.STREAM_SCRIPT):
            compile(script, "<script>", "exec")
            self.assertNotIn("assert ", script)


class ScriptTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.live = self.directory / "data" / "portfolio.db"
        self.live.parent.mkdir()


class SnapshotScriptTests(ScriptTestCase):
    """``SNAPSHOT_SCRIPT <database> <copy>``: a DELETE-mode copy and its revision."""

    def setUp(self) -> None:
        super().setUp()
        self.copy = self.directory / "data" / "deploy-backup-attempt.sqlite3"

    def test_a_database_in_use_is_copied_whole_into_one_delete_mode_file(self) -> None:
        make_database(self.live)
        # The application holds it open, with committed transactions still in the -wal.
        writer = sqlite3.connect(self.live)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.executemany("INSERT INTO wallets (label) VALUES (?)", [("four",), ("five",)])
        writer.commit()
        self.assertGreater(sidecar(self.live, "-wal").stat().st_size, 0, "the premise")
        live_file = self.live.read_bytes()

        process = run_script(deploy.SNAPSHOT_SCRIPT, self.live, self.copy)

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(stdout(process), f"ok {REVISION}")
        self.assertEqual(journal_header(self.copy), b"\x01\x01", "the copy's header says WAL")
        self.assertFalse(sidecar(self.copy, "-wal").exists())
        self.assertFalse(sidecar(self.copy, "-shm").exists())
        # The file alone holds every committed row, the -wal's included.
        alone = file_alone(self.copy, self.directory / "alone")
        self.assertEqual(query(alone, "SELECT COUNT(*) FROM wallets"), [(5,)])
        self.assertEqual(query(alone, "PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(query(alone, "PRAGMA journal_mode"), [("delete",)])
        self.assertEqual(query(alone, "SELECT version_num FROM alembic_version"), [(REVISION,)])
        # Read-only: the live file is not checkpointed or otherwise written.
        self.assertEqual(self.live.read_bytes(), live_file)

    def test_a_delete_mode_database_gives_the_same(self) -> None:
        make_database(self.live, wal=False)

        process = run_script(deploy.SNAPSHOT_SCRIPT, self.live, self.copy)

        self.assertEqual(stdout(process), f"ok {REVISION}", process.stderr)
        self.assertEqual(journal_header(self.copy), b"\x01\x01")

    def test_a_database_without_one_revision_gives_a_bare_ok(self) -> None:
        for case, versions in {
            "no alembic_version": None,
            "an empty alembic_version": (),
            "two revisions": (REVISION, MIGRATED),
        }.items():
            with self.subTest(case=case):
                self.setUp()
                make_database(self.live, versions)

                process = run_script(deploy.SNAPSHOT_SCRIPT, self.live, self.copy)

                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(stdout(process), "ok")
                self.assertEqual(journal_header(self.copy), b"\x01\x01")

    def test_a_missing_database_is_absent_and_nothing_is_created(self) -> None:
        process = run_script(deploy.SNAPSHOT_SCRIPT, self.live, self.copy)

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(stdout(process), "absent")
        self.assertEqual(list(self.live.parent.iterdir()), [])

    def test_a_file_that_is_not_a_database_fails(self) -> None:
        self.live.write_bytes(b"this is not an SQLite database, and must not pass as one\n" * 64)

        process = run_script(deploy.SNAPSHOT_SCRIPT, self.live, self.copy)

        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(stdout(process).startswith("ok"), stdout(process))


class RevisionScriptTests(ScriptTestCase):
    """``REVISION_SCRIPT <database>``: the one revision, read-write, recovering a -wal."""

    def test_it_prints_the_one_revision(self) -> None:
        for wal in (True, False):
            with self.subTest(wal=wal):
                self.setUp()
                make_database(self.live, wal=wal)

                process = run_script(deploy.REVISION_SCRIPT, self.live)

                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(stdout(process), REVISION)

    def test_it_recovers_a_wal_left_by_an_unclean_close_and_reports_its_revision(self) -> None:
        make_database(self.live)
        killed = run_script(UNCLEAN_WRITER, self.live, MIGRATED)
        self.assertEqual(killed.stdout, b"committed", killed.stderr)
        wal = sidecar(self.live, "-wal")
        # The premise: the migration is only in the -wal; the file alone is still behind.
        self.assertGreater(wal.stat().st_size, 0)
        before = file_alone(self.live, self.directory / "before")
        self.assertEqual(query(before, "SELECT version_num FROM alembic_version"), [(REVISION,)])

        process = run_script(deploy.REVISION_SCRIPT, self.live)

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(stdout(process), MIGRATED, "it must see what the -wal holds")
        # Recovered into the file and removed: restore-backup refuses while a -wal exists.
        self.assertFalse(wal.exists(), "a -wal is left, so restore-backup would refuse")
        after = file_alone(self.live, self.directory / "after")
        self.assertEqual(query(after, "SELECT version_num FROM alembic_version"), [(MIGRATED,)])
        self.assertEqual(
            query(after, "SELECT COUNT(*) FROM wallets WHERE label = 'written by the candidate'"),
            [(1,)],
        )

    def test_a_missing_database_is_absent_and_not_created(self) -> None:
        process = run_script(deploy.REVISION_SCRIPT, self.live)

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(stdout(process), "absent")
        self.assertEqual(list(self.live.parent.iterdir()), [], "it created a database")

    def test_a_database_without_exactly_one_revision_fails(self) -> None:
        for case, versions in {
            "no alembic_version": None,
            "an empty alembic_version": (),
            "two revisions": (REVISION, MIGRATED),
        }.items():
            with self.subTest(case=case):
                self.setUp()
                make_database(self.live, versions)

                process = run_script(deploy.REVISION_SCRIPT, self.live)

                self.assertNotEqual(process.returncode, 0, stdout(process))
                self.assertNotIn(REVISION, stdout(process))


class StreamScriptTests(ScriptTestCase):
    """``STREAM_SCRIPT <directory> <name> <size>``: stdin into the backups volume, byte for
    byte, under the name only when exactly ``size`` bytes arrived."""

    def setUp(self) -> None:
        super().setUp()
        self.backups = self.directory / "backups"
        self.backups.mkdir()
        self.name = deploy.copy_name(datetime(2026, 10, 5, 12, 34, 56, 789012, tzinfo=UTC))

    def test_stdin_lands_byte_for_byte_under_a_name_the_application_accepts(self) -> None:
        process = run_script(
            deploy.STREAM_SCRIPT, self.backups, self.name, len(AWKWARD), stdin=AWKWARD
        )

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(stdout(process), f"ok {len(AWKWARD)}")
        self.assertRegex(self.name, BACKUP_NAME)
        self.assertEqual(sorted(p.name for p in self.backups.iterdir()), [self.name])
        self.assertEqual((self.backups / self.name).read_bytes(), AWKWARD)

    def test_a_snapshot_round_trips_into_a_copy_restore_backup_accepts(self) -> None:
        make_database(self.live)
        snapshot = self.directory / "data" / "deploy-backup-attempt.sqlite3"
        taken = run_script(deploy.SNAPSHOT_SCRIPT, self.live, snapshot)
        self.assertEqual(stdout(taken), f"ok {REVISION}", taken.stderr)

        process = run_script(
            deploy.STREAM_SCRIPT,
            self.backups,
            self.name,
            snapshot.stat().st_size,
            stdin=snapshot.read_bytes(),
        )

        self.assertEqual(stdout(process), f"ok {snapshot.stat().st_size}", process.stderr)
        copy = self.backups / self.name
        self.assertEqual(copy.read_bytes(), snapshot.read_bytes())
        # What restore-backup checks (spec 029, steps 2 and 3): one file, no -wal, intact,
        # one revision. Checked on a copy, so this check leaves nothing beside the original.
        self.assertEqual(journal_header(copy), b"\x01\x01")
        self.assertFalse(sidecar(copy, "-wal").exists())
        alone = file_alone(copy, self.directory / "alone")
        self.assertEqual(query(alone, "PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(query(alone, "SELECT version_num FROM alembic_version"), [(REVISION,)])

    def test_an_empty_stdin_is_an_empty_file_and_says_so(self) -> None:
        process = run_script(deploy.STREAM_SCRIPT, self.backups, self.name, 0, stdin=b"")

        self.assertEqual(stdout(process), "ok 0", process.stderr)
        self.assertEqual((self.backups / self.name).read_bytes(), b"")

    def test_a_name_already_taken_is_refused_and_left_alone(self) -> None:
        (self.backups / self.name).write_bytes(b"an older copy with this name\n")

        process = run_script(
            deploy.STREAM_SCRIPT, self.backups, self.name, len(AWKWARD), stdin=AWKWARD
        )

        self.assertNotEqual(process.returncode, 0)
        self.assertEqual((self.backups / self.name).read_bytes(), b"an older copy with this name\n")
        self.assertEqual(sorted(p.name for p in self.backups.iterdir()), [self.name])

    def test_a_name_that_is_not_a_copy_s_is_refused(self) -> None:
        # The name is joined to the directory: it must never reach outside it.
        for name in (
            "portfolio.db",
            f"../{self.name}",
            f"sub/{self.name}",
            self.name.replace(".sqlite3", ".db"),
        ):
            with self.subTest(name=name):
                process = run_script(deploy.STREAM_SCRIPT, self.backups, name, 4, stdin=b"data")

                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(list(self.backups.iterdir()), [])
                self.assertFalse((self.directory / self.name).exists())

    def test_fewer_bytes_than_the_size_leave_nothing_and_say_both_counts(self) -> None:
        # A pipe cut short: what arrived is a prefix, and restore-backup must never see it.
        received = AWKWARD[:-100]

        process = run_script(
            deploy.STREAM_SCRIPT, self.backups, self.name, len(AWKWARD), stdin=received
        )

        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(stdout(process), "", "no ok for a short copy")
        # No final file and no .partial: the directory is as it was.
        self.assertEqual(list(self.backups.iterdir()), [])
        message = process.stderr.decode("utf-8")
        self.assertRegex(message, rf"\b{len(received)}\b")
        self.assertRegex(message, rf"\b{len(AWKWARD)}\b")

    def test_any_other_count_is_refused_too(self) -> None:
        for data, size in ((AWKWARD, len(AWKWARD) - 1), (b"", 10), (b"data", 0)):
            with self.subTest(received=len(data), size=size):
                process = run_script(
                    deploy.STREAM_SCRIPT, self.backups, self.name, size, stdin=data
                )

                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(list(self.backups.iterdir()), [])
                message = process.stderr.decode("utf-8")
                self.assertRegex(message, rf"\b{len(data)}\b")
                self.assertRegex(message, rf"\b{size}\b")

    def test_a_missing_or_malformed_size_writes_nothing(self) -> None:
        for size in ((), ("four",), ("",), ("4.0",)):
            with self.subTest(size=size):
                process = run_script(
                    deploy.STREAM_SCRIPT, self.backups, self.name, *size, stdin=b"data"
                )

                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(list(self.backups.iterdir()), [])


class CopyNameTests(unittest.TestCase):
    """``copy_name``: the application's own name for a copy, stamped in UTC."""

    def test_the_pattern_is_the_application_s(self) -> None:
        # Read with ast from backend/src/portfolio/domain/backups.py by the harness; it
        # anchors with \\A and \\Z, so a trailing newline does not pass.
        self.assertTrue(BACKUP_NAME.pattern.startswith(r"\Aportfolio-"))
        self.assertIsNone(BACKUP_NAME.fullmatch("portfolio-20261005T123456789012Z.sqlite3\n"))

    def test_a_copy_name_is_one_the_application_accepts(self) -> None:
        instant = datetime(2026, 10, 5, 12, 34, 56, 789012, tzinfo=UTC)
        name = deploy.copy_name(instant)
        self.assertEqual(name, "portfolio-20261005T123456789012Z.sqlite3")
        self.assertRegex(name, BACKUP_NAME)
        self.assertEqual(
            deploy.copy_name(datetime(2026, 1, 2, tzinfo=UTC)),
            "portfolio-20260102T000000000000Z.sqlite3",
        )

    def test_it_is_stamped_in_utc(self) -> None:
        east = timezone(timedelta(hours=3))
        self.assertEqual(
            deploy.copy_name(datetime(2026, 10, 5, 15, 34, 56, 789012, tzinfo=east)),
            "portfolio-20261005T123456789012Z.sqlite3",
        )

    def test_a_naive_instant_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            deploy.copy_name(datetime(2026, 10, 5, 12, 34, 56))


class SafetyCopyNameTests(unittest.TestCase):
    """``safety_copy_name``: the one thing of restore-backup's output a record may hold
    (spec 034, R5), and only as the name that directly follows "safety copy"."""

    SAFETY = "portfolio-20261005T123456789012Z.sqlite3"
    RESTORED = "portfolio-20261004T030000000000Z.sqlite3"
    OTHER = "portfolio-20261005T123457000000Z.sqlite3"

    def test_the_line_restore_backup_prints_on_success(self) -> None:
        line = f"The database as it was before is in the safety copy {self.SAFETY}."
        self.assertEqual(deploy.safety_copy_name(line), self.SAFETY)

    def test_the_sentence_of_a_restore_that_failed_after_taking_it(self) -> None:
        # db/backup.py, _before_sentence, at the end of the failure's message.
        message = (
            "Command failed (1): Writing the backup into /app/data/portfolio.db failed: disk "
            f"I/O error (SQLITE_IOERR). The safety copy {self.SAFETY} holds the database as "
            "it was before the restore."
        )
        self.assertEqual(deploy.safety_copy_name(message), self.SAFETY)

    def test_a_whole_report_gives_the_safety_copy_and_not_the_restored_one(self) -> None:
        report = "\n".join(
            [
                f"Restored {self.RESTORED}.",
                f"The database as it was before is in the safety copy {self.SAFETY}.",
                "Rows per table after the restore:",
                "  accounts: 3",
            ]
        )
        self.assertEqual(deploy.safety_copy_name(report), self.SAFETY)

    def test_the_same_name_twice_is_that_name(self) -> None:
        text = (
            f"The database as it was before is in the safety copy {self.SAFETY}.\n"
            f"The safety copy {self.SAFETY} holds the database as it was before the restore."
        )
        self.assertEqual(deploy.safety_copy_name(text), self.SAFETY)

    def test_no_name_or_two_different_names_give_none(self) -> None:
        for text in (
            "",
            "There was no database to copy first, so no safety copy was taken.",
            "There was no database before the restore, so there is no safety copy.",
            "The live database did not pass its own check and was moved aside to "
            "/app/data/portfolio.db.damaged-20261005T123456789012Z, which holds it as it "
            "was; there is no safety copy.",
            f"The safety copy {self.SAFETY} holds it, and so does the safety copy "
            f"{self.OTHER}.",
            f"The safety copy {self.SAFETY} holds the database.\n"
            f"The database as it was before is in the safety copy {self.OTHER}.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(deploy.safety_copy_name(text))

    def test_a_name_that_does_not_follow_safety_copy_is_not_taken(self) -> None:
        for text in (
            # On the same line as "safety copy", but not after it.
            f"Restored {self.RESTORED}; no safety copy was taken.",
            f"No safety copy was taken before {self.RESTORED} was restored.",
            f"The safety copy: {self.SAFETY}.",
            f"The safety copy holds nothing; see {self.SAFETY}.",
            # Not the words "safety copy", and not a whole copy name.
            f"The unsafety copy {self.SAFETY}.",
            f"The safety copy {self.SAFETY}x.",
            f"The safety copy {self.SAFETY[:-1]}.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(deploy.safety_copy_name(text))


class RunInputFileTests(unittest.TestCase):
    """R8: ``run(..., input_file=)`` gives the file to the command as stdin, unaltered."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / "incoming"
        self.directory.mkdir()
        self.source = self.directory / "database.sqlite3"
        self.source.write_bytes(AWKWARD)

    def test_the_file_reaches_stdin_byte_for_byte(self) -> None:
        reader = (
            "import hashlib, sys; data = sys.stdin.buffer.read(); "
            "print(len(data), hashlib.sha256(data).hexdigest())"
        )

        output = deploy.run([sys.executable, "-c", reader], input_file=self.source)

        self.assertEqual(output, f"{len(AWKWARD)} {hashlib.sha256(AWKWARD).hexdigest()}")

    def test_the_file_is_closed_afterwards_even_on_failure(self) -> None:
        # Windows refuses to rename a directory holding an open file, and record_failure
        # renames incoming/ to failed/ right after the rollback.
        deploy.run([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                   input_file=self.source)
        with self.assertRaises(deploy.DeploymentError) as caught:
            deploy.run(
                [sys.executable, "-c", "import sys; sys.stdin.buffer.read(); sys.exit(3)"],
                input_file=self.source,
            )
        self.assertEqual(str(caught.exception), "Command failed (3): No diagnostic output")
        moved = self.directory.with_name("failed")
        self.directory.rename(moved)
        (moved / "database.sqlite3").unlink()

    def test_without_an_input_file_the_command_reads_an_empty_stdin(self) -> None:
        """deploy.py's own stdin is the ssh session from the Actions runner, and compose run
        attaches stdin by default: a command it runs without ``input_file`` must find
        nothing there, never what deploy.py was given.

        Run in a child whose stdin holds bytes, as deploy.py runs: a command that inherited
        it would read them, and one given /dev/null reads nothing and ends.
        """
        wrapper = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('deploy', sys.argv[1])\n"
            "deploy = importlib.util.module_from_spec(spec)\n"
            "sys.modules['deploy'] = deploy\n"
            "spec.loader.exec_module(deploy)\n"
            "reader = 'import sys; print(len(sys.stdin.buffer.read()))'\n"
            "print(deploy.run([sys.executable, '-c', reader]))\n"
            "print(deploy.run([sys.executable, '-c', reader], input_file=sys.argv[2]))\n"
        )
        session = b"what the runner's ssh session sends\n"

        process = run_script(wrapper, deploy.__file__, self.source, stdin=session)

        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(stdout(process).splitlines(), ["0", str(len(AWKWARD))])

    def test_a_failure_names_neither_the_file_nor_what_it_holds(self) -> None:
        self.source.write_bytes(b"what the database holds\n")
        with self.assertRaises(deploy.DeploymentError) as caught:
            deploy.run(
                [sys.executable, "-c", "import sys; sys.stdin.buffer.read(); sys.exit(4)"],
                input_file=self.source,
            )
        message = str(caught.exception)
        for revealing in (str(self.source), self.source.as_posix(), "what the database holds"):
            self.assertNotIn(revealing, message)


if __name__ == "__main__":
    unittest.main()
