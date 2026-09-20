"""The single quality gate, used locally, by the agent hooks and mirrored in CI.

One command so that "it passes on my machine", "the agent said it was done" and "CI is
green" cannot mean three different things.

Usage:
    python scripts/check.py           # full gate: lint, types, layering, tests, secrets
    python scripts/check.py --fast    # quick gate for the agent stop hook
    python scripts/check.py --backend # backend only
    python scripts/check.py --frontend
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
FRONTEND = REPO_ROOT / "frontend"

Step = tuple[str, list[str], Path]


def backend_steps(fast: bool) -> list[Step]:
    if not (BACKEND / "pyproject.toml").exists():
        return []
    pytest_args = ["-q", "-x"] if fast else ["--cov", "--cov-report=term-missing"]
    steps: list[Step] = [
        ("ruff check", ["uv", "run", "ruff", "check", "."], BACKEND),
        ("ruff format", ["uv", "run", "ruff", "format", "--check", "."], BACKEND),
        ("mypy", ["uv", "run", "mypy"], BACKEND),
        ("import layering", ["uv", "run", "lint-imports"], BACKEND),
        ("pytest", ["uv", "run", "pytest", *pytest_args], BACKEND),
    ]
    return steps


def frontend_steps(fast: bool) -> list[Step]:
    if not (FRONTEND / "package.json").exists():
        return []
    test = ["npm", "run", "test"] if fast else ["npm", "run", "test:coverage"]
    steps: list[Step] = [
        ("eslint", ["npm", "run", "lint"], FRONTEND),
        ("tsc", ["npm", "run", "typecheck"], FRONTEND),
        ("vitest", test, FRONTEND),
    ]
    if not fast:
        steps.insert(1, ("prettier", ["npm", "run", "format:check"], FRONTEND))
    return steps


def shared_steps(fast: bool) -> list[Step]:
    steps: list[Step] = [
        (
            "deployment guardrails",
            [sys.executable, "-m", "unittest", "discover", "-s", "tests/deploy"],
            REPO_ROOT,
        )
    ]
    if not fast:
        # The history scan is the slow one, so the fast gate skips it. The pre-push hook
        # still runs it, which is the moment that actually matters.
        steps.append(
            ("secret scan", [sys.executable, "scripts/secret_scan.py", "--history"], REPO_ROOT)
        )
    return steps


def run(step: Step) -> bool:
    name, command, cwd = step
    if command[0] == sys.executable:
        resolved = command
    else:
        # On Windows npm and uv are .cmd shims; subprocess needs the resolved path.
        executable = shutil.which(command[0])
        if executable is None:
            print(f"  SKIP  {name}: {command[0]} is not installed")
            return True
        resolved = [executable, *command[1:]]
    started = time.monotonic()
    result = subprocess.run(resolved, cwd=cwd, check=False)
    elapsed = time.monotonic() - started
    status = "ok" if result.returncode == 0 else "FAILED"
    print(f"  {status:6} {name}  ({elapsed:.1f}s)")
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true", help="skip the slowest checks")
    parser.add_argument("--backend", action="store_true", help="backend checks only")
    parser.add_argument("--frontend", action="store_true", help="frontend checks only")
    args = parser.parse_args(argv)

    only_backend = args.backend and not args.frontend
    only_frontend = args.frontend and not args.backend

    steps: list[Step] = []
    if not only_frontend:
        steps += backend_steps(args.fast)
    if not only_backend:
        steps += frontend_steps(args.fast)
    if not (only_backend or only_frontend):
        steps += shared_steps(args.fast)

    if not steps:
        print("Nothing to check yet.")
        return 0

    failures = [step[0] for step in steps if not run(step)]
    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print(f"All {len(steps)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
