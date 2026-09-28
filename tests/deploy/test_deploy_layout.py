"""The deployment layout, the migration and the rollback target, against a fake Docker.

Each test builds a host under a temporary ``$HOME`` -- fresh, on the legacy
``~/portfolio-app-deploy`` attempts/ layout, or already on ``~/portfolio-app`` -- runs
``deploy.deploy`` with ``deploy.run`` replaced by ``deploy_harness.FakeDocker``, and asserts
on the files left behind and on every command Docker was asked to run.

docs/specs/018-deploy-layout.md's test plan is the checklist. This script runs against the
production Raspberry Pi on merge, and its first run migrates that host, so the failure
paths get as much attention as the happy one.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from deploy_harness import (
    LEGACY_ATTEMPT_IDS,
    POSIX,
    REAL_DEPLOYMENT_LOCK,
    SENTINEL_SECRET,
    Crash,
    Host,
    RecordingLock,
    Release,
    deploy,
    expected_compose_sh,
    legacy_write_json,
    mode,
    read_manifest,
    taken_by,
    tree,
    without_record,
)

R1, R2, R3, R4, R5 = (Release(n) for n in range(1, 6))
EVIDENCE = {"compose.yml", "request.json", "result.json", "database.sqlite3"}
BACKUP = {"compose.yml", "current.json", "database.sqlite3"}


class HostTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.host = Host(Path(temp.name))
        self.docker = self.host.docker
        self.addCleanup(self.assert_no_unexpected_commands)

    def assert_no_unexpected_commands(self) -> None:
        self.assertEqual(
            self.docker.unexpected, [], "deploy.py ran a command the fake does not know"
        )

    @property
    def prod(self) -> Path:
        return self.host.prod

    def assert_compose_context(self) -> None:
        """Every compose call names the project, the live secrets file and the port."""
        for call in self.docker.calls:
            if call.kind.startswith("compose"):
                self.assertEqual(call.project, "portfolio-app-prod", call.argv)
                self.assertEqual(call.env["PORTFOLIO_PORT"], "8083", call.argv)
                self.assertEqual(call.env["PORTFOLIO_ENVIRONMENT"], "prod", call.argv)
                self.assertEqual(
                    call.env["PORTFOLIO_SECRETS_ENV_FILE"], str(self.prod / "secrets.env")
                )

    def assert_fails(self, release: Release, **kwargs: Any) -> str:
        with self.assertRaises(deploy.DeploymentError) as caught:
            self.host.deploy(release, **kwargs)
        return str(caught.exception)

    def live_up(self) -> list[Any]:
        return self.docker.of_kind("compose-up")


class FreshHostTests(HostTestCase):
    def test_a_fresh_host_gets_the_live_layout_and_no_backup(self) -> None:
        result = self.host.deploy(R1)

        self.assertTrue(self.prod.is_dir())
        self.assertFalse(self.host.legacy.exists())
        self.assertEqual(
            sorted(path.name for path in self.prod.iterdir()),
            ["compose.sh", "compose.yml", "current.json", "last-attempt.json", "secrets.env"],
            "no backup/, incoming/, failed/ or attempts/ after a first deployment",
        )
        self.assertTrue((self.host.root / "deploy.lock").is_file())
        self.assertEqual((self.prod / "compose.yml").read_bytes(), R1.compose)
        self.assertEqual((self.prod / "secrets.env").read_bytes(), b"")
        current = self.host.current()
        self.assertEqual(current, result)
        self.assertEqual(current["image"], R1.image)
        self.assertEqual(current["status"], "healthy")
        self.assertEqual(current["layout"], 2)
        self.assertIs(current["backup"], False)
        self.assertEqual(read_manifest(self.prod / "last-attempt.json"), current)
        self.assertEqual(self.host.sqlite_files(), [])
        if POSIX:
            self.assertEqual(mode(self.prod / "secrets.env"), 0o600)

    def test_a_fresh_host_runs_pull_provenance_up_and_verify_only(self) -> None:
        self.host.deploy(R1)

        self.assertEqual(
            self.docker.kinds(), ["pull", "image-inspect", "compose-up", "compose-ps", "inspect"]
        )
        (up,) = self.live_up()
        self.assertEqual(Path(up.compose_file or ""), self.prod / "incoming" / "compose.yml")
        self.assertEqual(up.compose_bytes, R1.compose)
        self.assertEqual(up.image, R1.image)
        self.assert_compose_context()
        self.assertEqual(self.docker.running_image, R1.image)

    def test_the_lock_is_taken_in_the_new_root(self) -> None:
        self.host.deploy(R1)
        self.assertEqual(self.host.lock.taken, [self.host.root])

    def test_nothing_is_touched_when_validation_fails(self) -> None:
        for override in (
            {"image": "ghcr.io/emanuelturtula/portfolio:latest"},
            {"revision": "abc"},
            {"environment": "staging"},
            {"run_number": 0},
        ):
            with self.subTest(override=override):
                with self.assertRaises(deploy.DeploymentError):
                    self.host.deploy(R1, publish=False, **override)
                self.assertEqual(list(self.host.home.iterdir()), [])
                self.assertEqual(self.docker.calls, [])
                self.assertEqual(self.host.lock.taken, [])

    @unittest.skipUnless(POSIX, "Windows has no POSIX permission bits to assert")
    def test_every_file_and_directory_it_creates_is_private(self) -> None:
        self.host.deploy(R1)
        self.host.deploy(R2)
        self.docker.fail_up.add(R3.image)
        self.assert_fails(R3)
        for path in [self.host.root, *self.host.root.rglob("*")]:
            if path.is_dir():
                self.assertEqual(mode(path), 0o700, path)
            elif path.name != "database.sqlite3":
                # A snapshot keeps the mode docker cp gives it; the 0700 directories
                # above it are what keep it private.
                self.assertEqual(mode(path) & 0o077, 0, f"{path} is readable by others")
        self.assertEqual(len(self.host.sqlite_files()), 2)


class LegacyMigrationTests(HostTestCase):
    def assert_tombstone(self) -> None:
        self.assertTrue(self.host.legacy.is_file(), "the old root is a regular file")
        self.assertFalse(self.host.legacy.is_symlink())
        self.assertEqual(self.host.legacy.read_bytes(), deploy.TOMBSTONE.encode("utf-8"))

    def setUp(self) -> None:
        super().setUp()
        self.host.build_legacy()
        self.legacy_tree = tree(self.host.legacy)
        self.legacy_current = (self.host.legacy / "prod" / "current.json").read_bytes()
        self.lock_inode = (self.host.legacy / "deploy.lock").stat().st_ino

    def test_the_legacy_root_is_renamed_and_secrets_env_kept_byte_for_byte(self) -> None:
        self.host.deploy(R4)

        self.assert_tombstone()
        self.assertEqual(
            sorted(p.name for p in self.host.home.iterdir()),
            ["portfolio-app", "portfolio-app-deploy"],
        )
        (line,) = [x for x in self.host.stderr[-1].splitlines() if x.startswith("Migrated")]
        self.assertIn("portfolio-app-deploy", line)
        self.assertEqual((self.prod / "secrets.env").read_bytes(), SENTINEL_SECRET)
        if POSIX:
            self.assertEqual(mode(self.prod / "secrets.env"), 0o600)
        # The lock file moved with the directory: the same file, and only one of it.
        self.assertEqual((self.host.root / "deploy.lock").stat().st_ino, self.lock_inode)
        self.assertEqual(len(list(self.host.home.rglob("deploy.lock"))), 1)

    def test_the_backup_reads_the_live_container_through_the_rebased_compose_file(self) -> None:
        self.host.deploy(R4)

        self.assertEqual(
            self.docker.kinds(),
            [
                "pull",
                "image-inspect",
                "compose-ps",
                "inspect",
                "exec-backup",
                "cp",
                "exec-rm",
                "compose-up",
                "compose-ps",
                "inspect",
            ],
        )
        previous_ps = self.docker.calls[2]
        self.assertEqual(Path(previous_ps.compose_file or ""), self.host.legacy_compose_path())
        self.assertEqual(previous_ps.compose_bytes, R3.compose)
        self.assertEqual(previous_ps.image, R3.image)
        up = self.live_up()[0]
        self.assertEqual(Path(up.compose_file or ""), self.prod / "incoming" / "compose.yml")
        self.assert_compose_context()

    def test_success_replaces_attempts_with_one_backup_of_the_legacy_deployment(self) -> None:
        snapshot = self.docker.database
        self.host.deploy(R4)

        self.assertFalse((self.prod / "attempts").exists(), "the ten old copies must go")
        self.assertEqual(
            without_record(tree(self.prod / "backup")),
            {
                "compose.yml": R3.compose,
                "current.json": self.legacy_current,
                "database.sqlite3": snapshot,
            },
        )
        self.assertEqual(taken_by(self.prod / "backup"), self.host.current()["attempt"])
        self.assertEqual(self.host.sqlite_files(), [self.prod / "backup" / "database.sqlite3"])
        self.assertEqual((self.prod / "compose.yml").read_bytes(), R4.compose)
        self.assertEqual(self.host.current()["image"], R4.image)
        self.assertEqual(self.host.current()["layout"], 2)
        for leftover in ("incoming", "failed", "backup.new", "backup.old"):
            self.assertFalse((self.prod / leftover).exists(), leftover)

    def test_a_migrated_host_deploys_again_on_the_new_layout(self) -> None:
        self.host.deploy(R4)
        first_live = (self.prod / "current.json").read_bytes()
        self.docker.calls.clear()

        self.host.deploy(R5)

        self.assertEqual(Path(self.docker.calls[2].compose_file or ""), self.prod / "compose.yml")
        self.assertEqual(tree(self.prod / "backup")["current.json"], first_live)
        self.assertEqual(tree(self.prod / "backup")["compose.yml"], R4.compose)
        self.assertEqual(len(self.host.sqlite_files()), 1)

    def test_an_explicit_root_never_migrates(self) -> None:
        self.host.deploy(R4, migrate=False)

        self.assertEqual(tree(self.host.legacy), self.legacy_tree)
        self.assertEqual(self.host.lock.taken, [self.host.root])
        self.assertIs(self.host.current()["backup"], False)

    def test_main_migrates_the_default_roots(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        self.host.stage_kit(R4)
        self.docker.publish(R4)
        with (
            self.host.patched(),
            mock.patch.object(deploy, "default_roots", lambda: (self.host.root, self.host.legacy)),
            mock.patch.object(Path, "home", staticmethod(lambda: self.host.home)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = deploy.main(
                [
                    "--environment",
                    "prod",
                    "--image",
                    R4.image,
                    "--revision",
                    R4.revision,
                    "--version",
                    R4.version,
                    "--run-number",
                    str(R4.run_number),
                    "--source-run-url",
                    R4.source_run_url,
                ]
            )

        self.assertEqual(code, 0, stderr.getvalue())
        self.assert_tombstone()
        self.assertEqual(
            stderr.getvalue(),
            "Migrated the deployment root from ~/portfolio-app-deploy to ~/portfolio-app.\n",
        )
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed, self.host.current())
        # stdout reaches the public Actions log through ssh: no host path in it.
        self.assertNotIn(str(self.host.home), stdout.getvalue())
        self.assertNotIn(self.host.home.as_posix(), stdout.getvalue())

    def test_default_roots_are_under_the_home_directory(self) -> None:
        with mock.patch.object(Path, "home", staticmethod(lambda: self.host.home)):
            root, legacy = deploy.default_roots()
        self.assertEqual(root, self.host.home / "portfolio-app")
        self.assertEqual(legacy, self.host.home / "portfolio-app-deploy")


class MigrationWithoutSnapshotTests(HostTestCase):
    """A migrating deployment that cannot snapshot the live database.

    The legacy attempts hold the only copies of the database on the host. Deleting them
    without a snapshot to replace them would leave none, in exactly the case where this
    deployment is the fix for an unhealthy service. So backup/ is seeded from the newest
    legacy attempt that has a database, and only then is attempts/ deleted.
    """

    def setUp(self) -> None:
        super().setUp()
        self.host.build_legacy()
        self.attempts = self.host.legacy / "prod" / "attempts"
        self.docker.containers[self.docker.running or ""].health = "unhealthy"

    def attempt(self, index: int) -> Path:
        return self.attempts / LEGACY_ATTEMPT_IDS[index]

    def deploy_capturing_stderr(self) -> str:
        """R4's stderr, without the one line every migration prints."""
        self.host.deploy(R4)
        lines = self.host.stderr[-1].splitlines()
        return "".join(
            line + "\n" for line in lines if not line.startswith("Migrated the deployment root")
        )

    def test_backup_is_seeded_from_the_newest_legacy_attempt_with_a_database(self) -> None:
        # attempts/<id>/database.sqlite3 was taken from the deployment that attempt
        # replaced, the one its previous.json names, which was made in the attempt before.
        newest = tree(self.attempt(-1))
        its_deployment = tree(self.attempt(-2))

        self.assertEqual(self.deploy_capturing_stderr(), "")

        self.assertEqual(self.docker.snapshots, [], "the premise: no snapshot was taken")
        self.assertEqual(
            without_record(tree(self.prod / "backup")),
            {
                "compose.yml": its_deployment["compose.yml"],
                "current.json": newest["previous.json"],
                "database.sqlite3": newest["database.sqlite3"],
            },
        )
        self.assertEqual(taken_by(self.prod / "backup"), LEGACY_ATTEMPT_IDS[-1])
        self.assertEqual(its_deployment["compose.yml"], R2.compose)
        self.assertFalse((self.prod / "attempts").exists())
        self.assertEqual(self.host.sqlite_files(), [self.prod / "backup" / "database.sqlite3"])

    def test_an_attempt_without_a_database_is_skipped(self) -> None:
        (self.attempt(-1) / "database.sqlite3").unlink()
        second = tree(self.attempt(-2))

        self.host.deploy(R4)

        backup = tree(self.prod / "backup")
        self.assertEqual(backup["database.sqlite3"], second["database.sqlite3"])
        self.assertEqual(backup["compose.yml"], R1.compose)
        self.assertEqual(backup["current.json"], second["previous.json"])
        self.assertEqual(len(self.host.sqlite_files()), 1)

    def test_a_failed_attempt_still_names_the_deployment_its_database_came_from(self) -> None:
        # The old script took its backup before `up`, so a failed attempt holds a
        # database too, taken from the same deployment its previous.json names.
        newest = self.attempt(-1)
        result = read_manifest(newest / "result.json")
        legacy_write_json(newest / "result.json", dict(result, status="failed"))
        previous = (newest / "previous.json").read_bytes()

        self.host.deploy(R4)

        self.assertEqual(tree(self.prod / "backup")["current.json"], previous)

    def test_a_missing_compose_file_is_omitted_and_the_reason_recorded(self) -> None:
        (self.attempt(-2) / "compose.yml").unlink()
        previous = read_manifest(self.attempt(-1) / "previous.json")
        database = (self.attempt(-1) / "database.sqlite3").read_bytes()

        self.host.deploy(R4)

        backup = without_record(tree(self.prod / "backup"))
        self.assertEqual(set(backup), {"current.json", "database.sqlite3"})
        self.assertEqual(backup["database.sqlite3"], database)
        recorded = json.loads(backup["current.json"])
        note = recorded.pop("backup_note")
        self.assertIn("compose file", note)
        self.assertEqual(recorded, previous)

    def test_an_unreadable_previous_json_leaves_only_a_note(self) -> None:
        (self.attempt(-1) / "previous.json").write_bytes(b"{ torn")
        database = (self.attempt(-1) / "database.sqlite3").read_bytes()

        self.host.deploy(R4)

        backup = without_record(tree(self.prod / "backup"))
        self.assertEqual(set(backup), {"current.json", "database.sqlite3"})
        self.assertEqual(backup["database.sqlite3"], database)
        self.assertEqual(set(json.loads(backup["current.json"])), {"backup_note"})

    def test_with_no_legacy_database_it_says_so_without_naming_anything(self) -> None:
        for path in self.attempts.rglob("database.sqlite3"):
            path.unlink()

        with mock.patch.object(Path, "home", staticmethod(lambda: self.host.home)):
            stderr = self.deploy_capturing_stderr()

        self.assertFalse((self.prod / "attempts").exists())
        self.assertFalse((self.prod / "backup").exists())
        self.assertEqual(self.host.sqlite_files(), [])
        lines = stderr.splitlines()
        self.assertEqual(len(lines), 1, self.host.stderr[-1])
        self.assertIn("No database backup", lines[0])
        for revealing in (str(self.host.home), self.host.home.as_posix(), "~/", "sqlite3"):
            self.assertNotIn(revealing, lines[0])

    def test_an_existing_backup_database_is_kept_and_attempts_still_go(self) -> None:
        # A crash after the swap but before attempts/ was deleted leaves both.
        backup = self.host.legacy / "prod" / "backup"
        backup.mkdir()
        (backup / "database.sqlite3").write_bytes(b"backup from an interrupted migration\n")
        (backup / "compose.yml").write_bytes(R3.compose)
        (backup / "current.json").write_bytes(
            (self.host.legacy / "prod" / "current.json").read_bytes()
        )
        kept = tree(backup)

        self.host.deploy(R4)

        self.assertEqual(tree(self.prod / "backup"), kept)
        self.assertFalse((self.prod / "attempts").exists())
        self.assertEqual(len(self.host.sqlite_files()), 1)


