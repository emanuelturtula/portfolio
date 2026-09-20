"""Run gitleaks over this repository, failing closed.

This repository is public and the application handles real money, so the scan must never
silently pass. It exits non-zero when gitleaks cannot be found and when a ``.gitleaksignore``
file exists, because both turn a security gate into a no-op without anyone noticing.

Usage:
    python scripts/secret_scan.py --staged    # pre-commit: only what is about to be committed
    python scripts/secret_scan.py --history   # pre-push and CI: the full git history

Set ``GITLEAKS_BIN`` to point at a specific binary; otherwise ``PATH`` is searched, then the
default winget install location on Windows.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

CONFIG_NOT_FOUND = 2
REPO_ROOT = Path(__file__).resolve().parents[1]

WINGET_GLOB = r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gitleaks.Gitleaks_*\gitleaks.exe"


def find_gitleaks() -> str | None:
    """Locate the gitleaks binary, or return None so the caller can fail closed."""
    explicit = os.environ.get("GITLEAKS_BIN")
    if explicit and Path(explicit).exists():
        return explicit
    on_path = shutil.which("gitleaks")
    if on_path:
        return on_path
    if os.name == "nt":
        matches = sorted(glob.glob(os.path.expandvars(WINGET_GLOB)))
        if matches:
            return matches[-1]
    return None


def build_command(binary: str, *, staged: bool) -> list[str]:
    command = [
        binary,
        "git",
        "--config",
        str(REPO_ROOT / ".gitleaks.toml"),
        "--redact",
        "--verbose",
        "--no-banner",
        # An allowlist comment in the code must never be able to wave a finding through.
        "--ignore-gitleaks-allow",
        "--exit-code",
        "1",
    ]
    if staged:
        command += ["--staged", "--pre-commit"]
    command.append(str(REPO_ROOT))
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="scan only staged changes")
    mode.add_argument("--history", action="store_true", help="scan the full git history")
    args = parser.parse_args(argv)

    # A .gitleaksignore file would silently exempt findings from every future scan.
    ignore_file = REPO_ROOT / ".gitleaksignore"
    if ignore_file.exists():
        print(
            f"{ignore_file} exists. This repository does not permit blanket gitleaks "
            "exemptions: narrow the rule in .gitleaks.toml instead and explain it in the PR.",
            file=sys.stderr,
        )
        return CONFIG_NOT_FOUND

    binary = find_gitleaks()
    if binary is None:
        print(
            "gitleaks was not found. The secret scan fails closed rather than passing "
            "without having checked anything.\n"
            "  Windows: winget install Gitleaks.Gitleaks\n"
            "  macOS:   brew install gitleaks\n"
            "  Linux:   https://github.com/gitleaks/gitleaks/releases\n"
            "Or set GITLEAKS_BIN to the binary path.",
            file=sys.stderr,
        )
        return CONFIG_NOT_FOUND

    result = subprocess.run(build_command(binary, staged=args.staged), check=False)
    if result.returncode != 0:
        print(
            "\nSecret scan failed. Do not amend the commit to hide the value: once it is in "
            "the object database it must be treated as compromised and rotated.",
            file=sys.stderr,
        )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
