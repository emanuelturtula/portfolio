"""compose.sh: the one command a person uses against the live deployment.

It exists because every ``docker compose`` command the docs used to give pointed at a file
that did not exist and lacked four variables only deploy.py set. The text is asserted byte
for byte on every platform. Running it needs a POSIX shell, so that part runs on Linux CI
and is skipped on Windows; the fake ``docker`` it runs records its arguments and
environment, which is everything compose would act on.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from deploy_harness import (
    POSIX,
    SENTINEL_SECRET,
    FilesystemTap,
    Host,
    Release,
    deploy,
    expected_compose_sh,
    mode,
)

R1, R2, R3 = Release(1), Release(2), Release(3)

FAKE_DOCKER = """#!/bin/sh
{
  for argument in "$@"; do
    printf 'arg=%s\\n' "$argument"
  done
  printf 'PORTFOLIO_IMAGE=%s\\n' "${PORTFOLIO_IMAGE-<unset>}"
  printf 'PORTFOLIO_PORT=%s\\n' "${PORTFOLIO_PORT-<unset>}"
  printf 'PORTFOLIO_ENVIRONMENT=%s\\n' "${PORTFOLIO_ENVIRONMENT-<unset>}"
  printf 'PORTFOLIO_SECRETS_ENV_FILE=%s\\n' "${PORTFOLIO_SECRETS_ENV_FILE-<unset>}"
} >> "$FAKE_DOCKER_RECORD"
printf 'end\\n' >> "$FAKE_DOCKER_RECORD"
"""


class ComposeScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.host = Host(Path(temp.name))
        self.script = self.host.prod / "compose.sh"

    def test_it_is_exactly_the_specified_script(self) -> None:
        self.host.deploy(R1)
        self.assertEqual(self.script.read_bytes(), expected_compose_sh(R1))
        self.assertNotIn(b"\r", self.script.read_bytes())

    def test_it_follows_the_live_deployment_and_not_a_failed_one(self) -> None:
        self.host.deploy(R1)
        self.host.deploy(R2)
        self.assertEqual(self.script.read_bytes(), expected_compose_sh(R2))
        self.host.docker.fail_up.add(R3.image)
        with self.assertRaises(deploy.DeploymentError):
            self.host.deploy(R3)
        self.assertEqual(self.script.read_bytes(), expected_compose_sh(R2))

    def test_it_carries_no_secret_and_no_host_path(self) -> None:
        self.host.write_secrets(self.host.prod)
        self.host.deploy(R1)
        text = self.script.read_bytes()
        self.assertNotIn(SENTINEL_SECRET.strip(), text)
        self.assertNotIn(str(self.host.home).encode(), text)
        self.assertNotIn(self.host.home.as_posix().encode(), text)

    @unittest.skipUnless(POSIX, "Windows has no POSIX permission bits to assert")
    def test_it_is_owner_only_and_executable(self) -> None:
        self.host.deploy(R1)
        self.assertEqual(mode(self.script), 0o700)

    def test_its_mode_is_requested_as_0700_on_every_platform(self) -> None:
        tap = FilesystemTap()
        self.host.deploy(R1, tap=tap)
        (replace,) = [s for s in tap.named("os.replace") if Path(s.args[1]) == self.script]
        temporary = Path(replace.args[0])
        modes = [
            s.args[1] for s in tap.named("os.chmod") if Path(s.args[0]) in (temporary, self.script)
        ]
        self.assertEqual(modes, [0o700])

    def test_it_refuses_to_embed_an_unvalidated_value(self) -> None:
        good = {"image": R1.image, "environment": "prod"}
        for bad in (
            {"image": R1.image + "'; rm -rf ~; '"},
            {"image": "ghcr.io/emanuelturtula/portfolio:latest"},
            {"environment": "prod'"},
            {"environment": "staging"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(deploy.DeploymentError):
                    deploy.compose_script(dict(good, **bad))
        self.assertEqual(deploy.compose_script(good).encode(), expected_compose_sh(R1))


@unittest.skipUnless(POSIX, "compose.sh is a POSIX shell script; Linux CI runs it")
class RunningComposeScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.host = Host(base)
        self.host.write_secrets(self.host.prod)
        self.host.deploy(R1)
        bin_dir = base / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_bytes(FAKE_DOCKER.encode())
        os.chmod(docker, 0o700)
        self.record = base / "docker-calls.txt"
        self.elsewhere = base / "elsewhere"
        self.elsewhere.mkdir()
        self.environment = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "FAKE_DOCKER_RECORD": str(self.record),
            # Decoys: the script must set these itself, not inherit them.
            "PORTFOLIO_IMAGE": "decoy-image",
            "PORTFOLIO_SECRETS_ENV_FILE": "/decoy/secrets.env",
        }

    def run_script(self, script: str, *args: str, cwd: Path | None = None) -> dict[str, list[str]]:
        self.record.unlink(missing_ok=True)
        subprocess.run(
            [script, *args],
            cwd=cwd or self.elsewhere,
            env=self.environment,
            check=True,
            timeout=30,
            capture_output=True,
        )
        lines = self.record.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[-1], "end")
        recorded: dict[str, list[str]] = {"arg": []}
        for line in lines[:-1]:
            key, _, value = line.partition("=")
            recorded.setdefault(key, []).append(value)
        return recorded

    def assert_invocation(self, recorded: dict[str, list[str]], prod: Path, *args: str) -> None:
        argv = recorded["arg"]
        self.assertEqual(argv[:4], ["compose", "--project-name", "portfolio-app-prod", "--file"])
        self.assertEqual(os.path.realpath(argv[4]), os.path.realpath(prod / "compose.yml"))
        self.assertEqual(argv[5:], list(args))
        self.assertEqual(recorded["PORTFOLIO_IMAGE"], [R1.image])
        self.assertEqual(recorded["PORTFOLIO_PORT"], ["8083"])
        self.assertEqual(recorded["PORTFOLIO_ENVIRONMENT"], ["prod"])
        (secrets,) = recorded["PORTFOLIO_SECRETS_ENV_FILE"]
        self.assertEqual(os.path.realpath(secrets), os.path.realpath(prod / "secrets.env"))

    def test_it_runs_the_documented_commands_against_the_live_deployment(self) -> None:
        for args in (
            ("up", "-d", "--force-recreate", "app"),
            ("ps",),
            ("exec", "app", "python", "-m", "portfolio", "create-user", "--username", "x"),
            ("exec", "app", "echo", "an argument with spaces", "and 'quotes'"),
        ):
            with self.subTest(args=args):
                recorded = self.run_script(str(self.host.prod / "compose.sh"), *args)
                self.assert_invocation(recorded, self.host.prod, *args)

    def test_it_works_from_its_own_directory_too(self) -> None:
        # A path with a slash, as a person types it; a bare name would search $PATH.
        recorded = self.run_script("./compose.sh", "ps", cwd=self.host.prod)
        self.assert_invocation(recorded, self.host.prod, "ps")

    def test_it_survives_a_rename_of_the_root(self) -> None:
        moved = self.host.home / "renamed"
        os.rename(self.host.root, moved)
        recorded = self.run_script(str(moved / "prod" / "compose.sh"), "ps")
        self.assert_invocation(recorded, moved / "prod", "ps")


if __name__ == "__main__":
    unittest.main()