class NewLayoutTests(HostTestCase):
    def test_a_second_deploy_backs_up_the_first(self) -> None:
        first = self.host.deploy(R1)
        first_bytes = (self.prod / "current.json").read_bytes()
        snapshot = self.docker.database
        self.docker.calls.clear()

        self.host.deploy(R2)

        self.assertEqual(
            without_record(tree(self.prod / "backup")),
            {"compose.yml": R1.compose, "current.json": first_bytes, "database.sqlite3": snapshot},
        )
        self.assertEqual(taken_by(self.prod / "backup"), self.host.current()["attempt"])
        self.assertEqual(json.loads(first_bytes), first)
        self.assertEqual((self.prod / "compose.yml").read_bytes(), R2.compose)
        self.assertEqual(self.host.current()["image"], R2.image)
        self.assertIs(self.host.current()["backup"], True)
        self.assertEqual(
            self.docker.kinds(),
            [
                "pull",
                "image-inspect",
                "compose-ps",
                "inspect",
                "exec-backup",
                "cp",
                "exec-rm",
                "compose-up",
                "compose-ps",
                "inspect",
            ],
        )
        previous_ps = self.docker.calls[2]
        self.assertEqual(Path(previous_ps.compose_file or ""), self.prod / "compose.yml")
        self.assertEqual((previous_ps.image, previous_ps.compose_bytes), (R1.image, R1.compose))
        self.assert_compose_context()

    def test_a_third_deploy_replaces_the_backup_with_the_new_snapshot(self) -> None:
        self.host.deploy(R1)
        self.host.deploy(R2)
        old_snapshot = tree(self.prod / "backup")["database.sqlite3"]
        second_bytes = (self.prod / "current.json").read_bytes()
        new_snapshot = self.docker.database

        self.host.deploy(R3)

        backup = without_record(tree(self.prod / "backup"))
        self.assertEqual(
            backup,
            {
                "compose.yml": R2.compose,
                "current.json": second_bytes,
                "database.sqlite3": new_snapshot,
            },
        )
        self.assertEqual(taken_by(self.prod / "backup"), self.host.current()["attempt"])
        self.assertNotEqual(backup["database.sqlite3"], old_snapshot)
        self.assertEqual(self.docker.snapshots[-1], new_snapshot)
        for leftover in ("incoming", "failed", "backup.new", "backup.old", "attempts"):
            self.assertFalse((self.prod / leftover).exists(), leftover)

    def test_the_rollback_target_comes_from_the_layout_not_a_stored_path(self) -> None:
        # A decoy that looks exactly like a legacy compose path, stored in a layout-2
        # manifest. Following it would roll back to the wrong file.
        self.host.deploy(R1)
        self.host.deploy(R2)
        decoy = self.prod / "attempts" / LEGACY_ATTEMPT_IDS[0] / "compose.yml"
        decoy.parent.mkdir(parents=True)
        decoy.write_bytes(b"# decoy: not the live deployment\n")
        current = self.host.current()
        current["compose"] = str(decoy)
        legacy_write_json(self.prod / "current.json", current)
        self.docker.fail_up.add(R3.image)

        self.assert_fails(R3)

        rollback = self.live_up()[-1]
        self.assertEqual(Path(rollback.compose_file or ""), self.prod / "compose.yml")
        self.assertEqual(rollback.compose_bytes, R2.compose)

    def test_a_deployment_that_took_no_snapshot_keeps_the_backup(self) -> None:
        for why in ("unhealthy", "absent", "mismatched"):
            with self.subTest(why=why):
                self.setUp()
                self.host.deploy(R1)
                self.host.deploy(R2)
                backup_before = tree(self.prod / "backup")
                if why == "unhealthy":
                    self.docker.containers[self.docker.running or ""].health = "unhealthy"
                elif why == "absent":
                    self.docker.database = None
                else:
                    self.docker.containers[self.docker.running or ""].image = R1.image

                self.host.deploy(R3)

                self.assertEqual(tree(self.prod / "backup"), backup_before)
                self.assertIs(self.host.current()["backup"], False)
                self.assertEqual(self.host.current()["image"], R3.image)
                self.assertEqual(len(self.host.sqlite_files()), 1)

    def test_a_stale_incoming_directory_is_cleared_first(self) -> None:
        self.host.deploy(R1)
        stale = self.prod / "incoming"
        stale.mkdir()
        (stale / "database.sqlite3").write_bytes(b"stale snapshot from a crashed attempt\n")
        (stale / "leftover.txt").write_bytes(b"stale\n")

        self.host.deploy(R2)

        self.assertFalse(stale.exists())
        self.assertNotIn(b"stale", b"".join(tree(self.host.root).values()))
        self.assertEqual(len(self.host.sqlite_files()), 1)


