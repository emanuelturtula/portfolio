"""compose.sh: the one command a person uses against the live deployment.

It exists because every ``docker compose`` command the docs used to give pointed at a file
that did not exist and lacked four variables only deploy.py set. The text is asserted byte
for byte on every platform. Running it needs a POSIX shell, so that part runs on Linux CI
and is skipped on Windows; the fake ``docker`` it runs records its arguments and
environment, which is everything compose would act on.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from deploy_harness import (
    LEGACY_ATTEMPT_IDS,
    POSIX,
    SENTINEL_SECRET,
    FilesystemTap,
    Host,
    Release,
    deploy,
    expected_compose_sh,
    mode,
)

R1, R2, R3, R4 = Release(1), Release(2), Release(3), Release(4)
LEGACY_LIVE = f"attempts/{LEGACY_ATTEMPT_IDS[-1]}/compose.yml"

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
        self.assertEqual(
            deploy.compose_script(good, LEGACY_LIVE).encode(),
            expected_compose_sh(R1, LEGACY_LIVE),
        )
        for bad_file in (
            "../secrets.env",
            "/etc/compose.yml",
            "attempts/attempt-3/compose.yml",
            "attempts/../compose.yml",
            f"attempts/{LEGACY_ATTEMPT_IDS[-1]}/compose.yml'",
            "failed/compose.yml",
        ):
            with self.subTest(compose_file=bad_file):
                with self.assertRaises(deploy.DeploymentError):
                    deploy.compose_script(good, bad_file)


class ComposeScriptWhileLiveTests(unittest.TestCase):
    """R4: whenever a deployment is live, compose.sh exists and names it.

    It is written before the candidate's ``up``, so a deployment that then fails --
    including the first, migrating one -- still leaves the owner a working command.
    """

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.host = Host(Path(temp.name))
        self.script = self.host.prod / "compose.sh"

    def deploy_failing(self, release: Release) -> None:
        self.host.docker.fail_up.add(release.image)
        with self.assertRaises(deploy.DeploymentError):
            self.host.deploy(release)

    def test_a_failed_first_migration_leaves_compose_sh_for_the_legacy_deployment(self) -> None:
        self.host.build_legacy()
        self.deploy_failing(R4)
        self.assertEqual(self.script.read_bytes(), expected_compose_sh(R3, LEGACY_LIVE))
        self.assertTrue((self.host.prod / LEGACY_LIVE).is_file(), "it names a file that exists")
        if POSIX:
            self.assertEqual(mode(self.script), 0o700)

    def test_a_missing_compose_sh_is_restored_before_a_failed_deployment(self) -> None:
        self.host.deploy(R1)
        self.host.deploy(R2)
        self.script.unlink()
        self.deploy_failing(R3)
        self.assertEqual(self.script.read_bytes(), expected_compose_sh(R2))

    def test_a_stale_compose_sh_is_rewritten_for_the_live_deployment(self) -> None:
        self.host.deploy(R1)
        self.host.deploy(R2)
        self.script.write_bytes(expected_compose_sh(R1))
        self.deploy_failing(R3)
        self.assertEqual(self.script.read_bytes(), expected_compose_sh(R2))

    def test_a_live_manifest_that_does_not_validate_never_blocks_a_deployment(self) -> None:
        self.host.deploy(R1)
        current = self.host.prod / "current.json"
        manifest = json.loads(current.read_bytes())
        manifest["image"] = R1.image[:-1] + "Z"  # not a digest compose.sh may embed
        current.write_bytes(json.dumps(manifest).encode())
        self.script.unlink()

        self.host.deploy(R2)

        self.assertEqual(self.host.current()["image"], R2.image)
        self.assertEqual(self.script.read_bytes(), expected_compose_sh(R2))
        said = [line for line in self.host.stderr[-1].splitlines() if "compose.sh" in line]
        self.assertEqual(len(said), 1, self.host.stderr[-1])


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
            # Decoys: the script must set these itself, not inherit them, and a CDPATH
            # must not steer its cd anywhere else.
            "PORTFOLIO_IMAGE": "decoy-image",
            "PORTFOLIO_SECRETS_ENV_FILE": "/decoy/secrets.env",
            "CDPATH": str(self.elsewhere),
        }

    def run_script(self, script: str, *args: str, cwd: Path | None = None) -> dict[str, list[str]]:
        self.record.unlink(missing_ok=True)
        done = subprocess.run(
            [script, *args],
            cwd=cwd or self.elsewhere,
            env=self.environment,
            check=True,
            timeout=30,
            capture_output=True,
        )
        self.assertEqual(done.stdout, b"", "cd printed a CDPATH match into the command's output")
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

    def test_it_works_through_a_symlink(self) -> None:
        link = self.elsewhere / "portfolio-compose"
        link.symlink_to(self.host.prod / "compose.sh")
        recorded = self.run_script(str(link), "ps")
        self.assert_invocation(recorded, self.host.prod, "ps")

    def test_it_survives_a_rename_of_the_root(self) -> None:
        moved = self.host.home / "renamed"
        os.rename(self.host.root, moved)
        recorded = self.run_script(str(moved / "prod" / "compose.sh"), "ps")
        self.assert_invocation(recorded, moved / "prod", "ps")


if __name__ == "__main__":
    unittest.main()
