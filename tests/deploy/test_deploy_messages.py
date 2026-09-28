"""No message deploy.py prints or raises names the host's home directory.

stderr and every DeploymentError reach the public Actions log through ssh, and the home
directory names the host user. That user is a repository secret GitHub masks, but the
script does not rely on the mask: it writes paths under the home directory as ``~/...``.

``str(OSError)`` is where that slips: on Linux it spells out both of the error's paths in
full, while on Windows the same failure often has none, which is how a leak reached CI
without any Windows run noticing. So these tests construct the OSError themselves, with
two absolute paths under the home directory, and raise it at each place an OSError can
surface. The result is the same on every platform.
"""

from __future__ import annotations

import contextlib
import errno
import io
import tempfile
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from deploy_harness import Host, Release, deploy

R1, R2, R3 = Release(1), Release(2), Release(3)


class NoHomePathInMessagesTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.host = Host(Path(temp.name))
        home = mock.patch.object(Path, "home", staticmethod(lambda: self.host.home))
        home.start()
        self.addCleanup(home.stop)
        self.host.deploy(R1)

    def injected(self) -> OSError:
        """OSError(errno, strerror, filename, winerror, filename2): winerror is None, so
        the error is the same object on every platform, with both paths set."""
        prod = self.host.prod
        error = OSError(
            errno.EACCES,
            "Permission denied",
            str(prod / ".compose.sh.tmp"),
            None,
            str(prod / "compose.sh"),
        )
        assert error.filename2 == str(prod / "compose.sh")
        return error

    def assert_no_home_path(self, *texts: str) -> None:
        """No absolute path under the home directory, in any spelling.

        The temporary directory's own name is part of every absolute path here and of no
        ~/ path, so it is found whether a message prints a path plainly, as POSIX, or as
        repr() escapes it -- which is how str(OSError) prints filenames on Windows.
        """
        joined = "\n".join(texts)
        self.assertTrue(joined.strip(), "the premise: something was said")
        for revealing in (str(self.host.home), self.host.home.as_posix(), self.host.base.name):
            self.assertNotIn(revealing, joined)
        self.assertIn("~/portfolio-app/prod", joined, "the paths are named, as ~/...")

    @contextlib.contextmanager
    def failing(self, name: str, when: Callable[..., bool]) -> Iterator[None]:
        """Make deploy.<name> raise the injected OSError whenever ``when`` says so."""
        real = getattr(deploy, name)

        def replacement(*args: Any, **kwargs: Any) -> Any:
            if when(*args, **kwargs):
                raise self.injected()
            return real(*args, **kwargs)

        with mock.patch.object(deploy, name, replacement):
            yield

    def deploy_and_collect(self, release: Release) -> tuple[str, str]:
        """Deploy; return what went to stderr and the DeploymentError's message, if any."""
        try:
            self.host.deploy(release)
            message = ""
        except deploy.DeploymentError as error:
            message = str(error)
        return self.host.stderr[-1], message

    def test_compose_sh_that_cannot_be_written(self) -> None:
        # Both writes fail: the early one prints a line, the rotation's raises.
        self.host.prod.joinpath("compose.sh").unlink()
        with self.failing("write_atomic", lambda path, *a, **k: Path(path).name == "compose.sh"):
            stderr, message = self.deploy_and_collect(R2)
        self.assertIn("compose.sh", stderr)
        self.assertIn("recording it failed", message)
        self.assert_no_home_path(stderr, message)

    def test_failure_evidence_that_cannot_be_recorded(self) -> None:
        self.host.docker.fail_up.add(R2.image)
        with self.failing("write_atomic", lambda path, *a, **k: Path(path).name == "result.json"):
            stderr, message = self.deploy_and_collect(R2)
        self.assertIn("not recorded", message)
        self.assert_no_home_path(stderr, message)

    def test_a_backup_that_fails_with_an_os_error(self) -> None:
        with self.failing("backup", lambda *a, **k: True):
            stderr, message = self.deploy_and_collect(R2)
        self.assertIn("backup failed", message)
        self.assert_no_home_path(stderr, message)

    def test_an_os_error_that_reaches_main(self) -> None:
        self.host.stage_kit(R2)
        self.host.docker.publish(R2)
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            self.host.patched(),
            self.failing("retire_incoming", lambda *a, **k: True),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = deploy.main(
                [
                    "--environment",
                    "prod",
                    "--image",
                    R2.image,
                    "--revision",
                    R2.revision,
                    "--version",
                    R2.version,
                    "--run-number",
                    str(R2.run_number),
                    "--source-run-url",
                    R2.source_run_url,
                    "--root",
                    str(self.host.root),
                ]
            )
        self.assertEqual(code, 1)
        self.assert_no_home_path(stderr.getvalue(), stdout.getvalue())

    def test_the_guard_can_fail(self) -> None:
        # str() of the injected error names both paths in full on every platform, so a
        # message built from it would be caught above.
        error = self.injected()
        self.assertEqual(error.filename2, str(self.host.prod / "compose.sh"))
        with self.assertRaises(AssertionError):
            self.assert_no_home_path(str(error) + " ~/portfolio-app/prod")


if __name__ == "__main__":
    unittest.main()
