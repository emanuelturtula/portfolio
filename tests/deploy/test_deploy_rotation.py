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

        written = [
            Path(step.args[0])
            for step in tap.steps
            if step.name in ("open", "Path.write_text", "Path.write_bytes")
        ]
        snapshot_copies = [p for p in written if p.name == "database.sqlite3"]
        for path in written:
            if path in snapshot_copies:
                continue  # the fake `docker cp`, standing in for Docker, not deploy.py
            self.assertIn(path, set(replaced.values()), f"{path} was written in place")
        self.assertTrue(written, "the tap saw no writes at all, so this proved nothing")

        opened = [Path(step.args[0]) for step in tap.named("open")]
        synced_then_replaced = [Path(step.args[0]) for step in tap.named("os.replace")]
        self.assertEqual(sorted(map(str, opened)), sorted(map(str, synced_then_replaced)))
        self.assertGreaterEqual(len(tap.named("os.fsync")), len(opened), "every write is synced")

    def test_modes_are_set_on_the_temporary_file_before_it_is_renamed_in(self) -> None:
        tap = FilesystemTap()
        self.host.deploy(R1, tap=tap)
        self.host.deploy(R2, tap=tap)
        chmods = {Path(step.args[0]): step.args[1] for step in tap.named("os.chmod")}
        for step in tap.named("os.replace"):
            temporary, final = Path(step.args[0]), Path(step.args[1])
            expected = 0o700 if final.name == "compose.sh" else 0o600
            self.assertEqual(chmods.get(temporary), expected, final)


class CrashSweep(unittest.TestCase):
    """Kill a deployment before each of its filesystem steps in turn."""

    def sweep(
        self,
        prepare: Callable[[Host], None],
        attempt: Release,
        check_crashed: Callable[[Host, dict[str, Any]], None],
        *,
        fails: bool = False,
        copies: tuple[int, ...] = (1,),
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
                        self.check_next_deployment_recovers(host, copies)
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
        }

    def live_prod(self, host: Host) -> Path:
        exists = [path for path in (host.root, host.legacy) if path.exists()]
        self.assertEqual(len(exists), 1, "exactly one root, always")
        return exists[0] / "prod"

    def check_common(self, host: Host) -> Path:
        prod = self.live_prod(host)
        for path in host.home.rglob("*.json"):
            json.loads(path.read_bytes())  # never torn
        self.assertEqual((prod / "secrets.env").read_bytes(), SENTINEL_SECRET)
        return prod

    def check_next_deployment_recovers(self, host: Host, copies: tuple[int, ...]) -> None:
        release = host.next_release
        assert release is not None
        host.docker.fail_up.clear()
        host.deploy(release)
        prod = host.prod
        self.assertFalse(host.legacy.exists())
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

        steps = self.sweep(prepare, R3, check)
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

        self.sweep(prepare, R3, check, fails=True)


if __name__ == "__main__":
    unittest.main()
