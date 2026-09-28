"""Atomic writes, and a crash at every step of a deployment.

deploy.py promises that every file it writes is replaced atomically, and that the rotation
order bounds what a crash can leave: at worst ``current.json`` one deployment behind the
running container, which the next deployment handles. These tests hold it to that.

``FilesystemTap`` numbers every filesystem mutation deploy.py makes -- through its own
``os``, ``shutil`` and ``open``, and through ``Path`` -- and kills the process before the
k-th. Each sweep runs k = 0, 1, 2, ... until a run completes, so every step of the
deployment is a crash point, including ones added later. After each crash the state must
satisfy the invariants below, and the next deployment must succeed and leave exactly the
layout a clean one would.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deploy_harness import (
    SENTINEL_SECRET,
    Crash,
    FilesystemTap,
    Host,
    Release,
    deploy,
    expected_compose_sh,
    tree,
)

R1, R2, R3, R4, R5 = (Release(n) for n in range(1, 6))
MAX_STEPS = 300
BACKUP = {"compose.yml", "current.json", "database.sqlite3"}
EVIDENCE = {"compose.yml", "request.json", "result.json", "database.sqlite3"}


def image_of(data: bytes | None) -> str | None:
    return None if data is None else json.loads(data)["image"]


class AtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.host = Host(Path(temp.name))

    def test_every_file_is_written_beside_itself_then_replaced(self) -> None:
        tap = FilesystemTap()
        self.host.deploy(R1, tap=tap)
        self.host.deploy(R2, tap=tap)
        self.host.docker.fail_up.add(R3.image)
        with self.assertRaises(deploy.DeploymentError):
            self.host.deploy(R3, tap=tap)
        prod = self.host.prod

        replaced = {Path(step.args[1]): Path(step.args[0]) for step in tap.named("os.replace")}
        for final in (
            prod / "compose.yml",
            prod / "current.json",
            prod / "compose.sh",
            prod / "last-attempt.json",
            prod / "incoming" / "compose.yml",
            prod / "incoming" / "request.json",
            prod / "incoming" / "result.json",
            prod / "backup.new" / "compose.yml",
            prod / "backup.new" / "current.json",
        ):
            self.assertIn(final, replaced, f"{final} was not replaced atomically")
            self.assertEqual(
                replaced[final].parent, final.parent, "same directory, same filesystem"
            )
            self.assertNotEqual(replaced[final], final)

        # Database copies arrive by docker cp or a file copy, not by a write of deploy.py's.
        written = [
            Path(step.args[0])
            for step in tap.steps
            if step.name in ("open", "Path.write_text", "Path.write_bytes")
            and "database.sqlite3" not in Path(step.args[0]).name
        ]
        self.assertTrue(written, "the tap saw no writes at all, so this proved nothing")
        sources = [Path(step.args[0]) for step in tap.named("os.replace")]
        for path in written:
            self.assertIn(path, sources, f"{path} was written in place, not replaced")
            self.assertEqual(sources.count(path), written.count(path), f"{path} left behind")
        self.assertGreaterEqual(len(tap.named("os.fsync")), len(written), "every write is synced")

    def test_modes_are_set_on_the_temporary_file_before_it_is_renamed_in(self) -> None:
        tap = FilesystemTap()
        self.host.deploy(R1, tap=tap)
        self.host.deploy(R2, tap=tap)
        chmods = {Path(step.args[0]): step.args[1] for step in tap.named("os.chmod")}
        written = {Path(step.args[0]) for step in tap.named("open")}
        checked = 0
        for step in tap.named("os.replace"):
            temporary, final = Path(step.args[0]), Path(step.args[1])
            if temporary not in written:
                continue  # a database copy renamed into place, not a file deploy.py wrote
            expected = 0o700 if final.name == "compose.sh" else 0o600
            self.assertEqual(chmods.get(temporary), expected, final)
            checked += 1
        self.assertGreaterEqual(checked, 10)


class DurableBeforeDeletingTests(unittest.TestCase):
    """The new database copy is on disk before any older copy is deleted.

    On ext4 a new file renamed into a new name can lag its data by about thirty seconds,
    while the unlinks of the copies it replaces commit in about five. A power cut in
    between would leave an empty backup and nothing else. So the file, the directory it
    lands in and, after a swap, prod/ itself are fsynced first. fsync_directory is a no-op
    off POSIX, but it is still called, and the tap sees the call on every platform.
    """

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.host = Host(Path(temp.name))

    def assert_durable_before_deleting(self, tap: FilesystemTap, copy: Path, *dirs: Path) -> None:
        deletions = [
            index
            for index, step in enumerate(tap.steps)
            if (step.name == "shutil.rmtree" and step.extra)
            or (
                step.name in ("os.remove", "os.unlink", "Path.unlink")
                and str(step.args[0]).endswith(".sqlite3")
            )
        ]
        self.assertTrue(deletions, "the premise: an older copy was deleted")
        before = tap.steps[: deletions[0]]
        synced_files = {step.extra for step in before if step.name == "fsync_file"}
        synced_dirs = {step.extra for step in before if step.name == "fsync_directory"}
        self.assertIn(copy.stat().st_ino, synced_files, f"{copy} not fsynced before deleting")
        for directory in (copy.parent, *dirs):
            self.assertIn(
                directory.stat().st_ino, synced_dirs, f"{directory} not fsynced before deleting"
            )

    def test_a_snapshot_is_durable_before_the_old_backup_goes(self) -> None:
        self.host.deploy(R1)
        self.host.deploy(R2)
        tap = FilesystemTap()
        self.host.deploy(R3, tap=tap)
        prod = self.host.prod
        self.assert_durable_before_deleting(tap, prod / "backup" / "database.sqlite3", prod)

    def test_a_migration_snapshot_is_durable_before_attempts_goes(self) -> None:
        self.host.build_legacy()
        tap = FilesystemTap()
        self.host.deploy(R4, tap=tap)
        prod = self.host.prod
        self.assert_durable_before_deleting(tap, prod / "backup" / "database.sqlite3", prod)
        self.assertIn(
            self.host.home.stat().st_ino,
            {s.extra for s in tap.named("fsync_directory")},
            "the rename of the root is made durable",
        )

    def test_a_seeded_copy_is_durable_before_attempts_goes(self) -> None:
        self.host.build_legacy()
        self.host.docker.containers[self.host.docker.running or ""].health = "unhealthy"
        tap = FilesystemTap()
        self.host.deploy(R4, tap=tap)
        prod = self.host.prod
        self.assertEqual(self.host.docker.snapshots, [], "the premise: seeded, not snapshotted")
        self.assert_durable_before_deleting(tap, prod / "backup" / "database.sqlite3", prod)

    def test_a_failures_snapshot_is_durable_before_the_older_failure_goes(self) -> None:
        self.host.deploy(R1)
        self.host.docker.fail_up.update({R2.image, R3.image})
        with self.assertRaises(deploy.DeploymentError):
            self.host.deploy(R2)
        tap = FilesystemTap()
        with self.assertRaises(deploy.DeploymentError):
            self.host.deploy(R3, tap=tap)
        self.assert_durable_before_deleting(tap, self.host.prod / "failed" / "database.sqlite3")


class InterruptedBackupSwapTests(unittest.TestCase):
    """What the next deployment makes of a backup swap a crash interrupted.

    ``backup.new/`` is complete exactly when it holds ``database.sqlite3``, which is moved
    in last. A complete one holds the newest database copy on the host, so it is promoted;
    an incomplete one is discarded, and a backup moved aside is put back. Each deployment
    here cannot snapshot -- the live container is unhealthy -- so what it keeps is what
    the crash left, not a fresh copy that would hide a lost one.
    """

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.host = Host(Path(temp.name))
        self.host.deploy(R1)
        self.host.deploy(R2)
        self.prod = self.host.prod
        self.backup = tree(self.prod / "backup")
        self.newer = {
            "compose.yml": R2.compose,
            "current.json": (self.prod / "current.json").read_bytes(),
            "database.sqlite3": b"the newer snapshot a crashed deployment took\n",
        }

    def leave(self, name: str, files: dict[str, bytes]) -> None:
        directory = self.prod / name
        directory.mkdir()
        for file, data in files.items():
            (directory / file).write_bytes(data)

    def deploy_without_a_snapshot(self) -> dict[str, bytes]:
        docker = self.host.docker
        docker.containers[docker.running or ""].health = "unhealthy"
        self.host.deploy(R3)
        self.assertEqual(self.host.current()["image"], R3.image)
        self.assertIs(self.host.current()["backup"], False, "the premise: no snapshot")
        for leftover in ("backup.new", "backup.old"):
            self.assertFalse((self.prod / leftover).exists(), leftover)
        self.assertEqual(len(self.host.sqlite_files()), 1)
        return tree(self.prod / "backup")

    def test_a_complete_backup_new_beside_the_backup_is_promoted(self) -> None:
        self.leave("backup.new", self.newer)
        self.assertEqual(self.deploy_without_a_snapshot(), self.newer)

    def test_a_complete_backup_new_after_the_backup_moved_aside_is_promoted(self) -> None:
        (self.prod / "backup").rename(self.prod / "backup.old")
        self.leave("backup.new", self.newer)
        self.assertEqual(self.deploy_without_a_snapshot(), self.newer)

    def test_an_incomplete_backup_new_is_discarded(self) -> None:
        self.leave("backup.new", {k: v for k, v in self.newer.items() if k != "database.sqlite3"})
        self.assertEqual(self.deploy_without_a_snapshot(), self.backup)

    def test_an_incomplete_backup_new_after_the_backup_moved_aside_restores_it(self) -> None:
        (self.prod / "backup").rename(self.prod / "backup.old")
        self.leave("backup.new", {"compose.yml": R2.compose})
        self.assertEqual(self.deploy_without_a_snapshot(), self.backup)

    def test_a_stale_aside_does_not_block_promoting_a_complete_backup_new(self) -> None:
        # No crash sequence leaves all three: settle_backups clears backup.old first. A
        # person restoring by hand can, and the swap must not then fail after the
        # candidate is already healthy.
        self.leave("backup.old", {"database.sqlite3": b"a copy someone set aside\n"})
        self.leave("backup.new", self.newer)
        self.assertEqual(self.deploy_without_a_snapshot(), self.newer)

    def test_a_backup_left_aside_after_the_swap_completed_is_removed(self) -> None:
        self.leave("backup.old", {"database.sqlite3": b"the generation before\n"})
        self.assertEqual(self.deploy_without_a_snapshot(), self.backup)


class CrashSweep(unittest.TestCase):
    """Kill a deployment before each of its filesystem steps in turn."""

    # Whether the deployment after the crash finds the running container unable to give
    # a snapshot. See CrashThenNoSnapshotSweep.
    RECOVER_WITHOUT_SNAPSHOT = False

    def sweep(
        self,
        prepare: Callable[[Host], None],
        attempt: Release,
        check_crashed: Callable[[Host, dict[str, Any]], None],
        *,
        fails: bool = False,
        copies: tuple[int, ...] = (1,),
        newest: bool = False,
    ) -> int:
        k = 0
        while True:
            self.assertLess(k, MAX_STEPS, "the deployment never completed")
            with tempfile.TemporaryDirectory() as temp:
                host = Host(Path(temp))
                prepare(host)
                before = self.observe(host)
                if fails:
                    host.docker.fail_up.add(attempt.image)
                tap = FilesystemTap(crash_at=k)
                with self.subTest(crash_before_step=k):
                    try:
                        host.deploy(attempt, tap=tap)
                    except Crash:
                        pass
                    except deploy.DeploymentError:
                        self.assertTrue(fails, "only the failing sweep may fail cleanly")
                    if tap.crashed:
                        check_crashed(host, before)
                        self.check_databases_are_whole(host, before)
                        self.check_next_deployment_recovers(host, copies, newest=newest)
                        self.check_databases_are_whole(host, before)
                if not tap.crashed:
                    self.assertGreater(k, 10, "the tap saw too few steps to mean anything")
                    return k
            k += 1

    def observe(self, host: Host) -> dict[str, Any]:
        live = host.root if host.root.exists() else host.legacy
        prod = live / "prod"
        return {
            "current": (prod / "current.json").read_bytes()
            if (prod / "current.json").exists()
            else None,
            "compose": (prod / "compose.yml").read_bytes()
            if (prod / "compose.yml").exists()
            else None,
            "backup": tree(prod / "backup"),
            "compose.sh": (prod / "compose.sh").read_bytes()
            if (prod / "compose.sh").exists()
            else None,
            "database": host.docker.database,
            "running": host.docker.running_image,
            "databases": {path.read_bytes() for path in host.sqlite_files()},
        }

    def check_databases_are_whole(self, host: Host, before: dict[str, Any]) -> None:
        """Every database copy on the host is one that was taken whole: never a torn one."""
        known = before["databases"] | set(host.docker.snapshots)
        for path in host.sqlite_files():
            self.assertIn(path.read_bytes(), known, f"{path.name} in {path.parent.name} is torn")

    def live_prod(self, host: Host) -> Path:
        roots = [path for path in (host.root, host.legacy) if path.is_dir()]
        self.assertEqual(len(roots), 1, "exactly one root directory, always")
        if roots[0] == host.root and host.legacy.exists():
            self.assertEqual(host.legacy.read_bytes(), deploy.TOMBSTONE.encode("utf-8"))
        return roots[0] / "prod"

    def check_common(self, host: Host) -> Path:
        prod = self.live_prod(host)
        for path in host.home.rglob("*.json"):
            json.loads(path.read_bytes())  # never torn
        self.assertEqual((prod / "secrets.env").read_bytes(), SENTINEL_SECRET)
        return prod

    def check_next_deployment_recovers(
        self, host: Host, copies: tuple[int, ...], *, newest: bool = False
    ) -> None:
        release = host.next_release
        assert release is not None
        left = {path.read_bytes() for path in host.sqlite_files()}
        snapshots = len(host.docker.snapshots)
        host.docker.fail_up.clear()
        if self.RECOVER_WITHOUT_SNAPSHOT and host.docker.running is not None:
            host.docker.containers[host.docker.running].health = "unhealthy"
        migrated = host.legacy.exists() or (self.live_prod(host) / "attempts").is_dir()
        host.deploy(release)
        prod = host.prod
        if migrated:
            self.assertTrue(host.legacy.is_file(), "a migrated host keeps its tombstone")
            self.assertEqual(host.legacy.read_bytes(), deploy.TOMBSTONE.encode("utf-8"))
        else:
            self.assertFalse(host.legacy.exists())
        self.assertEqual(list(host.home.glob(".*.tmp")), [], "a temporary file in home")
        current = host.current()
        self.assertEqual((current["image"], current["status"]), (release.image, "healthy"))
        self.assertEqual((prod / "compose.yml").read_bytes(), release.compose)
        self.assertEqual((prod / "compose.sh").read_bytes(), expected_compose_sh(release))
        self.assertEqual(json.loads((prod / "last-attempt.json").read_bytes()), current)
        for leftover in ("incoming", "failed", "attempts", "backup.new", "backup.old"):
            self.assertFalse((prod / leftover).exists(), leftover)
        self.assertEqual(list(host.root.rglob("*.tmp")), [], "a temporary file was left behind")
        if (prod / "backup").exists():
            self.assertEqual(set(tree(prod / "backup")), BACKUP)
        self.assertIn(len(host.sqlite_files()), copies, "database copies after recovering")
        for path in host.home.rglob("*.json"):
            json.loads(path.read_bytes())
        if newest:
            # backup/ ends up with the newest database copy on the host: this deployment's
            # own snapshot, else the most recently taken of the copies the crash left,
            # wherever it left them (incoming/, failed/, backup.new/, backup/, backup.old/).
            if len(host.docker.snapshots) > snapshots:
                expected = host.docker.snapshots[-1]
            else:
                taken = host.docker.snapshots
                expected = max((c for c in left if c in taken), key=taken.index, default=None)
            self.assertEqual(
                tree(prod / "backup").get("database.sqlite3"),
                expected,
                "the recovered backup is not the newest copy the crash left",
            )

    # -- a deployment on the new layout ---------------------------------------------------

    def test_a_crash_at_any_step_of_a_deployment_is_recovered(self) -> None:
        def prepare(host: Host) -> None:
            host.write_secrets(host.prod)
            host.deploy(R1)
            host.deploy(R2)
            host.next_release = R4

        def check(host: Host, before: dict[str, Any]) -> None:
            prod = self.check_common(host)
            current = (prod / "current.json").read_bytes()
            promoted = image_of(current) == R3.image
            self.assertTrue(current == before["current"] or promoted)
            compose = (prod / "compose.yml").read_bytes()
            self.assertIn(compose, (R2.compose, R3.compose))
            if promoted:
                self.assertEqual(
                    compose, R3.compose, "the manifest never runs ahead of its compose file"
                )
            new_backup = {
                "compose.yml": R2.compose,
                "current.json": before["current"],
                "database.sqlite3": before["database"],
            }
            if (prod / "backup").exists():
                backup = tree(prod / "backup")
                self.assertIn(backup, (before["backup"], new_backup), "backup/ is never a mixture")
                if backup == new_backup:
                    self.assertTrue(promoted, "the backup is swapped only after current.json")
            else:
                self.assertEqual(tree(prod / "backup.old"), before["backup"])
                self.assertEqual(tree(prod / "backup.new"), new_backup)
                self.assertTrue(promoted)
            script = (prod / "compose.sh").read_bytes()
            self.assertIn(script, (before["compose.sh"], expected_compose_sh(R3)))
            if script == expected_compose_sh(R3):
                self.assertTrue(promoted)
            last = json.loads((prod / "last-attempt.json").read_bytes())
            if last["image"] == R3.image:
                self.assertTrue(promoted)
            self.assertGreaterEqual(len(host.sqlite_files()), 1, "a crash never costs the backup")

        steps = self.sweep(prepare, R3, check, newest=True)
        self.assertGreaterEqual(steps, 12)

    # -- the first deployment on a host --------------------------------------------------

    def test_a_crash_at_any_step_of_a_first_deployment_is_recovered(self) -> None:
        def prepare(host: Host) -> None:
            host.write_secrets(host.prod)
            host.next_release = R2

        def check(host: Host, before: dict[str, Any]) -> None:
            prod = self.check_common(host)
            if (prod / "current.json").exists():
                self.assertEqual((prod / "compose.yml").read_bytes(), R1.compose)

        # A crash before current.json was written leaves a host with no live deployment,
        # so the next one is a first deployment again and has nothing to back up.
        self.sweep(prepare, R1, check, copies=(0, 1))

    # -- the migrating deployment --------------------------------------------------------

    def test_a_crash_at_any_step_of_a_migration_is_recovered(self) -> None:
        def prepare(host: Host) -> None:
            host.build_legacy()
            host.next_release = R5

        def check(host: Host, before: dict[str, Any]) -> None:
            prod = self.check_common(host)
            current = (prod / "current.json").read_bytes()
            promoted = image_of(current) == R4.image
            self.assertTrue(current == before["current"] or promoted)
            if promoted:
                self.assertEqual((prod / "compose.yml").read_bytes(), R4.compose)
            if not (prod / "attempts").exists():
                self.assertTrue(promoted, "attempts/ goes only after the new manifest is in")
            if (prod / "backup").exists():
                self.assertEqual(
                    tree(prod / "backup"),
                    {
                        "compose.yml": R3.compose,
                        "current.json": before["current"],
                        "database.sqlite3": before["database"],
                    },
                )
                self.assertTrue(promoted)
            self.assertGreaterEqual(len(host.sqlite_files()), 1, "a crash never costs every copy")

        self.sweep(prepare, R4, check)

    def test_a_crash_at_any_step_of_a_migration_without_a_snapshot_is_recovered(self) -> None:
        def prepare(host: Host) -> None:
            host.build_legacy()
            host.docker.containers[host.docker.running or ""].health = "unhealthy"
            host.next_release = R5

        def check(host: Host, before: dict[str, Any]) -> None:
            self.check_common(host)
            self.assertGreaterEqual(len(host.sqlite_files()), 1, "a crash never costs every copy")

        self.sweep(prepare, R4, check)

    def test_a_crash_while_seeding_from_the_only_legacy_copy_is_recovered(self) -> None:
        # The owner's host may hold a single legacy copy. Seeding must not put that copy
        # where the next deployment would discard it as an incomplete backup.new.
        def prepare(host: Host) -> None:
            host.build_legacy()
            attempts = host.legacy / "prod" / "attempts"
            for path in sorted(attempts.rglob("database.sqlite3"))[:-1]:
                path.unlink()
            host.docker.containers[host.docker.running or ""].health = "unhealthy"
            host.next_release = R5

        def check(host: Host, before: dict[str, Any]) -> None:
            self.check_common(host)
            self.assertGreaterEqual(
                len(host.sqlite_files()), 1, "a crash never costs the only copy"
            )

        self.sweep(prepare, R4, check)

    # -- carrying a snapshot forward ------------------------------------------------------

    def prepare_young_host(self, host: Host) -> None:
        """One success, no backup/, then a failure whose rollback came up unhealthy: the
        only database copy on the host is the snapshot in failed/."""
        host.write_secrets(host.prod)
        host.deploy(R1)
        host.docker.fail_up.add(R2.image)
        host.docker.unhealthy.add(R1.image)
        with self.assertRaises(deploy.DeploymentError):
            host.deploy(R2)
        host.docker.fail_up.clear()
        self.assertEqual(len(host.sqlite_files()), 1)
        self.assertFalse((host.prod / "backup").exists())
        host.next_release = R4

    def check_only_copy_survives(self, host: Host, before: dict[str, Any]) -> None:
        self.check_common(host)
        copies = [path.read_bytes() for path in host.sqlite_files()]
        self.assertGreaterEqual(len(copies), 1, "a crash never costs the only copy")
        for copy in copies:
            self.assertIn(copy, before["databases"], "the carried copy is byte-identical")

    def test_a_crash_while_a_success_carries_the_only_copy_is_recovered(self) -> None:
        self.sweep(self.prepare_young_host, R3, self.check_only_copy_survives, newest=True)

    def test_a_crash_while_a_failure_carries_the_only_copy_is_recovered(self) -> None:
        self.sweep(
            self.prepare_young_host, R3, self.check_only_copy_survives, fails=True, newest=True
        )

    def test_a_crash_in_the_success_after_a_failure_is_recovered(self) -> None:
        # failed/ holds an older failure's snapshot while this success takes a newer one.
        # Whatever the crash leaves, the recovery must keep the newer.
        def prepare(host: Host) -> None:
            host.write_secrets(host.prod)
            host.deploy(R1)
            host.deploy(R2)
            host.docker.fail_up.add(R3.image)
            with self.assertRaises(deploy.DeploymentError):
                host.deploy(R3)
            host.docker.fail_up.clear()
            self.assertEqual(len(host.sqlite_files()), 2)
            host.next_release = R5

        def check(host: Host, before: dict[str, Any]) -> None:
            self.check_common(host)
            self.assertGreaterEqual(len(host.sqlite_files()), 1)

        self.sweep(prepare, R4, check, newest=True)

    # -- the failure path -----------------------------------------------------------------

    def test_a_crash_at_any_step_of_a_failed_deployment_is_recovered(self) -> None:
        def prepare(host: Host) -> None:
            host.write_secrets(host.prod)
            host.deploy(R1)
            host.deploy(R2)
            host.next_release = R4

        def check(host: Host, before: dict[str, Any]) -> None:
            prod = self.check_common(host)
            self.assertEqual((prod / "current.json").read_bytes(), before["current"])
            self.assertEqual((prod / "compose.yml").read_bytes(), before["compose"])
            self.assertEqual(tree(prod / "backup"), before["backup"])
            self.assertEqual((prod / "compose.sh").read_bytes(), before["compose.sh"])
            if (prod / "failed").exists():
                self.assertEqual(set(tree(prod / "failed")), EVIDENCE, "failed/ is never partial")

        self.sweep(prepare, R3, check, fails=True, newest=True)


class CrashThenNoSnapshotSweep(CrashSweep):
    """Every sweep again, with a recovery deployment that cannot take a snapshot.

    A crash is rarely alone: the container it leaves running may be the reason the
    deployment after it is needed, and then that deployment has nothing to back up. It
    must still find, in whatever the crash left, the copy it is not allowed to lose --
    a complete backup.new included.
    """

    RECOVER_WITHOUT_SNAPSHOT = True


if __name__ == "__main__":
    unittest.main()
