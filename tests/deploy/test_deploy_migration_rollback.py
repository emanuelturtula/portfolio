"""A rollback puts back the database a failed candidate migrated (issue #140, spec 034).

The application migrates its database forward at startup and never backwards, and an image
that does not know the database's revision refuses to start. ``FakeDocker`` models both: a
candidate scripted in ``migrations`` moves the live database to a new revision as it starts,
before its health check can fail, and an image started on a revision it does not know fails
its ``up``. So a rollback that skipped the restore here ends ``rollback=failed``, exactly as
production did before this change.

docs/specs/034-rollback-restores-the-database.md's test plan is the checklist; each test
names the criterion it proves. The real in-container scripts are proven against real SQLite
files in ``test_rollback_scripts.py``: a fake Docker proves the choreography, not the
scripts.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from deploy_harness import (
    BACKUP_NAME,
    BASE_REVISION,
    RUN_ARGS,
    SENTINEL_ENV_LINE,
    SENTINEL_ROW_COUNT,
    UP_ARGS,
    Call,
    Host,
    Release,
    deploy,
    read_manifest,
    tree,
)

R1, R2, R3, R9 = Release(1), Release(2), Release(3), Release(9)
MIGRATED = "0012_candidate_migration"
DATABASE = "/app/data/portfolio.db"
RESTORED_SEQUENCE = [
    "compose-stop",
    "compose-run-revision",
    "compose-run-stream",
    "compose-run-restore",
    "compose-up",
    "compose-ps",
    "inspect",
]
UNCHANGED_SEQUENCE = ["compose-stop", "compose-run-revision", "compose-up", "compose-ps", "inspect"]
ONE_OFF_KINDS = ("compose-run-revision", "compose-run-stream", "compose-run-restore")


class RollbackTestCase(unittest.TestCase):
    """A host with R1 then R2 deployed: R2 is live, with a backup of R1 behind it."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.host = Host(Path(temp.name))
        self.docker = self.host.docker
        self.addCleanup(self.assert_no_unexpected_commands)
        self.host.write_env_file(self.host.prod)
        self.host.deploy(R1)
        self.host.deploy(R2)
        self.live_files = self.prod_without_evidence()
        # What the next attempt's snapshot takes: the database R2 runs on.
        self.snapshot = self.docker.database
        self.assertIsNotNone(self.snapshot)
        self.assertEqual(self.docker.revision, BASE_REVISION)
        self.docker.calls.clear()

    def assert_no_unexpected_commands(self) -> None:
        self.assertEqual(
            self.docker.unexpected, [], "deploy.py ran a command the fake does not know"
        )

    @property
    def prod(self) -> Path:
        return self.host.prod

    def prod_without_evidence(self) -> dict[str, bytes]:
        return {
            name: data
            for name, data in tree(self.prod).items()
            if not name.startswith("failed/") and name != "last-attempt.json"
        }

    def migrating_candidate(self, release: Release = R3) -> None:
        """``release`` migrates the database as it starts, then fails its health check."""
        self.docker.migrations[release.image] = MIGRATED
        self.docker.unhealthy.add(release.image)

    def assert_fails(self, release: Release = R3) -> str:
        started = datetime.now(UTC)
        with self.assertRaises(deploy.DeploymentError) as caught:
            self.host.deploy(release)
        self.window = (started, datetime.now(UTC))
        return str(caught.exception)

    def failed_result(self) -> dict[str, Any]:
        """failed/result.json, which last-attempt.json must repeat exactly."""
        result = read_manifest(self.prod / "failed" / "result.json")
        self.assertEqual(read_manifest(self.prod / "last-attempt.json"), result)
        self.assertFalse((self.prod / "incoming").exists())
        return result

    def after_candidate(self, verify: tuple[str, ...] = ()) -> list[Call]:
        """Every call after the candidate's ``up`` and the ``verify`` calls it got as far as.

        Nothing may come between those and the rollback's first step.
        """
        kinds = self.docker.kinds()
        up = kinds.index("compose-up")
        candidate_up = self.docker.calls[up]
        self.assertEqual(candidate_up.image, R3.image, "the first up is the candidate's")
        self.assertEqual(tuple(kinds[up + 1 : up + 1 + len(verify)]), verify)
        return self.docker.calls[up + 1 + len(verify) :]

    def assert_previous_deployment(self, call: Call) -> None:
        """``call`` was made with the previous deployment's manifest and compose file."""
        self.assertEqual(Path(call.compose_file or ""), self.prod / "compose.yml", call.kind)
        self.assertEqual((call.image, call.compose_bytes), (R2.image, R2.compose), call.kind)
        self.assertEqual(call.project, "portfolio-app-prod", call.kind)
        self.assertEqual(call.env["PORTFOLIO_PORT"], "8083", call.kind)
        self.assertEqual(call.env["PORTFOLIO_ENVIRONMENT"], "prod", call.kind)
        self.assertEqual(
            call.env["PORTFOLIO_SECRETS_ENV_FILE"], str(self.prod / "secrets.env"), call.kind
        )

    def assert_stop_of_the_candidate(self, stop: Call) -> None:
        self.assertEqual(stop.kind, "compose-stop")
        self.assertEqual(stop.compose_args, ("stop", "app"))
        self.assertEqual(Path(stop.compose_file or ""), self.prod / "incoming" / "compose.yml")
        self.assertEqual((stop.image, stop.compose_bytes), (R3.image, R3.compose))
        self.assertEqual(stop.env["PORTFOLIO_SECRETS_ENV_FILE"], str(self.prod / "secrets.env"))
        self.assertIsNotNone(stop.writer, "the premise: the candidate was running")

    def assert_revision_read(self, revision: Call) -> None:
        self.assertEqual(revision.kind, "compose-run-revision")
        self.assert_previous_deployment(revision)
        self.assertEqual(
            revision.compose_args,
            (*RUN_ARGS, "python", "-c", deploy.REVISION_SCRIPT, DATABASE),
        )
        self.assertIsNone(revision.writer, "the revision was read while the candidate ran")