class BothRootsTests(HostTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.host.build_legacy()
        self.host.write_secrets(self.prod).write_bytes(b"OTHER=root\n")
        self.before = tree(self.host.home)

    def test_both_roots_are_refused_before_anything_runs(self) -> None:
        message = self.assert_fails(R4)

        self.assertIn("portfolio-app-deploy", message)
        self.assertIn("portfolio-app", message.replace("portfolio-app-deploy", ""))
        self.assertEqual(tree(self.host.home), self.before, "nothing renamed or written")
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.host.lock.taken, [], "refused before any lock is taken")

    def test_the_refusal_names_both_roots_relative_to_home(self) -> None:
        with mock.patch.object(Path, "home", staticmethod(lambda: self.host.home)):
            message = self.assert_fails(R4)
        self.assertIn("~/portfolio-app-deploy", message)
        self.assertRegex(message, r"~/portfolio-app(?![\w-])")
        self.assertNotIn(str(self.host.home), message)
        self.assertNotIn(self.host.home.as_posix(), message)

    def test_settle_root_refuses_both_under_the_lock_too(self) -> None:
        with self.assertRaises(deploy.DeploymentError):
            deploy.settle_root(self.host.root, self.host.legacy)
        self.assertEqual(tree(self.host.home), self.before)


class LockTests(HostTestCase):
    def test_the_lock_is_taken_in_the_legacy_root_and_the_rename_happens_under_it(self) -> None:
        self.host.build_legacy()
        seen: list[list[str]] = []

        def before(index: int, directory: Path) -> None:
            seen.append(sorted(p.name for p in self.host.home.iterdir()))

        self.host.lock = RecordingLock(before=before)
        self.host.deploy(R4)

        self.assertEqual(self.host.lock.taken, [self.host.legacy])
        self.assertEqual(seen, [["portfolio-app-deploy"]], "the rename must follow the lock")
        self.assertEqual(
            sorted(p.name for p in self.host.home.iterdir()),
            ["portfolio-app", "portfolio-app-deploy"],
        )
        self.assertTrue(self.host.legacy.is_file(), "the old root is a tombstone file")
        self.assertEqual(self.host.legacy.read_bytes(), deploy.TOMBSTONE.encode("utf-8"))

    def test_resolution_before_the_lock(self) -> None:
        root, legacy = self.host.root, self.host.legacy
        self.assertEqual(deploy.lock_directory(root, legacy), root, "neither exists")
        self.assertEqual(deploy.lock_directory(root, None), root)
        legacy.mkdir()
        self.assertEqual(deploy.lock_directory(root, legacy), legacy)
        self.assertEqual(deploy.lock_directory(root, None), root, "an explicit root ignores it")
        root.mkdir()
        with self.assertRaises(deploy.DeploymentError):
            deploy.lock_directory(root, legacy)
        legacy.rmdir()
        self.assertEqual(deploy.lock_directory(root, legacy), root)
        # A file at the old path is a tombstone, never a root: no refusal, nothing moved.
        legacy.write_bytes(b"a tombstone\n")
        self.assertEqual(deploy.lock_directory(root, legacy), root)
        root.rmdir()
        self.assertEqual(deploy.lock_directory(root, legacy), root)
        self.assertEqual(sorted(p.name for p in self.host.home.iterdir()), ["portfolio-app-deploy"])

    def test_a_waiter_on_the_old_path_re_resolves_to_the_new_root(self) -> None:
        self.host.build_legacy()
        self.host.deploy(R4)
        after_migration = tree(self.host.home)

        # A second process that chose the legacy root and then waited on its lock file
        # (the same file, now under the new name) re-resolves once it holds the lock.
        self.assertEqual(deploy.settle_root(self.host.root, self.host.legacy), self.host.root)
        self.assertEqual(deploy.lock_directory(self.host.root, self.host.legacy), self.host.root)
        self.assertEqual(tree(self.host.home), after_migration, "it renames nothing")
        self.assertTrue(self.host.legacy.is_file(), "the old root is a tombstone file")
        self.assertEqual(self.host.legacy.read_bytes(), deploy.TOMBSTONE.encode("utf-8"))

    def test_a_root_moved_before_its_lock_opened_is_retried_in_the_new_root(self) -> None:
        self.host.build_legacy()

        def before(index: int, directory: Path) -> None:
            if index == 0:
                # Another deployment migrates the host between this one choosing the legacy
                # root and opening its lock file.
                self.assertEqual(directory, self.host.legacy)
                os.rename(self.host.legacy, self.host.root)

        self.host.lock = RecordingLock(before=before)
        self.host.deploy(R4)

        self.assertEqual(self.host.lock.taken, [self.host.legacy, self.host.root])
        # The other deployment died before writing the tombstone; this one writes it,
        # because the legacy attempts/ shows the host was migrated.
        self.assertTrue(self.host.legacy.is_file(), "the old root is a tombstone file")
        self.assertEqual(self.host.legacy.read_bytes(), deploy.TOMBSTONE.encode("utf-8"))
        self.assertEqual(self.host.current()["image"], R4.image)
        self.assertEqual(tree(self.prod / "backup")["compose.yml"], R3.compose)

    def test_a_root_that_keeps_moving_gives_up_without_running_anything(self) -> None:
        self.host.build_legacy()

        def before(index: int, directory: Path) -> None:
            raise deploy.RootMoved("moved again")

        self.host.lock = RecordingLock(before=before)
        with self.assertRaises(deploy.DeploymentError):
            self.host.deploy(R4)
        self.assertEqual(len(self.host.lock.taken), deploy.LOCK_ATTEMPTS)
        self.assertLessEqual(deploy.LOCK_ATTEMPTS, 5)
        self.assertEqual(self.docker.calls, [])

    @unittest.skipUnless(POSIX, "fcntl.flock exists only on POSIX; Linux CI runs this")
    def test_the_real_lock_is_held_across_the_rename(self) -> None:
        import fcntl

        legacy, root = self.host.legacy, self.host.root
        legacy.mkdir()
        with REAL_DEPLOYMENT_LOCK(legacy):
            os.rename(legacy, root)
            with open(root / "deploy.lock", "a", encoding="utf-8") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(deploy.DeploymentError) as caught:
                with REAL_DEPLOYMENT_LOCK(root, timeout_seconds=0.3):
                    self.fail("a second holder got the lock")
            self.assertNotIsInstance(caught.exception, deploy.RootMoved)
            self.assertIn("busy", str(caught.exception))
        with REAL_DEPLOYMENT_LOCK(root, timeout_seconds=0.3):
            pass

    @unittest.skipUnless(POSIX, "fcntl.flock exists only on POSIX; Linux CI runs this")
    def test_the_real_lock_reports_a_vanished_directory_as_moved(self) -> None:
        gone = self.host.home / "portfolio-app-deploy"
        with self.assertRaises(deploy.RootMoved):
            with REAL_DEPLOYMENT_LOCK(gone):
                self.fail("locked a directory that does not exist")
        self.assertFalse(gone.exists(), "the lock must never recreate a moved root")


