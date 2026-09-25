"""Guardrail tests for the time bounds on every workflow job and on every backend test.

A job without ``timeout-minutes`` runs until GitHub's default of 360 minutes. While #10 was
under review, a test-isolation bug hung the backend suite forever inside one test, and a
mutation that made the scheduler tick catch ``BaseException`` did the same. In CI either
one would have failed six hours late and held a runner the whole time. #65 put a bound on
every job and every test. These tests fail when a job is added without one, when a bound
grows too large to mean anything, or when the bounds stop firing in the order they were
chosen for.

The workflows are read with a small line parser rather than PyYAML, because this suite
runs with nothing installed. The parser relies on the two-space indentation every
workflow here already uses. One test feeds it a sample with a missing timeout, to show
the check can fail at all: a guard nobody has seen fail is a guard nobody knows the
state of.

Plain ``unittest``, no dependencies.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
import unittest
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
BACKEND_PYPROJECT = REPO_ROOT / "backend" / "pyproject.toml"

# The largest bound any job may declare without editing this file. The deploy job holds the
# largest today, at 20. A job set to 360 would pass a "has a timeout" check and still hold
# a runner for six hours, so the check has to be about size as well as presence.
MAX_TIMEOUT_MINUTES = 30

# How much longer the deploy job must be allowed to run than the SSH call it wraps. Before
# that call, remote_deploy.py spends up to 60 s on scp, and the job has already checked
# out and joined the tailnet, each under 10 s in every measured run. After it, the
# script's `finally` needs one more short SSH call to remove the registry credential.
DEPLOY_JOB_HEADROOM_SECONDS = 120

# The job that runs pytest, and so the one whose timeout the per-test ceiling must beat.
BACKEND_TEST_JOB = ("ci.yml", "test-backend")
DEPLOY_JOB = ("remote-deploy.yml", "deploy")

JOB_ID = re.compile(r"^  (?P<id>[A-Za-z_][A-Za-z0-9_-]*):\s*(?:#.*)?$")
JOB_KEY = re.compile(r"^    (?P<key>[A-Za-z_][A-Za-z0-9_-]*):\s*(?P<value>[^#]*?)\s*(?:#.*)?$")


def workflow_jobs(text: str) -> dict[str, dict[str, str]]:
    """Map each job id under ``jobs:`` to its own keys and their scalar values.

    Only the keys directly on the job are collected, which is all the checks below need.
    Anything nested deeper, such as ``steps`` or ``with``, has a deeper indent and is
    skipped.
    """
    jobs: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    in_jobs = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_jobs = line.rstrip() == "jobs:"
            current = None
            continue
        if not in_jobs:
            continue
        if match := JOB_ID.match(line):
            current = jobs.setdefault(match["id"], {})
        elif current is not None and (match := JOB_KEY.match(line)):
            current[match["key"]] = match["value"]
    return jobs


def unbounded_jobs(name: str, text: str) -> list[str]:
    """Every job in one workflow that could run until GitHub's 360-minute default.

    A job that calls a reusable workflow cannot take ``timeout-minutes``; GitHub rejects
    the key there. It is accepted only when the called workflow is in this repository,
    because then this suite checks that workflow's jobs as well.
    """
    problems: list[str] = []
    for job_id, keys in workflow_jobs(text).items():
        where = f"{name}: job '{job_id}'"
        if "uses" in keys:
            if not keys["uses"].startswith("./.github/workflows/"):
                problems.append(f"{where} calls a workflow this suite cannot check")
            continue
        value = keys.get("timeout-minutes")
        if value is None:
            problems.append(f"{where} has no timeout-minutes")
        elif not value.isdigit() or not 1 <= int(value) <= MAX_TIMEOUT_MINUTES:
            problems.append(
                f"{where} has timeout-minutes {value!r}; it must be a whole number from 1 "
                f"to {MAX_TIMEOUT_MINUTES}"
            )
    return problems


def workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")])


def job_timeout_seconds(workflow: str, job_id: str) -> int:
    jobs = workflow_jobs((WORKFLOWS_DIR / workflow).read_text(encoding="utf-8"))
    return int(jobs[job_id]["timeout-minutes"]) * 60


def load_remote_deploy() -> ModuleType:
    if "remote_deploy" in sys.modules:
        return sys.modules["remote_deploy"]
    spec = importlib.util.spec_from_file_location(
        "remote_deploy", REPO_ROOT / "scripts" / "remote_deploy.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["remote_deploy"] = module
    spec.loader.exec_module(module)
    return module


SAMPLE = """\
name: Sample