class MigratedCandidateTests(RollbackTestCase):
    """Criteria 1 and 2: the previous version runs, healthy, on the database it left."""

    def assert_restored(self, message: str, verify: tuple[str, ...] = ()) -> dict[str, Any]:
        calls = self.after_candidate(verify)
        self.assertEqual([call.kind for call in calls], RESTORED_SEQUENCE)
        stop, revision, stream, restore, up = calls[:5]

        # 1. The candidate is stopped first, through its own compose file.
        self.assert_stop_of_the_candidate(stop)
        # 2. The live revision is read by a one-off container of the previous image.
        self.assert_revision_read(revision)
        self.assertEqual(revision.database[1], MIGRATED, "the premise: the candidate migrated")
        migrated = revision.database[0]
        # 3. The attempt's own snapshot goes into the backups volume through stdin...
        name = stream.compose_args[-1]
        self.assertEqual(
            stream.compose_args,
            (*RUN_ARGS, "python", "-c", deploy.STREAM_SCRIPT, "/app/backups", name),
        )
        self.assertRegex(name, BACKUP_NAME)
        # R4: stamped in UTC when the restore starts, during this deployment.
        stamp = datetime.strptime(name[len("portfolio-") : -len(".sqlite3")], "%Y%m%dT%H%M%S%fZ")
        started, ended = self.window
        self.assertLessEqual(started, stamp.replace(tzinfo=UTC))
        self.assertLessEqual(stamp.replace(tzinfo=UTC), ended)
        self.assertEqual(stream.input_file, self.prod / "incoming" / "database.sqlite3")
        self.assertEqual(stream.stdin, self.snapshot)
        self.assertEqual(
            [call.kind for call in self.docker.calls if call.stdin is not None],
            ["compose-run-stream"],
            "only the stream is given stdin",
        )
        # ...and the previous image's own restore-backup puts it back.
        self.assertEqual(
            restore.compose_args,
            (*RUN_ARGS, "python", "-m", "portfolio", "restore-backup", name),
        )
        for call in (revision, stream, restore, up):
            self.assert_previous_deployment(call)
        for call in (revision, stream, restore):
            self.assertIsNone(call.writer, f"{call.kind} ran while the candidate held the data")
        # 4. The previous image starts on the snapshot, not on the migrated database.
        self.assertEqual(up.compose_args, UP_ARGS)
        self.assertEqual(up.database, (self.snapshot, BASE_REVISION))
        self.assertEqual(self.docker.refused_starts, [])
        self.assertEqual(self.docker.running_image, R2.image)
        self.assertEqual(self.docker.revision, BASE_REVISION)
        # R7: the streamed copy and the safety copy stay in the backups volume.
        (safety,) = self.docker.safety_copies
        self.assertEqual(self.docker.restored, [name])
        self.assertEqual(self.docker.backups, {name: self.snapshot, safety: migrated})
        self.assertNotEqual(safety, name)

        result = self.failed_result()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["rollback"], "healthy")
        self.assertNotIn("rollback_error", result)
        self.assertIs(result["backup"], True)
        self.assertEqual(result["database"], "restored")
        self.assertEqual(result["database_revision"], BASE_REVISION)
        self.assertEqual(result["database_revision_live"], MIGRATED)
        self.assertEqual(result["database_restored_from"], name)
        self.assertEqual(result["database_safety_copy"], safety)
        # R7: the snapshot also stays in failed/, as it always has.
        self.assertEqual((self.prod / "failed" / "database.sqlite3").read_bytes(), self.snapshot)
        self.assertRegex(
            message, r"\ADeployment failed; rollback=healthy; database=restored; evidence=\S"
        )
        self.assertEqual(self.prod_without_evidence(), self.live_files, "the live files moved")
        return result

    def test_a_candidate_that_migrates_and_fails_its_health_check(self) -> None:
        """Criteria 1, 2: in order, stop, revision read, stream, restore-backup, up."""
        self.migrating_candidate()

        message = self.assert_fails()

        self.assert_restored(message)

    def test_a_candidate_that_migrates_and_fails_the_digest_check(self) -> None:
        """Criterion 1: the same when ``up`` succeeds but the container runs another digest."""
        self.docker.substitute[R3.image] = R9.image
        self.docker.migrations[R9.image] = MIGRATED

        message = self.assert_fails()

        result = self.assert_restored(message, verify=("compose-ps", "inspect"))
        self.assertIn("does not match", result["error"])

    def test_the_revision_read_recovers_the_wal_an_unclean_stop_left(self) -> None:
        """R3: a candidate killed after its grace period leaves a -wal, which restore-backup
        reads as an open database and refuses. The read-write revision read recovers it."""
        self.migrating_candidate()
        self.docker.unclean_stop = True

        message = self.assert_fails()

        self.assert_restored(message)
        (revision,) = self.docker.of_kind("compose-run-revision")
        (restore,) = self.docker.of_kind("compose-run-restore")
        self.assertTrue(revision.wal, "the premise: the stop left a -wal")
        self.assertFalse(restore.wal)

    def test_without_the_restore_this_rollback_would_fail(self) -> None:
        """The model is what makes the tests above mean something: with the database step
        taken out, the previous image is started on the migrated database and fails, as
        production did before #140."""
        self.migrating_candidate()

        with mock.patch.object(deploy, "roll_back_database", lambda *args, **kwargs: None):
            message = self.assert_fails()

        self.assertIn("rollback=failed", message)
        self.assertEqual(self.docker.refused_starts, [(R2.image, MIGRATED)])
        self.assertIn("Can't locate revision", self.failed_result()["rollback_error"])

    def test_the_safety_copy_is_read_only_from_the_line_that_names_one(self) -> None:
        """R5: a live database moved aside as damaged, or none at all, names no safety copy,
        though its line mentions one and the output names the restored copy."""
        for outcome in ("damaged", "none"):
            with self.subTest(outcome=outcome):
                self.setUp()
                self.migrating_candidate()
                self.docker.restore_outcome = outcome

                message = self.assert_fails()

                self.assertIn("database=restored", message)
                result = self.failed_result()
                self.assertEqual((result["rollback"], result["database"]), ("healthy", "restored"))
                self.assertNotIn("database_safety_copy", result)
                self.assertEqual(self.docker.safety_copies, [])
                self.assertRegex(result["database_restored_from"], BACKUP_NAME)

    def test_a_successful_deployment_records_the_revision_it_started_from(self) -> None:
        """The success path: the snapshot's revision is in current.json, and no rollback
        step runs."""
        self.docker.migrations[R3.image] = MIGRATED

        self.host.deploy(R3)

        current = self.host.current()
        self.assertEqual(current["database_revision"], BASE_REVISION)
        self.assertEqual(self.docker.revision, MIGRATED)
        for kind in ("compose-stop", *ONE_OFF_KINDS):
            self.assertNotIn(kind, self.docker.kinds())
        self.assertEqual(self.docker.backups, {})

    def test_a_snapshot_without_one_revision_records_none(self) -> None:
        self.docker.revision = None  # alembic_version missing, or not one row

        self.host.deploy(R3)

        self.assertIs(self.host.current()["backup"], True)
        self.assertIsNone(self.host.current().get("database_revision"))


