"""Guardrail tests for the agent hook commands in ``.claude/settings.json``.

Hooks run with the agent's current working directory, not the repository root, and the
``Bash`` tool's working directory persists between calls. The README and ``CLAUDE.md`` both
tell people to run ``cd backend && uv run ...``, so a hook command written as a
repository-relative path stops resolving the moment anyone follows those instructions.

That failure is not cosmetic. The ``PreToolUse`` guards fail closed: once the interpreter
cannot find the script, every subsequent ``Bash`` call is refused, and the session cannot
recover because the hook fires before the ``cd`` that would fix it.

Claude Code exports ``CLAUDE_PROJECT_DIR`` for exactly this reason, and it resolves to the
worktree root when the session runs in a git worktree. These tests fail if a hook command
ever goes back to a relative path.

Plain ``unittest``, no dependencies, so this runs even when the backend does not install.
"""

from __future__ import annotations

import json
import shlex
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS = REPO_ROOT / ".claude" / "settings.json"

ANCHOR = "$CLAUDE_PROJECT_DIR"
# The path may one day contain a space -- worktree directories are named for their branch.
QUOTED_ANCHOR = f'"{ANCHOR}/'


def hook_commands() -> list[tuple[str, str]]:
    """Return every configured hook as ``(event, command)``."""
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    commands: list[tuple[str, str]] = []
    for event, matchers in settings.get("hooks", {}).items():
        for matcher in matchers:
            for hook in matcher.get("hooks", []):
                if hook.get("type") == "command":
                    commands.append((event, hook["command"]))
    return commands


def script_arguments(command: str) -> list[str]:
    """Return the arguments of a command that name a Python script."""
    return [argument for argument in shlex.split(command) if argument.endswith(".py")]


class HookCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.commands = hook_commands()

    def test_there_are_hooks_to_check(self) -> None:
        # Guards against this whole file passing vacuously if the settings are restructured.
        self.assertTrue(self.commands, f"no command hooks found in {SETTINGS}")

    def test_every_hook_runs_a_script(self) -> None:
        for event, command in self.commands:
            with self.subTest(event=event, command=command):
                self.assertEqual(
                    len(script_arguments(command)),
                    1,
                    "a hook command should invoke exactly one script",
                )

    def test_every_hook_script_is_anchored_to_the_project_directory(self) -> None:
        for event, command in self.commands:
            with self.subTest(event=event, command=command):
                script = script_arguments(command)[0]
                self.assertTrue(
                    script.startswith(f"{ANCHOR}/"),
                    f"{script!r} is relative to the working directory, which is not the "
                    f"repository root when an agent has run 'cd backend'. Invoke it "
                    f'through {QUOTED_ANCHOR}...\" instead.',
                )

    def test_every_hook_script_path_is_quoted(self) -> None:
        for event, command in self.commands:
            with self.subTest(event=event, command=command):
                self.assertIn(
                    QUOTED_ANCHOR,
                    command,
                    "quote the script path: a worktree directory may contain a space",
                )

    def test_every_hook_script_exists(self) -> None:
        for event, command in self.commands:
            with self.subTest(event=event, command=command):
                script = script_arguments(command)[0]
                relative = script[len(ANCHOR) + 1 :]
                self.assertTrue(
                    (REPO_ROOT / relative).is_file(),
                    f"{relative} does not exist; the hook would fail closed on every call",
                )


if __name__ == "__main__":
    unittest.main()