class FailureTests(HostTestCase):
    def prod_without_evidence(self) -> dict[str, bytes]:
        return {
            name: data
            for name, data in tree(self.prod).items()
            if not name.startswith("failed/") and name != "last-attempt.json"
        }

    def assert_evidence(self, release: Release, *, snapshot: bytes | None) -> dict[str, Any]:
        failed = tree(self.prod / "failed")
        expected = (
            EVIDENCE | {"snapshot.json"}
            if snapshot is not None
            else EVIDENCE - {"database.sqlite3"}
        )
        self.assertEqual(set(failed), expected)
        self.assertEqual(failed["compose.yml"], release.compose)
        request = json.loads(failed["request.json"])
        self.assertEqual((request["image"], request["status"]), (release.image, "pending"))
        result: dict[str, Any] = json.loads(failed["result.json"])
        self.assertEqual((result["image"], result["status"]), (release.image, "failed"))
        self.assertTrue(result["error"])
        if snapshot is not None:
            self.assertEqual(failed["database.sqlite3"], snapshot)
        last = read_manifest(self.prod / "last-attempt.json")
        self.assertEqual(last, result)
        self.assertFalse((self.prod / "incoming").exists())
        return result

    def test_a_failure_on_the_new_layout_rolls_back_to_prod_compose_yml(self) -> None:
        self.host.deploy(R1)
        self.host.deploy(R2)
        before = self.prod_without_evidence()
        snapshot = self.docker.database
        self.docker.fail_up.add(R3.image)

        message = self.assert_fails(R3)

        candidate, rollback = self.live_up()[-2:]
        self.assertEqual(Path(candidate.compose_file or ""), self.prod / "incoming" / "compose.yml")
        self.assertEqual(Path(rollback.compose_file or ""), self.prod / "compose.yml")
        self.assertEqual((rollback.image, rollback.compose_bytes), (R2.image, R2.compose))
        self.assertEqual(self.docker.running_image, R2.image)
        self.assertEqual(self.prod_without_evidence(), before, "compose.yml, current.json, backup/")
        result = self.assert_evidence(R3, snapshot=snapshot)
        self.assertEqual(result["rollback"], "healthy")
        self.assertIn("rollback=healthy", message)
        self.assertIn("failed", message)
        self.assert_compose_context()

    def test_a_failure_on_the_legacy_layout_rolls_back_to_the_rebased_attempt(self) -> None:
        self.host.build_legacy()
        legacy_prod = tree(self.host.legacy / "prod")
        snapshot = self.docker.database
        self.docker.fail_up.add(R4.image)

        self.assert_fails(R4)

        rollback = self.live_up()[-1]
        self.assertEqual(Path(rollback.compose_file or ""), self.host.legacy_compose_path())
        self.assertEqual((rollback.image, rollback.compose_bytes), (R3.image, R3.compose))
        self.assertEqual(self.docker.running_image, R3.image)
        after = self.prod_without_evidence()
        # R4: the one addition, compose.sh for the live legacy deployment, so a failed first
        # migration still leaves the owner a working command.
        legacy_live = self.host.legacy_compose_path().relative_to(self.prod).as_posix()
        self.assertEqual(after.pop("compose.sh"), expected_compose_sh(R3, legacy_live))
        self.assertEqual(after, legacy_prod, "attempts/ and current.json untouched")
        self.assertEqual(self.assert_evidence(R4, snapshot=snapshot)["rollback"], "healthy")

    def test_a_legacy_host_that_failed_once_migrates_on_its_next_success(self) -> None:
        self.host.build_legacy()
        legacy_current = (self.host.legacy / "prod" / "current.json").read_bytes()
        self.docker.fail_up.add(R4.image)
        self.assert_fails(R4)
        snapshot = self.docker.database

        self.host.deploy(R5)

        self.assertFalse((self.prod / "attempts").exists())
        self.assertFalse((self.prod / "failed").exists())
        self.assertEqual(
            without_record(tree(self.prod / "backup")),
            {
                "compose.yml": R3.compose,
                "current.json": legacy_current,
                "database.sqlite3": snapshot,
            },
        )
        self.assertEqual(len(self.host.sqlite_files()), 1)

    def test_a_failure_on_a_fresh_host_brings_the_candidate_down(self) -> None:
        self.docker.fail_up.add(R1.image)

        message = self.assert_fails(R1)

        (down,) = self.docker.of_kind("compose-down")
        self.assertEqual(Path(down.compose_file or ""), self.prod / "incoming" / "compose.yml")
        self.assertIsNone(self.docker.running)
        self.assertEqual(
            self.assert_evidence(R1, snapshot=None)["rollback"], "no_previous_deployment"
        )
        for absent in ("compose.yml", "current.json", "compose.sh", "backup"):
            self.assertFalse((self.prod / absent).exists(), absent)
        self.assertIn("no_previous_deployment", message)

    def test_a_digest_mismatch_after_up_rolls_back(self) -> None:
        self.host.deploy(R1)
        before = self.prod_without_evidence()
        self.docker.substitute[R2.image] = Release(9).image

        self.assert_fails(R2)

        self.assertEqual(self.docker.running_image, R1.image)
        self.assertEqual(self.prod_without_evidence(), before)
        result = self.assert_evidence(R2, snapshot=self.docker.snapshots[-1])
        self.assertIn("does not match", result["error"])

    def test_an_unhealthy_candidate_rolls_back(self) -> None:
        self.host.deploy(R1)
        self.docker.unhealthy.add(R2.image)

        self.assert_fails(R2)

        self.assertEqual(self.docker.running_image, R1.image)
        self.assertEqual(self.host.current()["image"], R1.image)

    def test_the_next_failure_replaces_failed(self) -> None:
        self.host.deploy(R1)
        self.docker.fail_up.update({R2.image, R3.image})
        self.assert_fails(R2)
        first = (self.prod / "failed" / "database.sqlite3").read_bytes()
        # The second failure cannot snapshot. Its evidence replaces the first failure's,
        # and the first failure's snapshot, the only copy, is carried into it.
        self.docker.containers[self.docker.running or ""].health = "unhealthy"

        self.assert_fails(R3)

        self.assert_evidence(R3, snapshot=first)
        self.assertEqual(self.host.sqlite_files(), [self.prod / "failed" / "database.sqlite3"])

    def test_a_success_removes_the_failure_evidence(self) -> None:
        self.host.deploy(R1)
        self.docker.fail_up.add(R2.image)
        self.assert_fails(R2)

        self.host.deploy(R3)

        self.assertFalse((self.prod / "failed").exists())
        self.assertEqual(len(self.host.sqlite_files()), 1)

    def test_a_rollback_that_fails_is_recorded_and_nothing_live_changes(self) -> None:
        self.host.deploy(R1)
        before = self.prod_without_evidence()
        self.docker.fail_up.update({R2.image, R1.image})

        message = self.assert_fails(R2)

        result = self.assert_evidence(R2, snapshot=self.docker.snapshots[-1])
        self.assertEqual(result["rollback"], "failed")
        self.assertTrue(result["rollback_error"])
        self.assertIn("rollback=failed", message)
        self.assertEqual(self.prod_without_evidence(), before)

    def test_a_backup_failure_stops_before_the_service_is_replaced(self) -> None:
        self.host.deploy(R1)
        before = self.prod_without_evidence()
        self.docker.fail_backup = True

        message = self.assert_fails(R2)

        self.assertNotIn("compose-up", self.docker.kinds()[5:])
        self.assertEqual(self.docker.running_image, R1.image)
        self.assertEqual(self.prod_without_evidence(), before)
        self.assertIn("backup failed", message)
        result = read_manifest(self.prod / "failed" / "result.json")
        self.assertEqual((result["stage"], result["status"]), ("backup", "failed"))