class UnmigratedCandidateTests(RollbackTestCase):
    """Criterion 5: a candidate that fails without migrating leaves the database untouched."""

    def test_no_restore_runs_when_the_revision_did_not_move(self) -> None:
        for why in ("unhealthy", "fail_up", "digest"):
            with self.subTest(why=why):
                self.setUp()
                verify: tuple[str, ...] = ()
                if why == "unhealthy":
                    self.docker.unhealthy.add(R3.image)
                elif why == "fail_up":
                    self.docker.fail_up.add(R3.image)
                else:
                    self.docker.substitute[R3.image] = R9.image
                    verify = ("compose-ps", "inspect")

                message = self.assert_fails()

                calls = self.after_candidate(verify)
                self.assertEqual([call.kind for call in calls], UNCHANGED_SEQUENCE)
                stop, revision, up = calls[:3]
                self.assert_stop_of_the_candidate(stop)
                self.assert_revision_read(revision)
                self.assert_previous_deployment(up)
                for kind in ("compose-run-stream", "compose-run-restore"):
                    self.assertNotIn(kind, self.docker.kinds())
                self.assertEqual(self.docker.backups, {})
                self.assertEqual(self.docker.restored, [])
                # Nothing touched the database between the read and the previous image's start.
                self.assertEqual(up.database, revision.database)
                self.assertEqual(up.database[1], BASE_REVISION)
                self.assertEqual(self.docker.running_image, R2.image)

                result = self.failed_result()
                self.assertEqual((result["rollback"], result["database"]), ("healthy", "unchanged"))
                self.assertEqual(result["database_revision"], BASE_REVISION)
                self.assertEqual(result["database_revision_live"], BASE_REVISION)
                self.assertNotIn("database_restored_from", result)
                self.assertNotIn("database_safety_copy", result)
                self.assertEqual(
                    (self.prod / "failed" / "database.sqlite3").read_bytes(), self.snapshot
                )
                self.assertRegex(
                    message,
                    r"\ADeployment failed; rollback=healthy; database=unchanged; evidence=\S",
                )

    def test_with_no_database_before_or_after_the_state_is_unchanged(self) -> None:
        self.docker.database = None  # no database file at all: nothing to snapshot
        self.docker.unhealthy.add(R3.image)

        message = self.assert_fails()

        self.assertEqual(
            [call.kind for call in self.after_candidate()], UNCHANGED_SEQUENCE
        )
        result = self.failed_result()
        self.assertIs(result["backup"], False)
        self.assertEqual((result["rollback"], result["database"]), ("healthy", "unchanged"))
        self.assertIsNone(result.get("database_revision_live"))
        self.assertIn("database=unchanged", message)


