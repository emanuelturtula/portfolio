"""Guardrail tests for the host-side deployment script.

Only the pure logic is exercised here -- argument validation, rerun ordering and attempt
pruning. Anything touching Docker or the filesystem lock is verified by an actual
deployment, because a mock of Docker proves nothing about Docker.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("deploy", REPO_ROOT / "deploy" / "deploy.py")
assert _spec is not None and _spec.loader is not None
deploy = importlib.util.module_from_spec(_spec)
sys.modules["deploy"] = deploy
_spec.loader.exec_module(deploy)

IMAGE = "ghcr.io/emanuelturtula/portfolio@sha256:" + "a" * 64
REVISION = "b" * 40
RUN_URL = "https://github.com/emanuelturtula/portfolio/actions/runs/123"


class ValidateTests(unittest.TestCase):
    def validate(self, **overrides: object) -> None:
        arguments: dict[str, object] = {
            "environment": "prod",
            "image": IMAGE,
            "revision": REVISION,
            "version": "v1.2.3",
            "run_number": 1,
            "source_run_url": RUN_URL,
        }
        arguments.update(overrides)
        deploy.validate(**arguments)  # type: ignore[arg-type]

    def test_accepts_a_well_formed_request(self) -> None:
        self.validate()

    def test_rejects_an_unknown_environment(self) -> None:
        with self.assertRaises(deploy.DeploymentError):
            self.validate(environment="staging")

    def test_rejects_a_mutable_tag(self) -> None:
        with self.assertRaises(deploy.DeploymentError):
            self.validate(image="ghcr.io/emanuelturtula/portfolio:latest")

    def test_rejects_an_image_from_another_repository(self) -> None:
        with self.assertRaises(deploy.DeploymentError):
            self.validate(image="ghcr.io/someone-else/portfolio@sha256:" + "a" * 64)

    def test_rejects_a_short_revision(self) -> None:
        with self.assertRaises(deploy.DeploymentError):
            self.validate(revision="abc1234")

    def test_rejects_a_version_without_the_v_prefix(self) -> None:
        with self.assertRaises(deploy.DeploymentError):
            self.validate(version="1.2.3")

    def test_rejects_a_run_url_from_another_repository(self) -> None:
        with self.assertRaises(deploy.DeploymentError):
            self.validate(
                source_run_url="https://github.com/someone-else/portfolio/actions/runs/1"
            )

    def test_rejects_a_boolean_masquerading_as_a_run_number(self) -> None:
        # bool is a subclass of int, so an isinstance check would let True through as 1.
        with self.assertRaises(deploy.DeploymentError):
            self.validate(run_number=True)


class RunOrderTests(unittest.TestCase):
    def previous(self, run_number: int) -> dict[str, object]:
        return {
            "image": IMAGE,
            "revision": REVISION,
            "delivery": {"run_number": run_number, "source_run_url": RUN_URL},
        }

    def test_allows_a_newer_run(self) -> None:
        deploy.check_run_order(self.previous(4), IMAGE, REVISION, 5)

    def test_allows_an_identical_rerun_of_the_same_artifact(self) -> None:
        # Re-running a green workflow must be safe and idempotent.
        deploy.check_run_order(self.previous(4), IMAGE, REVISION, 4)

    def test_rejects_an_older_run(self) -> None:
        # Otherwise re-running an old workflow silently rolls production backwards.
        with self.assertRaises(deploy.DeploymentError):
            deploy.check_run_order(self.previous(9), IMAGE, REVISION, 3)

    def test_rejects_the_same_run_number_with_a_different_artifact(self) -> None:
        other = "ghcr.io/emanuelturtula/portfolio@sha256:" + "c" * 64
        with self.assertRaises(deploy.DeploymentError):
            deploy.check_run_order(self.previous(4), other, REVISION, 4)

    def test_allows_the_first_ever_deployment(self) -> None:
        deploy.check_run_order(None, IMAGE, REVISION, 1)


class PruneAttemptsTests(unittest.TestCase):
    def test_keeps_the_most_recent_attempts_and_the_protected_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            attempts = Path(temp)
            names = [f"2026010{index}T000000Z-{index:012d}" for index in range(1, 10)]
            for name in names:
                (attempts / name).mkdir()
            protected = str(attempts / names[0])
            deploy.KEEP_ATTEMPTS = 3
            deploy.prune_attempts(attempts, {protected})
            remaining = sorted(path.name for path in attempts.iterdir())
            self.assertIn(names[0], remaining, "the protected attempt must survive")
            self.assertEqual(len(remaining), 4, remaining)


if __name__ == "__main__":
    unittest.main()