class MissingRollbackTargetTests(HostTestCase):
    def test_a_missing_live_compose_file_is_refused_before_pull(self) -> None:
        self.host.deploy(R1)
        (self.prod / "compose.yml").unlink()
        before = tree(self.host.home)
        self.docker.calls.clear()

        message = self.assert_fails(R2)

        self.assertIn("missing", message)
        self.assertEqual(self.docker.calls, [], "refused before pull")
        self.assertEqual(tree(self.host.home), before)

    def test_a_missing_legacy_attempt_is_refused_before_pull(self) -> None:
        self.host.build_legacy()
        (self.host.legacy / "prod" / "attempts" / LEGACY_ATTEMPT_IDS[-1] / "compose.yml").unlink()
        before = tree(self.host.legacy)
        running = self.docker.running

        message = self.assert_fails(R4)

        self.assertIn("missing", message)
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.docker.running, running)
        self.assertEqual(tree(self.host.root), before, "at most renamed, never modified")

    def test_a_legacy_manifest_naming_an_unrecognised_compose_path_is_refused(self) -> None:
        good = LEGACY_ATTEMPT_IDS[-1]
        cases = {
            "id in another format": ("prod", "attempts", "attempt-3", "compose.yml"),
            "another file name": ("prod", "attempts", good, "other.yml"),
            "not under attempts": ("prod", "elsewhere", good, "compose.yml"),
            "another environment": ("staging", "attempts", good, "compose.yml"),
            "a parent reference": ("prod", "attempts", "..", "compose.yml"),
        }
        for case, parts in [*cases.items(), ("no compose key", ())]:
            with self.subTest(case=case):
                self.setUp()
                self.host.build_legacy()
                prod = self.host.legacy / "prod"
                manifest = read_manifest(prod / "current.json")
                if parts:
                    target = self.host.legacy.joinpath(*parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(R3.compose)
                    manifest["compose"] = str(target)
                else:
                    del manifest["compose"]
                legacy_write_json(prod / "current.json", manifest)

                self.assert_fails(R4)

                self.assertEqual(self.docker.calls, [])
                self.assertFalse((self.prod / "incoming").exists())


class CarryForwardTests(HostTestCase):
    """A snapshot is carried forward, never dropped, by a deployment that takes none.

    The reviewer's case: a young host has had one success and has no backup/. A failed
    deployment's rollback comes up unhealthy, so the only database copy on the host is the
    snapshot in failed/. The next deployment cannot snapshot the unhealthy container.
    """

    def setUp(self) -> None:
        super().setUp()
        self.host.deploy(R1)
        self.docker.fail_up.add(R2.image)
        self.docker.unhealthy.add(R1.image)  # the rollback's container
        message = self.assert_fails(R2)
        self.assertIn("rollback=failed", message)
        self.assertFalse((self.prod / "backup").exists(), "the premise: a young host")
        self.only_copy = (self.prod / "failed" / "database.sqlite3").read_bytes()
        self.assertEqual(self.host.sqlite_files(), [self.prod / "failed" / "database.sqlite3"])
        self.first_live = (self.prod / "current.json").read_bytes()
        self.snapshots = len(self.docker.snapshots)

    def assert_no_snapshot_was_taken(self) -> None:
        self.assertEqual(len(self.docker.snapshots), self.snapshots)

    def test_a_success_that_cannot_snapshot_makes_the_failed_snapshot_the_backup(self) -> None:
        failed_attempt = read_manifest(self.prod / "failed" / "request.json")["attempt"]

        self.host.deploy(R3)

        self.assert_no_snapshot_was_taken()
        current = self.host.current()
        self.assertIs(current["backup"], False, "no snapshot of its own")
        self.assertEqual(current["backup_carried_from"], failed_attempt)
        self.assertEqual(
            without_record(tree(self.prod / "backup")),
            {
                "compose.yml": R1.compose,
                "current.json": self.first_live,
                "database.sqlite3": self.only_copy,
            },
        )
        self.assertEqual(taken_by(self.prod / "backup"), failed_attempt)
        self.assertFalse((self.prod / "failed").exists())
        self.assertEqual(len(self.host.sqlite_files()), 1)

    def test_a_failure_that_cannot_snapshot_carries_it_into_its_own_evidence(self) -> None:
        self.docker.fail_up.add(R3.image)
        self.assert_fails(R3)

        self.assert_no_snapshot_was_taken()
        request = read_manifest(self.prod / "failed" / "request.json")
        self.assertEqual(request["image"], R3.image)
        self.assertEqual((self.prod / "failed" / "database.sqlite3").read_bytes(), self.only_copy)
        self.assertEqual(len(self.host.sqlite_files()), 1)

        self.host.deploy(R4)

        self.assertEqual(tree(self.prod / "backup")["database.sqlite3"], self.only_copy)
        self.assertEqual(len(self.host.sqlite_files()), 1)

    def test_a_success_that_takes_a_snapshot_supersedes_the_failed_one(self) -> None:
        self.docker.unhealthy.discard(R1.image)
        self.docker.containers[self.docker.running or ""].health = "healthy"

        self.host.deploy(R3)

        self.assertEqual(len(self.docker.snapshots), self.snapshots + 1)
        self.assertEqual(tree(self.prod / "backup")["database.sqlite3"], self.docker.snapshots[-1])
        self.assertEqual(len(self.host.sqlite_files()), 1)
        current = self.host.current()
        self.assertIs(current["backup"], True)
        self.assertNotIn("backup_carried_from", current, "the record claims a carry it never made")

    def test_a_failure_that_takes_a_snapshot_keeps_its_own(self) -> None:
        self.docker.unhealthy.discard(R1.image)
        self.docker.containers[self.docker.running or ""].health = "healthy"
        self.docker.fail_up.add(R3.image)

        self.assert_fails(R3)

        result = read_manifest(self.prod / "failed" / "result.json")
        self.assertIs(result["backup"], True)
        self.assertNotIn("backup_carried_from", result)
        own = self.docker.snapshots[-1]
        self.assertEqual((self.prod / "failed" / "database.sqlite3").read_bytes(), own)


class NewestCopyAfterAPromotedBackupTests(HostTestCase):
    """The reviewer's second-pass chain: carry by attempt time, not by what backup/ names.

    X dies after building a complete backup.new of the live deployment (DATA-A). F
    promotes it, takes no snapshot, fails and rolls back. The application writes DATA-B.
    G snapshots DATA-B and fails. H takes no snapshot and succeeds. backup/ still names
    the live deployment, which the first carry rule read as "the backup is newer" -- and
    DATA-B, the newest copy on the host, was deleted with failed/.
    """

    def test_the_failed_snapshot_newer_than_the_backup_becomes_the_backup(self) -> None:
        host, docker = self.host, self.docker
        host.deploy(Release(1))
        host.deploy(R2)
        docker.database = b"DATA-A, before X\n"

        real_write = deploy.write_atomic

        def dies_before_the_live_pair(path: Path, data: bytes, **kwargs: Any) -> None:
            if Path(path) == self.prod / "compose.yml":
                raise Crash("X killed after rotation step 1")
            real_write(path, data, **kwargs)

        with mock.patch.object(deploy, "write_atomic", dies_before_the_live_pair):
            with self.assertRaises(Crash):
                host.deploy(R3)  # X
        self.assertEqual(tree(self.prod / "backup.new")["database.sqlite3"], b"DATA-A, before X\n")

        docker.fail_up.add(R4.image)
        self.assert_fails(R4)  # F: X's candidate runs, so no snapshot; rolls back to R2
        self.assertEqual(tree(self.prod / "backup")["database.sqlite3"], b"DATA-A, before X\n")

        docker.database = b"DATA-B, written after F\n"
        docker.fail_up.add(R5.image)
        self.assert_fails(R5)  # G: snapshots DATA-B, fails
        self.assertEqual(
            (self.prod / "failed" / "database.sqlite3").read_bytes(), b"DATA-B, written after F\n"
        )

        docker.containers[docker.running or ""].health = "unhealthy"
        host.deploy(Release(6))  # H: no snapshot of its own

        self.assertEqual(
            tree(self.prod / "backup")["database.sqlite3"],
            b"DATA-B, written after F\n",
            "the newest copy on the host was deleted",
        )
        self.assertEqual(len(host.sqlite_files()), 1)


class InterruptedAttemptTests(HostTestCase):
    """A stale incoming/ holding a snapshot is an attempt a crash interrupted mid-``up``."""

    def setUp(self) -> None:
        super().setUp()
        self.host.deploy(R1)
        self.host.deploy(R2)
        self.backup = tree(self.prod / "backup")
        incoming = self.prod / "incoming"
        incoming.mkdir()
        (incoming / "compose.yml").write_bytes(R3.compose)
        (incoming / "request.json").write_bytes(b'{"image": "interrupted"}\n')
        self.interrupted = b"snapshot taken just before the interrupted candidate ran\n"
        (incoming / "database.sqlite3").write_bytes(self.interrupted)
        # The interrupted candidate is what runs now, so nothing matches current.json.
        self.docker.start(R3.image)
        self.snapshots = len(self.docker.snapshots)

    def test_its_snapshot_becomes_the_backup_of_the_next_success(self) -> None:
        self.host.deploy(R4)

        self.assertEqual(len(self.docker.snapshots), self.snapshots, "the premise: no snapshot")
        self.assertEqual(tree(self.prod / "backup")["database.sqlite3"], self.interrupted)
        for leftover in ("incoming", "failed"):
            self.assertFalse((self.prod / leftover).exists(), leftover)
        self.assertEqual(len(self.host.sqlite_files()), 1)

    def test_its_snapshot_survives_a_failure_too(self) -> None:
        self.docker.fail_up.add(R4.image)
        self.assert_fails(R4)

        self.assertEqual((self.prod / "failed" / "database.sqlite3").read_bytes(), self.interrupted)
        self.assertEqual(tree(self.prod / "backup"), self.backup)
        self.assertEqual(len(self.host.sqlite_files()), 2)

    def test_a_stale_incoming_without_a_snapshot_is_simply_cleared(self) -> None:
        (self.prod / "incoming" / "database.sqlite3").unlink()

        self.host.deploy(R4)

        self.assertEqual(tree(self.prod / "backup"), self.backup)
        for leftover in ("incoming", "failed"):
            self.assertFalse((self.prod / leftover).exists(), leftover)


class AttemptIdTests(HostTestCase):
    """Attempt ids carry the time the carry-forward compares, so they never go backwards,
    and a damaged record never stops a deployment.

    A Raspberry Pi without an RTC battery can boot with its clock behind the last attempt
    until NTP catches up, so a record from the future is the ordinary case here.
    """

    def rewrite_live_attempt(self, attempt: str) -> None:
        current = self.host.current()
        current["attempt"] = attempt
        legacy_write_json(self.prod / "current.json", current)

    def test_an_id_is_never_earlier_than_the_latest_the_host_records(self) -> None:
        self.host.deploy(R1)
        self.rewrite_live_attempt("20990101T000000000000Z-0123456789ab")

        self.host.deploy(R2)

        attempt = self.host.current()["attempt"]
        self.assertEqual(attempt[:22], "20990101T000000000001Z", attempt)

    def test_ids_are_ordered_to_the_microsecond(self) -> None:
        self.host.deploy(R1)
        first = deploy.attempt_time(self.host.current()["attempt"])
        self.host.deploy(R2)
        second = deploy.attempt_time(self.host.current()["attempt"])
        self.assertIsNotNone(first)
        self.assertGreater(second, first)
        self.assertRegex(self.host.current()["attempt"], r"^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}$")

    def test_a_damaged_attempt_id_never_stops_a_deployment(self) -> None:
        self.host.deploy(R1)
        self.rewrite_live_attempt("20261399T999999Z-0123456789ab")  # the shape, no real time

        self.host.deploy(R2)

        self.assertEqual(self.host.current()["image"], R2.image)
        self.assertIsNone(deploy.attempt_time("20261399T999999Z-0123456789ab"))

    def test_a_record_from_the_end_of_time_never_stops_a_deployment(self) -> None:
        self.host.deploy(R1)
        self.rewrite_live_attempt("99991231T235959999999Z-0123456789ab")

        self.host.deploy(R2)

        self.assertEqual(self.host.current()["image"], R2.image)
        self.assertIsNotNone(deploy.attempt_time(self.host.current()["attempt"]))


class OneBackupTests(HostTestCase):
    def test_exactly_one_database_copy_after_two_successes(self) -> None:
        self.host.deploy(R1)
        self.host.deploy(R2)
        self.host.deploy(R3)
        self.assertEqual(self.host.sqlite_files(), [self.prod / "backup" / "database.sqlite3"])

    def test_a_failure_leaves_the_backup_and_its_own_snapshot(self) -> None:
        self.host.deploy(R1)
        self.host.deploy(R2)
        self.docker.fail_up.add(R3.image)
        self.assert_fails(R3)
        self.assertEqual(
            self.host.sqlite_files(),
            [self.prod / "backup" / "database.sqlite3", self.prod / "failed" / "database.sqlite3"],
        )


class GuardTests(HostTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.host.deploy(R2)
        self.before = tree(self.host.home)
        self.docker.calls.clear()

    def assert_nothing_changed(self) -> None:
        self.assertEqual(tree(self.host.home), self.before)
        self.assertEqual(self.docker.running_image, R2.image)

    def test_an_older_run_is_refused_before_pull(self) -> None:
        self.assertIn("older", self.assert_fails(R1))
        self.assertEqual(self.docker.calls, [])
        self.assert_nothing_changed()

    def test_a_conflicting_run_with_the_same_number_is_refused_before_pull(self) -> None:
        self.assert_fails(R3, run_number=R2.run_number)
        self.assertEqual(self.docker.calls, [])
        self.assert_nothing_changed()

    def test_an_identical_rerun_redeploys(self) -> None:
        first = (self.prod / "current.json").read_bytes()
        self.host.deploy(R2)
        self.assertEqual(self.host.current()["image"], R2.image)
        self.assertEqual(tree(self.prod / "backup")["current.json"], first)

    def test_a_revision_label_mismatch_is_refused_before_anything_changes(self) -> None:
        self.docker.published[R3.image] = dict(
            R3.labels, **{"org.opencontainers.image.revision": "0" * 40}
        )
        self.assertIn("revision", self.assert_fails(R3, publish=False))
        self.assertEqual(self.docker.kinds(), ["pull", "image-inspect"])
        self.assert_nothing_changed()

    def test_a_version_label_mismatch_is_refused_before_anything_changes(self) -> None:
        self.docker.published[R3.image] = dict(
            R3.labels, **{"org.opencontainers.image.version": "v9.9.9"}
        )
        self.assertIn("version", self.assert_fails(R3, publish=False))
        self.assertEqual(self.docker.kinds(), ["pull", "image-inspect"])
        self.assert_nothing_changed()

    def test_an_unpullable_image_is_refused_before_anything_changes(self) -> None:
        self.assert_fails(R3, publish=False)
        self.assertEqual(self.docker.kinds(), ["pull"])
        self.assert_nothing_changed()

    def test_the_backup_is_the_integrity_checked_sqlite_backup(self) -> None:
        self.host.deploy(R3)
        (backup,) = self.docker.of_kind("exec-backup")
        script = backup.argv[5]
        self.assertIn("source.backup(target)", script)
        self.assertIn("PRAGMA integrity_check", script)
        self.assertIn("mode=ro", script)
        self.assertEqual(backup.argv[2], self.docker.calls[3].argv[2], "the verified container")
        (rm,) = self.docker.of_kind("exec-rm")
        self.assertEqual(rm.argv[5], backup.argv[6], "the in-container copy is removed")
        self.assertEqual(self.docker.inner_files, {})

    @unittest.skipUnless(POSIX, "Windows reports every writable file as 0o666")
    def test_a_loosened_secrets_env_is_refused_before_up(self) -> None:
        os.chmod(self.prod / "secrets.env", 0o644)
        self.assertIn("group or world readable", self.assert_fails(R3))
        self.assertNotIn("compose-up", self.docker.kinds())
        self.assertEqual(self.host.current()["image"], R2.image)


class SecretTests(HostTestCase):
    def test_the_secret_never_leaves_secrets_env(self) -> None:
        self.host.write_secrets(self.prod)
        self.host.deploy(R1)
        self.host.deploy(R2)
        self.docker.fail_up.add(R3.image)
        self.assert_fails(R3)
        stdout, stderr = io.StringIO(), io.StringIO()
        self.host.stage_kit(R4)
        self.docker.publish(R4)
        with (
            self.host.patched(),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = deploy.main(
                [
                    "--environment",
                    "prod",
                    "--image",
                    R4.image,
                    "--revision",
                    R4.revision,
                    "--version",
                    R4.version,
                    "--run-number",
                    str(R4.run_number),
                    "--source-run-url",
                    R4.source_run_url,
                    "--root",
                    str(self.host.root),
                ]
            )
        self.assertEqual(code, 0, stderr.getvalue())

        secret = SENTINEL_SECRET.strip()
        self.assertEqual((self.prod / "secrets.env").read_bytes(), SENTINEL_SECRET)
        for name, data in tree(self.host.home).items():
            if name != "portfolio-app/prod/secrets.env":
                self.assertNotIn(secret, data, name)
        for call in self.docker.calls:
            self.assertNotIn(secret.decode(), repr(call.argv) + repr(call.env))
        self.assertNotIn(secret.decode(), stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