on:
  pull_request:

jobs:
  bounded:
    runs-on: ubuntu-latest
    timeout-minutes: 5 # a trailing comment is not part of the value
    steps:
      - run: echo bounded

  unbounded:
    runs-on: ubuntu-latest
    steps:
      # A nested key with the same name must not count for the job.
      - timeout-minutes: 5
        run: echo unbounded

  six-hours:
    runs-on: ubuntu-latest
    timeout-minutes: 360

  reusable:
    uses: ./.github/workflows/ci.yml
"""


class ParserTests(unittest.TestCase):
    def test_reads_each_jobs_own_keys(self) -> None:
        jobs = workflow_jobs(SAMPLE)
        self.assertEqual(list(jobs), ["bounded", "unbounded", "six-hours", "reusable"])
        self.assertEqual(jobs["bounded"]["timeout-minutes"], "5")
        self.assertNotIn("timeout-minutes", jobs["unbounded"])
        self.assertEqual(jobs["reusable"]["uses"], "./.github/workflows/ci.yml")

    def test_reports_a_missing_bound_and_an_oversized_one(self) -> None:
        self.assertEqual(
            unbounded_jobs("sample.yml", SAMPLE),
            [
                "sample.yml: job 'unbounded' has no timeout-minutes",
                "sample.yml: job 'six-hours' has timeout-minutes '360'; it must be a whole "
                f"number from 1 to {MAX_TIMEOUT_MINUTES}",
            ],
        )


class WorkflowTimeoutTests(unittest.TestCase):
    def test_every_workflow_is_found_and_parsed(self) -> None:
        # Without this, a change in indentation would make the parser see no jobs, and
        # the check below would pass by checking nothing.
        files = workflow_files()
        self.assertTrue(files, f"no workflows found under {WORKFLOWS_DIR}")
        for path in files:
            with self.subTest(workflow=path.name):
                self.assertTrue(
                    workflow_jobs(path.read_text(encoding="utf-8")),
                    f"{path.name} parsed to no jobs",
                )

    def test_every_job_is_time_bounded(self) -> None:
        problems = [
            problem
            for path in workflow_files()
            for problem in unbounded_jobs(path.name, path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(problems, [], "\n".join(problems))

    def test_the_deploy_job_outlasts_the_ssh_call_it_wraps(self) -> None:
        # If the job timeout fired first, the runner would be killed mid-deploy, before
        # remote_deploy.py's `finally` removed the registry credential from the host.
        ssh_timeout = load_remote_deploy().SSH_TIMEOUT_SECONDS
        self.assertGreaterEqual(
            job_timeout_seconds(*DEPLOY_JOB),
            ssh_timeout + DEPLOY_JOB_HEADROOM_SECONDS,
        )


def pytest_ini_options() -> dict[str, object]:
    with BACKEND_PYPROJECT.open("rb") as handle:
        options: dict[str, object] = tomllib.load(handle)["tool"]["pytest"]["ini_options"]
    return options


class PerTestCeilingTests(unittest.TestCase):
    def ceiling_seconds(self) -> float:
        value = pytest_ini_options().get("timeout")
        self.assertIsNotNone(value, "backend/pyproject.toml sets no pytest `timeout`")
        # pytest-timeout reads the option as a number of seconds, fractions allowed.
        return float(str(value))

    def test_every_backend_test_has_a_ceiling(self) -> None:
        self.assertGreater(self.ceiling_seconds(), 0, "a pytest timeout of 0 disables it")

    def test_the_ceiling_cannot_be_dropped_silently(self) -> None:
        # With --strict-config, a `timeout` option and no plugin to claim it is a startup
        # error. Without it, losing pytest-timeout would quietly remove the ceiling.
        self.assertIn("--strict-config", pytest_ini_options().get("addopts", []))

    def test_the_ceiling_fires_before_the_job_timeout(self) -> None:
        # Otherwise a hung test ends as an anonymous job timeout again, the failure the
        # ceiling exists to replace.
        self.assertLess(self.ceiling_seconds(), job_timeout_seconds(*BACKEND_TEST_JOB))


if __name__ == "__main__":
    unittest.main()