class FailedRestoreTests(RollbackTestCase):
    """Criterion 6: a restore that fails never starts the previous image on the migrated
    database, ends ``rollback=failed`` and keeps the snapshot in failed/."""

    def test_a_failed_restore_or_stream_ends_restore_failed(self) -> None:
        # "short": the stream ended early and said so. Restoring half a snapshot is never
        # attempted; restore-backup would refuse it, but the check is deploy.py's to make.
        switches = {"restore": "fail_restore", "stream": "fail_stream", "short": "short_stream"}
        for broken, switch in switches.items():
            with self.subTest(broken=broken):
                self.setUp()
                self.migrating_candidate()
                setattr(self.docker, switch, True)

                message = self.assert_fails()

                expected = ["compose-stop", "compose-run-revision", "compose-run-stream"]
                if broken == "restore":
                    expected.append("compose-run-restore")
                calls = self.after_candidate()
                self.assertEqual(
                    [call.kind for call in calls], expected, "nothing may follow a failed restore"
                )
                self.assertEqual(self.docker.refused_starts, [])
                self.assertEqual(
                    [call.image for call in self.docker.of_kind("compose-up")], [R3.image]
                )
                self.assertIsNone(self.docker.running, "production is down, and says so")
                self.assertEqual(self.docker.revision, MIGRATED, "the restore wrote nothing")

                result = self.failed_result()
                self.assertEqual(result["rollback"], "failed")
                self.assertEqual(result["database"], "restore_failed")
                self.assertEqual(result["database_revision"], BASE_REVISION)
                self.assertEqual(result["database_revision_live"], MIGRATED)
                self.assertNotIn("database_safety_copy", result)
                self.assertTrue(result["rollback_error"])
                why = {"restore": "Refusing to restore", "stream": "No space left"}.get(broken)
                if why is not None:
                    self.assertIn(why, result["rollback_error"])
                if broken != "restore":
                    self.assertNotIn("database_restored_from", result)
                elif "database_restored_from" in result:
                    self.assertEqual(result["database_restored_from"], calls[2].compose_args[-1])
                self.assertEqual(
                    (self.prod / "failed" / "database.sqlite3").read_bytes(), self.snapshot
                )
                self.assertRegex(
                    message,
                    r"\ADeployment failed; rollback=failed; database=restore_failed; evidence=\S",
                )
                # Criterion 9: the reason stays in result.json, on the host, never in the line.
                self.assertNotIn(result["rollback_error"], message)
                for text in ("Refusing to restore", "No space left", "Command failed"):
                    self.assertNotIn(text, message)
                self.assertEqual(self.prod_without_evidence(), self.live_files)


