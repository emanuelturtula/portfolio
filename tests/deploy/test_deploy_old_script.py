"""An old delivery run, re-run after the migration, must stop before it touches anything.

Every commit before #94 ships a deploy.py that defaults to ``~/portfolio-app-deploy``, and a
re-run of an old delivery uploads exactly that script. On a migrated host it would recreate
the empty old root, find no ``current.json`` and so skip rerun protection, start a container
with an empty ``secrets.env``, and leave both roots behind, so every later deployment
refuses. The migration therefore leaves a regular file where the old root was: the old
script's ``root.mkdir(exist_ok=True)`` raises on it before any docker command.

The old script is vendored in ``fixtures/deploy_pre94.py``, because CI checks out a single
commit and could not otherwise run it. A test holds the fixture to git byte for byte
wherever git has the object.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from deploy_harness import (
    POSIX,
    PRE_94_COMMIT,
    Host,
    Release,
    deploy,
    fcntl_available,
    load_pre_94_deploy,
    pre_94_source,
    pre_94_source_from_git,
    tree,
)

R4, R5, R6, R7 = Release(4), Release(5), Release(6), Release(7)


class Pre94FixtureTests(unittest.TestCase):
    def test_the_fixture_is_the_pre_94_script_byte_for_byte(self) -> None:
        from_git = pre_94_source_from_git()
        if from_git is None:
            self.skipTest(
                f"git cannot show {PRE_94_COMMIT[:7]} here (a shallow clone); the tests "
                "below still run the vendored copy"
            )
        self.assertEqual(pre_94_source(), from_git)

    def test_the_fixture_is_the_script_this_issue_replaced(self) -> None:
        source = pre_94_source()
        self.assertIn(b'Path.home() / "portfolio-app-deploy"', source)
        self.assertIn(b"def prune_attempts", source)
        self.assertNotIn(b"\r", source)


class OldScriptAfterMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.host = Host(Path(temp.name))
        self.host.build_legacy()
        self.host.deploy(R4)
        upload = self.host.base / "old-upload"
        upload.mkdir()
        self.old = load_pre_94_deploy(upload)
        self.docker = self.host.docker
        self.docker.calls.clear()

    def run_old(self, release: Release) -> tuple[int, str]:
        """Run the old script's main() as an old delivery would: default root, no --root."""
        self.docker.publish(release)
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(fcntl_available())
            stack.enter_context(
                mock.patch.object(Path, "home", staticmethod(lambda: self.host.home))
            )
            stack.enter_context(mock.patch.object(self.old, "run", self.docker))
            if not POSIX:
                stack.enter_context(
                    mock.patch.object(
                        self.old, "prepare_secrets_env_file", old_windows_secrets(self.old)
                    )
                )
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(output))
            code = self.old.main(
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
                ]
            )
        return code, output.getvalue()

    def test_the_migration_leaves_a_regular_file_at_the_old_root(self) -> None:
        tombstone = self.host.legacy
        self.assertTrue(tombstone.is_file(), "a regular file, so mkdir(exist_ok=True) raises")
        self.assertFalse(tombstone.is_symlink())
        text = tombstone.read_text(encoding="utf-8")
        self.assertIn("portfolio-app", text.replace("portfolio-app-deploy", ""))
        self.assertTrue(self.host.root.is_dir())

    def test_an_old_delivery_stops_before_any_docker_command(self) -> None:
        tombstone = self.host.legacy.read_bytes()
        live = tree(self.host.root)

        code, output = self.run_old(R5)

        self.assertNotEqual(code, 0, output)
        self.assertEqual(self.docker.calls, [], "the old script reached docker")
        self.assertTrue(self.host.legacy.is_file())
        self.assertEqual(self.host.legacy.read_bytes(), tombstone)
        self.assertEqual(tree(self.host.root), live, "the live deployment was touched")
        self.assertEqual(self.docker.running_image, R4.image)

        self.host.deploy(R6)
        self.assertEqual(self.host.current()["image"], R6.image)
        self.assertTrue(self.host.legacy.is_file())

    def test_without_the_tombstone_an_old_delivery_would_split_the_host(self) -> None:
        # Why the tombstone exists: the guard above can fail. With the file gone, the old
        # script recreates its root and deploys into it, and the host is then refused.
        self.host.legacy.unlink()

        code, output = self.run_old(R5)

        self.assertEqual(code, 0, output)
        self.assertIn("compose-up", self.docker.kinds())
        self.assertTrue(self.host.legacy.is_dir())
        with self.assertRaises(deploy.DeploymentError):
            self.host.deploy(R6)

    def test_the_new_script_treats_the_tombstone_as_no_root(self) -> None:
        root, legacy = self.host.root, self.host.legacy
        before = tree(self.host.home)
        self.assertEqual(deploy.lock_directory(root, legacy), root)
        self.assertEqual(deploy.settle_root(root, legacy), root)
        self.assertEqual(tree(self.host.home), before, "the tombstone is left alone")

    def test_a_tombstone_without_a_new_root_is_a_fresh_host(self) -> None:
        tombstone = self.host.legacy.read_bytes()
        os.rename(self.host.root, self.host.base / "moved-away")

        self.host.deploy(R7)

        self.assertEqual(self.host.current()["image"], R7.image)
        self.assertEqual(self.host.legacy.read_bytes(), tombstone)


def old_windows_secrets(old: types.ModuleType) -> object:
    """The old script's secrets check, minus the mode test Windows cannot express."""
    original = old.prepare_secrets_env_file

    def prepare(root: Path, environment: str) -> Path:
        path = Path(root) / environment / "secrets.env"
        if path.exists():
            return path
        result: Path = original(root, environment)
        return result

    return prepare


if __name__ == "__main__":
    unittest.main()
