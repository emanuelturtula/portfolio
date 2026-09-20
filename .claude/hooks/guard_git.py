"""Block git commands that would bypass this project's safety rails.

Wired as a ``PreToolUse`` hook on ``Bash``. The rules it enforces are the ones the
repository's own configuration cannot: the branch ruleset stops a direct push to ``main``
on the server, but an agent can still waste a long run getting there, and ``--no-verify``
silently skips the secret scan on a public repository.

Exits 2 with an explanation to deny the command.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bgit\s+(?:\w+\s+)*(?:--no-verify|-n)\b(?!.*\bgit\s+clean)"),
        "--no-verify skips the pre-commit secret scan. On a public repository that is the "
        "one hook you cannot afford to skip. Fix what the hook reported instead.",
    ),
    (
        re.compile(r"\bgit\s+push\b.*(?:--force\b|--force-with-lease\b|\s-f\b)"),
        "A force push rewrites history. If a secret was committed, force-pushing does not "
        "remove it from the remote's object database, it only hides it -- the value must "
        "be rotated. If you are trying to clean up commits, rebase locally and push "
        "normally.",
    ),
    (
        re.compile(r"\bgit\s+push\b.*\borigin\s+(?:HEAD:)?main\b"),
        "Direct pushes to main are blocked by the branch ruleset. Open a pull request: "
        "branch, push the branch, then 'gh pr create'.",
    ),
    (
        re.compile(r"\bgh\s+pr\s+merge\b"),
        "Only the repository owner merges. Open the pull request and report its URL.",
    ),
    (
        re.compile(r"\bgit\s+stash\b(?!\s+(?:push|list|show))"),
        "A bare 'git stash' or 'git stash pop' shares one stack with every other worktree "
        "and session on this machine, so it can silently swallow or restore someone "
        "else's work. Make a temporary commit instead.",
    ),
]

PROTECTED_BRANCH = "main"


def current_branch() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout.strip()


def deny(message: str) -> int:
    print(f"Refused: {message}", file=sys.stderr)
    return 2


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # Fail open: the server-side ruleset is still behind this.

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0
    command = str(tool_input.get("command", ""))
    if not command:
        return 0

    for pattern, message in RULES:
        if pattern.search(command):
            return deny(message)

    # Committing straight onto main wastes a whole run: the push is rejected at the end.
    if re.search(r"\bgit\s+commit\b", command) and current_branch() == PROTECTED_BRANCH:
        return deny(
            f"You are on {PROTECTED_BRANCH}. Every change lands through a pull request: "
            "create a feature/<issue>-<slug> branch first."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