class CarriedSnapshotTests(unittest.TestCase):
    """Criterion 7, R6: with only a carried snapshot, a migrated database is not restored.

    R1 is deployed; R2 snapshots it, fails, and its rollback comes up unhealthy, so the only
    copy is R2's snapshot in failed/ and the live container cannot be snapshotted again.
    """

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.host = Host(Path(temp.name))
        self.docker = self.host.docker
        self.prod = self.host.prod
        self.host.deploy(R1)
        self.docker.fail_up.add(R2.image)
        self.docker.unhealthy.add(R1.image)
        with self.assertRaises(deploy.DeploymentError):
            self.host.deploy(R2)
        self.carried = (self.prod / "failed" / "database.sqlite3").read_bytes()
        self.docker.fail_up.clear()
        # R1's image could start healthy again: only the revision can stop it now.
        self.docker.unhealthy.discard(R1.image)
        self.assertEqual(self.docker.containers[self.docker.running or ""].health, "unhealthy")
        self.docker.calls.clear()

    def test_a_migrated_database_with_only_a_carried_snapshot_is_not_restored(self) -> None:
        self.docker.migrations[R3.image] = MIGRATED
        self.docker.unhealthy.add(R3.image)

        with self.assertRaises(deploy.DeploymentError) as caught:
            self.host.deploy(R3)
        message = str(caught.exception)

        self.assertEqual(self.docker.unexpected, [])
        kinds = self.docker.kinds()
        up = kinds.index("compose-up")
        self.assertEqual(kinds[up + 1 :], ["compose-stop", "compose-run-revision", "compose-up"])
        for kind in ("compose-run-stream", "compose-run-restore"):
            self.assertNotIn(kind, kinds)
        self.assertEqual(self.docker.backups, {})
        self.assertEqual(self.docker.restored, [])
        # The previous image was still tried, and failed on the revision, as R6 says.
        self.assertEqual(self.docker.refused_starts, [(R1.image, MIGRATED)])

        result = read_manifest(self.prod / "failed" / "result.json")
        self.assertIs(result["backup"], False, "the premise: no snapshot of its own")
        self.assertTrue(result["backup_carried_from"])
        self.assertEqual(result["database"], "not_restored")
        self.assertEqual(result["database_revision_live"], MIGRATED)
        self.assertIsNone(result.get("database_revision"), "a revision it never read")
        self.assertNotIn("database_restored_from", result)
        self.assertNotIn("database_safety_copy", result)
        self.assertEqual(result["rollback"], "failed")
        self.assertIn("Can't locate revision", result["rollback_error"])
        self.assertEqual((self.prod / "failed" / "database.sqlite3").read_bytes(), self.carried)
        self.assertRegex(
            message, r"\ADeployment failed; rollback=failed; database=not_restored; evidence=\S"
        )

    def test_an_unmigrated_database_with_only_a_carried_snapshot_is_not_restored_either(
        self,
    ) -> None:
        # R6: with no revision of its own to compare with, any database counts as moved.
        # The previous image then comes up, which shows the candidate had not migrated.
        self.docker.unhealthy.add(R3.image)

        with self.assertRaises(deploy.DeploymentError) as caught:
            self.host.deploy(R3)

        self.assertEqual(self.docker.unexpected, [])
        kinds = self.docker.kinds()
        up = kinds.index("compose-up")
        self.assertEqual(
            kinds[up + 1 :],
            ["compose-stop", "compose-run-revision", "compose-up", "compose-ps", "inspect"],
        )
        self.assertEqual(self.docker.backups, {})
        self.assertEqual(self.docker.running_image, R1.image)
        result = read_manifest(self.prod / "failed" / "result.json")
        self.assertIs(result["backup"], False)
        self.assertEqual((result["rollback"], result["database"]), ("healthy", "not_restored"))
        self.assertEqual(result["database_revision_live"], BASE_REVISION)
        self.assertRegex(
            str(caught.exception),
            r"\ADeployment failed; rollback=healthy; database=not_restored; evidence=\S",
        )


