"""Guardrail tests for the runner-side deployment entry point.

These run with plain unittest and no dependencies, so they work on any machine and in CI
without installing the backend. They exist because every one of these checks is the only
thing standing between a crafted workflow run and a production deployment.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "remote_deploy", REPO_ROOT / "scripts" / "remote_deploy.py"
)
assert _spec is not None and _spec.loader is not None
remote_deploy = importlib.util.module_from_spec(_spec)
sys.modules["remote_deploy"] = remote_deploy
_spec.loader.exec_module(remote_deploy)

IMAGE = "ghcr.io/emanuelturtula/portfolio@sha256:" + "a" * 64
REVISION = "b" * 40
WORKFLOW_REF = (
    "emanuelturtula/portfolio/.github/workflows/delivery.yml@refs/heads/main"
)


def valid_environment() -> dict[str, str]:
    return {
        "DEPLOY_ENVIRONMENT": "prod",
        "DEPLOY_REVISION": REVISION,
        "DEPLOY_IMAGE": IMAGE,
        "DEPLOY_VERSION": "v1.0.0",
        "DEPLOY_HOST": "host.example.ts.net",
        "DEPLOY_USER": "deployuser",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REPOSITORY": "emanuelturtula/portfolio",
        "GITHUB_SHA": REVISION,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": WORKFLOW_REF,
        "GITHUB_RUN_NUMBER": "7",
        "GITHUB_RUN_ID": "999",
        "GHCR_TOKEN": "a-short-lived-job-token",
        "GHCR_USER": "someone",
    }


class ParseRequestTests(unittest.TestCase):
    def test_accepts_a_push_to_main_from_the_delivery_workflow(self) -> None:
        request = remote_deploy.parse_request(valid_environment())
        self.assertEqual(request.environment, "prod")
        self.assertEqual(request.image, IMAGE)
        self.assertEqual(
            request.source_run_url,
            "https://github.com/emanuelturtula/portfolio/actions/runs/999",
        )

    def reject(self, **overrides: str) -> str:
        environment = valid_environment()
        environment.update(overrides)
        with self.assertRaises(ValueError) as caught:
            remote_deploy.parse_request(environment)
        return str(caught.exception)

    def test_rejects_a_manually_dispatched_run(self) -> None:
        # workflow_dispatch would let anyone with write access deploy an arbitrary ref.
        self.reject(GITHUB_EVENT_NAME="workflow_dispatch")

    def test_rejects_a_push_to_a_feature_branch(self) -> None:
        self.reject(
            GITHUB_REF="refs/heads/feature/example",
            GITHUB_WORKFLOW_REF=(
                "emanuelturtula/portfolio/.github/workflows/"
                "delivery.yml@refs/heads/feature/example"
            ),
        )

    def test_rejects_a_different_workflow_in_this_repository(self) -> None:
        # A workflow added by a future pull request must not be able to reach the host.
        self.reject(
            GITHUB_WORKFLOW_REF=(
                "emanuelturtula/portfolio/.github/workflows/other.yml@refs/heads/main"
            )
        )

    def test_rejects_a_run_from_another_repository(self) -> None:
        self.reject(GITHUB_REPOSITORY="someone-else/portfolio")

    def test_rejects_a_revision_that_does_not_match_the_running_commit(self) -> None:
        self.reject(GITHUB_SHA="c" * 40)

    def test_rejects_a_mutable_image_tag(self) -> None:
        # Deploying by tag would make the deployed artifact unverifiable after the fact.
        self.reject(DEPLOY_IMAGE="ghcr.io/emanuelturtula/portfolio:latest")

    def test_rejects_an_image_from_another_repository(self) -> None:
        self.reject(DEPLOY_IMAGE="ghcr.io/someone-else/portfolio@sha256:" + "a" * 64)

    def test_rejects_a_prerelease_version_string(self) -> None:
        self.reject(DEPLOY_VERSION="v1.0.0-beta.1a2b3c4")

    def test_rejects_an_empty_registry_token(self) -> None:
        self.reject(GHCR_TOKEN="   ")

    def test_error_messages_never_echo_the_rejected_value(self) -> None:
        # A malformed secret must not end up in this public repository's build log.
        # Contains characters the host pattern rejects, so it is guaranteed to fail.
        secret = "super secret value/that@should never be printed"
        message = self.reject(DEPLOY_HOST=secret)
        self.assertNotIn(secret, message)
        self.assertNotIn("super secret", message)


class DeployCommandTests(unittest.TestCase):
    def test_scopes_the_registry_credential_to_the_temporary_directory(self) -> None:
        request = remote_deploy.parse_request(valid_environment())
        command = remote_deploy.deploy_command(request, ".cache/portfolio-delivery/abc")
        self.assertIn("DOCKER_CONFIG=.cache/portfolio-delivery/abc/docker", command)

    def test_passes_the_run_evidence_through_to_the_host(self) -> None:
        request = remote_deploy.parse_request(valid_environment())
        command = remote_deploy.deploy_command(request, "dir")
        self.assertIn("--run-number", command)
        self.assertIn("7", command)
        self.assertIn("--source-run-url", command)


if __name__ == "__main__":
    unittest.main()