class UnreadDatabaseTests(RollbackTestCase):
    """Criterion 10, R12: a stop or a revision read that fails has changed nothing, so the
    state is ``unread`` with ``database_error``, and the previous image is started anyway.

    Only a failed stream or restore skips that ``up`` (FailedRestoreTests).
    """

    BREAKS = {
        "stop": ("fail_stop", ["compose-stop"]),
        "revision read": ("fail_revision_read", ["compose-stop", "compose-run-revision"]),
        # The live database holds no single alembic_version row: the read exits non-zero.
        "no single revision": ("revision", ["compose-stop", "compose-run-revision"]),
    }

    def break_step(self, broken: str) -> list[str]:
        switch, steps = self.BREAKS[broken]
        if switch == "revision":
            self.docker.revision = None
        else:
            setattr(self.docker, switch, True)
        return steps

    def assert_unread(self, message: str, rollback: str) -> dict[str, Any]:
        result = self.failed_result()
        self.assertEqual(result["database"], "unread")
        self.assertTrue(result["database_error"])
        self.assertEqual(result["rollback"], rollback)
        for field in ("database_revision_live", "database_restored_from", "database_safety_copy"):
            self.assertNotIn(field, result)
        for kind in ("compose-run-stream", "compose-run-restore"):
            self.assertNotIn(kind, self.docker.kinds())
        self.assertEqual(self.docker.backups, {})
        self.assertRegex(
            message, rf"\ADeployment failed; rollback={rollback}; database=unread; evidence=\S"
        )
        self.assertNotIn(result["database_error"], message, "the reason stays on the host")
        return result

    def test_a_failed_step_still_starts_the_previous_image(self) -> None:
        """Criterion 10: the candidate did not migrate, so the previous image comes up."""
        for broken in self.BREAKS:
            with self.subTest(broken=broken):
                self.setUp()
                steps = self.break_step(broken)
                self.docker.unhealthy.add(R3.image)

                message = self.assert_fails()

                calls = self.after_candidate()
                self.assertEqual(
                    [call.kind for call in calls], [*steps, "compose-up", "compose-ps", "inspect"]
                )
                up = calls[len(steps)]
                self.assert_previous_deployment(up)
                self.assertEqual(up.compose_args, UP_ARGS)
                self.assertEqual(self.docker.running_image, R2.image)
                result = self.assert_unread(message, "healthy")
                self.assertNotIn("rollback_error", result)
                self.assertEqual(
                    (self.prod / "failed" / "database.sqlite3").read_bytes(), self.snapshot
                )
                self.assertEqual(self.prod_without_evidence(), self.live_files)

    def test_a_failed_step_after_a_migration_still_tries_the_previous_image(self) -> None:
        """R12: the up is attempted even then; it fails on the revision, and says why."""
        for broken in ("stop", "revision read"):
            with self.subTest(broken=broken):
                self.setUp()
                self.break_step(broken)
                self.migrating_candidate()

                message = self.assert_fails()

                self.assertEqual(
                    [call.image for call in self.docker.of_kind("compose-up")],
                    [R3.image, R2.image],
                )
                self.assertEqual(self.docker.refused_starts, [(R2.image, MIGRATED)])
                result = self.assert_unread(message, "failed")
                self.assertIn("Can't locate revision", result["rollback_error"])

    def test_a_revision_read_that_prints_anything_else_records_none_of_it(self) -> None:
        # R5: only a revision is recorded, never a command's output.
        printed = f"warning: {SENTINEL_ROW_COUNT} rows"
        self.docker.revision = printed
        # Both images start on it, so only the rollback's read meets it.
        self.docker.knows[R3.image] = self.docker.knows[R2.image] = {printed}
        self.docker.unhealthy.add(R3.image)

        message = self.assert_fails()

        self.assertEqual(self.docker.running_image, R2.image)
        self.assert_unread(message, "healthy")
        for name, data in tree(self.host.home).items():
            self.assertNotIn(str(SENTINEL_ROW_COUNT).encode(), data, name)
        self.assertNotIn(str(SENTINEL_ROW_COUNT), message)


class SnapshotWithoutRevisionTests(RollbackTestCase):
    """Criterion 11, R11: an own snapshot holding no single revision is never restored.

    restore-backup would refuse it anyway, which would end ``restore_failed`` and leave the
    previous image unstarted.
    """

    def test_a_migrated_database_with_a_snapshot_without_a_revision_is_not_restored(
        self,
    ) -> None:
        self.docker.revision = None  # the snapshot reports a bare "ok"
        self.migrating_candidate()

        message = self.assert_fails()

        (backup,) = self.docker.of_kind("exec-backup")
        self.assertEqual(backup.output, "ok", "the premise: a bare ok")
        calls = self.after_candidate()
        self.assertEqual(
            [call.kind for call in calls], ["compose-stop", "compose-run-revision", "compose-up"]
        )
        for kind in ("compose-run-stream", "compose-run-restore"):
            self.assertNotIn(kind, self.docker.kinds())
        self.assertEqual(self.docker.backups, {})
        self.assertEqual(self.docker.restored, [])
        # The previous image is still tried, and fails on the revision, as for R6.
        self.assertEqual(self.docker.refused_starts, [(R2.image, MIGRATED)])

        result = self.failed_result()
        self.assertIs(result["backup"], True, "the premise: a snapshot of its own")
        self.assertIsNone(result.get("database_revision"))
        self.assertEqual(result["database_revision_live"], MIGRATED)
        self.assertEqual(result["database"], "not_restored")
        self.assertEqual(result["rollback"], "failed")
        self.assertNotIn("database_restored_from", result)
        self.assertEqual((self.prod / "failed" / "database.sqlite3").read_bytes(), self.snapshot)
        self.assertRegex(
            message, r"\ADeployment failed; rollback=failed; database=not_restored; evidence=\S"
        )


class RollbackStepFailureTests(RollbackTestCase):
    """A rollback that never reaches the database step records no database state."""

    def test_a_first_deployment_runs_no_database_step(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        host = Host(Path(temp.name))
        host.docker.migrations[R1.image] = MIGRATED
        host.docker.unhealthy.add(R1.image)

        with self.assertRaises(deploy.DeploymentError) as caught:
            host.deploy(R1)

        self.assertEqual(host.docker.unexpected, [])
        for kind in ("compose-stop", *ONE_OFF_KINDS):
            self.assertNotIn(kind, host.docker.kinds())
        result = read_manifest(host.prod / "failed" / "result.json")
        self.assertEqual(result["rollback"], "no_previous_deployment")
        self.assertNotIn("database", result)
        self.assertNotIn("database=", str(caught.exception))


class NothingTheRestorePrintsLeavesTheHostTests(RollbackTestCase):
    """Criterion 9, R5: no row count and no output of restore-backup reaches the message,
    result.json, last-attempt.json or anything printed; the secrets sentinel neither."""

    def run_main(self, release: Release) -> tuple[int, str, str]:
        self.host.stage_kit(release)
        self.docker.publish(release)
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            self.host.patched(),
            mock.patch.object(Path, "home", staticmethod(lambda: self.host.home)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = deploy.main(
                [
                    "--environment",
                    "prod",
                    "--image",
                    release.image,
                    "--revision",
                    release.revision,
                    "--version",
                    release.version,
                    "--run-number",
                    str(release.run_number),
                    "--source-run-url",
                    release.source_run_url,
                    "--root",
                    str(self.host.root),
                ]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_nothing_leaked(self, *printed: str) -> None:
        restore_output = [
            call.output for call in self.docker.of_kind("compose-run-restore") if call.output
        ]
        forbidden = [str(SENTINEL_ROW_COUNT), "Rows per table", "Restored portfolio-"]
        files = {
            name: data.decode("utf-8", errors="replace")
            for name, data in tree(self.host.home).items()
            if name != "portfolio-app/prod/secrets.env"
        }
        self.assertIn("portfolio-app/prod/failed/result.json", files)
        self.assertIn("portfolio-app/prod/last-attempt.json", files)
        for text in (*printed, *files.values()):
            for needle in forbidden:
                self.assertNotIn(needle, text)
        for name, text in files.items():
            for line in "\n".join(restore_output).splitlines():
                if line.strip() and "safety copy" not in line:
                    self.assertNotIn(line.strip(), text, name)
        # The secrets sentinel, on the new paths too: no file, no argument, no stdin.
        sentinel = SENTINEL_ENV_LINE.strip()
        self.assertEqual((self.prod / "secrets.env").read_bytes(), SENTINEL_ENV_LINE)
        for name, data in tree(self.host.home).items():
            if name != "portfolio-app/prod/secrets.env":
                self.assertNotIn(sentinel, data, name)
        for call in self.docker.calls:
            self.assertNotIn(sentinel.decode(), repr(call.argv) + repr(call.env), call.kind)
            self.assertNotIn(sentinel, call.stdin or b"", call.kind)
        for text in printed:
            self.assertNotIn(sentinel.decode(), text)

    def test_a_restore_s_report_stays_on_the_host(self) -> None:
        self.migrating_candidate()

        code, stdout, stderr = self.run_main(R3)

        self.assertEqual(code, 1)
        (restore,) = self.docker.of_kind("compose-run-restore")
        self.assertIn(str(SENTINEL_ROW_COUNT), restore.output or "", "the premise: it printed")
        self.assertIn("database=restored", stderr)
        self.assertEqual(self.failed_result()["database"], "restored")
        self.assert_nothing_leaked(stdout, stderr)

    def test_a_restore_killed_after_its_report_stays_on_the_host(self) -> None:
        """run() quotes stdout when stderr is empty, so a restore that printed its report
        and was then killed would carry the counts into rollback_error."""
        self.migrating_candidate()
        self.docker.restore_killed_after_report = True

        code, stdout, stderr = self.run_main(R3)

        self.assertEqual(code, 1)
        result = self.failed_result()
        self.assertEqual((result["rollback"], result["database"]), ("failed", "restore_failed"))
        self.assertIn("database=restore_failed", stderr)
        self.assert_nothing_leaked(stdout, stderr, json.dumps(result))
        # R5 keeps exactly one thing from that report: where the migrated database went.
        (safety,) = self.docker.safety_copies
        self.assertEqual(result.get("database_safety_copy"), safety)

    def test_a_failed_restore_stays_on_the_host(self) -> None:
        self.migrating_candidate()
        self.docker.fail_restore = True

        code, stdout, stderr = self.run_main(R3)

        self.assertEqual(code, 1)
        self.assertIn("database=restore_failed", stderr)
        self.assert_nothing_leaked(stdout, stderr)
        # Criterion 9: the refusal may be kept on the host, in rollback_error, and only there.
        self.assertIn("Refusing to restore", self.failed_result()["rollback_error"])
        self.assertNotIn("Refusing to restore", stdout + stderr)


class FakeDockerModelTests(unittest.TestCase):
    """The fake refuses what the real commands refuse. A guard nobody has seen fail is a
    guard nobody knows the state of."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.host = Host(Path(temp.name))
        self.docker = self.host.docker
        self.host.deploy(R1)
        self.manifest = self.host.current()
        self.compose_file = self.host.prod / "compose.yml"
        self.secrets = self.host.prod / "secrets.env"

    def compose(self, *args: str, input_file: Path | None = None) -> str:
        """What deploy.compose would run, sent straight to the fake."""
        env = {
            "PORTFOLIO_IMAGE": self.manifest["image"],
            "PORTFOLIO_PORT": "8083",
            "PORTFOLIO_ENVIRONMENT": "prod",
            "PORTFOLIO_SECRETS_ENV_FILE": str(self.secrets),
        }
        argv = [
            "docker", "compose", "--project-name", "portfolio-app-prod",
            "--file", str(self.compose_file), *args,
        ]
        return self.docker(argv, env=env, input_file=input_file)

    def restore(self, name: str) -> str:
        return self.compose(*RUN_ARGS, "python", "-m", "portfolio", "restore-backup", name)

    def stream(self, name: str, data: bytes) -> str:
        source = self.host.base / "stdin.bin"
        source.write_bytes(data)
        return self.compose(
            *RUN_ARGS, "python", "-c", deploy.STREAM_SCRIPT, "/app/backups", name,
            input_file=source,
        )

    def snapshot_name(self) -> str:
        """Stream a copy whose revision the fake knows, as a snapshot would be."""
        data = self.docker.database or b""
        self.docker.snapshot_revisions[data] = BASE_REVISION
        name = deploy.copy_name(datetime(2026, 10, 5, 12, 0, tzinfo=UTC))
        self.assertEqual(self.stream(name, data), f"ok {len(data)}")
        return name

    def test_restore_refuses_while_the_application_runs(self) -> None:
        name = self.snapshot_name()
        self.assertIsNotNone(self.docker.running)
        with self.assertRaisesRegex(deploy.DeploymentError, "the database is open"):
            self.restore(name)
        self.compose("stop", "app")
        self.assertIsNone(self.docker.running)
        self.assertIn("Restored", self.restore(name))

    def test_restore_refuses_a_name_not_in_the_volume_or_an_unknown_revision(self) -> None:
        self.compose("stop", "app")
        with self.assertRaisesRegex(deploy.DeploymentError, "is not a backup"):
            self.restore("portfolio-20261005T120000000000Z.sqlite3")
        name = self.snapshot_name()
        self.docker.snapshot_revisions[self.docker.backups[name]] = "0099_from_the_future"
        with self.assertRaisesRegex(deploy.DeploymentError, "does not know"):
            self.restore(name)

    def test_the_stream_refuses_a_name_already_taken_and_needs_stdin(self) -> None:
        name = self.snapshot_name()
        with self.assertRaisesRegex(deploy.DeploymentError, "already exists"):
            self.stream(name, b"other")
        with self.assertRaises(deploy.DeploymentError):
            self.compose(*RUN_ARGS, "python", "-c", deploy.STREAM_SCRIPT, "/app/backups", name)
        self.docker.unexpected.clear()

    def test_an_unclean_stop_leaves_a_wal_the_revision_read_recovers(self) -> None:
        self.docker.unclean_stop = True
        self.compose("stop", "app")
        self.assertTrue(self.docker.wal)
        read = self.compose(*RUN_ARGS, "python", "-c", deploy.REVISION_SCRIPT, DATABASE)
        self.assertEqual(read, BASE_REVISION)
        self.assertFalse(self.docker.wal)

    def test_an_image_refuses_a_revision_it_does_not_know(self) -> None:
        self.docker.revision = MIGRATED
        with self.assertRaisesRegex(deploy.DeploymentError, "Can't locate revision"):
            self.compose(*UP_ARGS)
        self.docker.knows[R1.image] = {MIGRATED}
        self.compose(*UP_ARGS)
        self.assertEqual(self.docker.refused_starts, [(R1.image, MIGRATED)])

    def test_unknown_run_commands_are_refused(self) -> None:
        for command in (
            ("python", "-c", "print(1)", DATABASE),
            ("python", "-m", "portfolio", "backup"),
        ):
            with self.subTest(command=command), self.assertRaises(deploy.DeploymentError):
                self.compose(*RUN_ARGS, *command)
        with self.assertRaises(deploy.DeploymentError):
            self.compose("run", "--rm", "app", "python", "-c", deploy.REVISION_SCRIPT, DATABASE)
        self.assertEqual(len(self.docker.unexpected), 3)
        self.docker.unexpected.clear()


if __name__ == "__main__":
    unittest.main()
