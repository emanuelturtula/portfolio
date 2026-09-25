"""Behaviour tests for the ``PreToolUse`` git guard, ``.claude/hooks/guard_git.py``.

The guard refuses ``--no-verify`` because it skips the secret scan the git hooks run.
``-n`` is its short form only for ``git commit`` and ``git am``. For most other
subcommands git documents ``-n`` as something harmless, and refusing it there blocks
ordinary read-only work such as ``git grep -n``. Both directions are pinned here: every
bypass stays refused, and every harmless ``-n`` is let through.

A refusal only counts if it is for the right reason. Each refused case must name
``--no-verify`` in its message, so a command caught by an unrelated rule cannot pass for
one caught by this one.

The hook is loaded from its file and driven through ``main()`` exactly as Claude Code
drives it, JSON on stdin and a verdict in the exit code, but in-process, because a
subprocess per case would add seconds to the gate the agents run on every turn. Its
separate "committing on main" check is pinned to a feature branch so that it cannot
refuse a ``git commit`` case for a reason of its own. One test runs the real script as a
subprocess to prove the exit-code contract end to end.

Plain ``unittest``, no dependencies, so this runs even when the backend does not install.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / ".claude" / "hooks" / "guard_git.py"

REFUSED = 2
ALLOWED = 0

# Each of these hands git a --no-verify, in one spelling or another. See #66.
BYPASSES = [
    # The forms the issue names.
    "git commit -n",
    "git commit -an -m x",
    "git commit --no-verify",
    "git -c x=y commit -n",
    "git push --no-verify",
    # -n on its own, combined with other short flags, or after a value.
    "git commit -n -m x",
    "git commit -nm x",
    "git commit -anm x",
    "git commit -vn -m x",
    "git commit -m x -n",
    "git commit -m x file.txt -n",
    "git commit -Fmsg.txt -n",
    'git commit -m"note" -n',
    "git commit --amend --no-edit -n",
    # --no-verify after a value, and the prefixes of it that git accepts.
    "git commit -m x --no-verify",
    "git commit --no-verif -m x",
    "git commit --no-veri -m x",
    "git push --no-verif origin b",
    "git am --no-v patch.mbox",
    # Global options before the subcommand. -nm rather than -n, so that misreading which
    # word is the subcommand lets the case through instead of being caught by the fallback.
    "git -C . commit -nm x",
    "git -c x.y=z commit -nm x",
    "git --no-pager commit -nm x",
    "git -p commit -nm x",
    "git --git-dir .git commit -nm x",
    "git --git-dir=.git commit -nm x",
    "git --work-tree . commit -nm x",
    "git --namespace ns commit -nm x",
    "git --config-env x.y=HOME commit -nm x",
    "git --attr-source HEAD commit -nm x",
    "/usr/bin/git commit -n -m x",
    # git am: -n is --no-verify there too, and its -m, -c, -k and -3 take no value.
    "git am -n patch.mbox",
    "git am --no-verify patch.mbox",
    "git am -kn patch.mbox",
    "git am -mn patch.mbox",
    "git am -cn patch.mbox",
    "git am -3n patch.mbox",
    "git am -C1 -n patch.mbox",
    # Quoting that the shell removes before git sees the word.
    "git commit $'-n' -m x",
    'git commit -"n" -m x',
    "git commit \\-n -m x",
    # An operator inside quotes is part of a word, and does not end the command.
    'git commit -m ";" -n',
    "git commit -m 'a && b' -n",
    # A redirection does not end the command either.
    "git commit -m x 2>&1 -n",
    # Inside a compound command, a command substitution or a nested shell.
    "cd backend && git commit -n -m x",
    "git commit -n -m x && git clean -f",
    "git status; git commit -n -m x",
    "git status\ngit commit -n -m x",
    "echo $(git commit -n -m x)",
    "echo `git commit -n -m x`",
    "bash -c 'git commit -n -m x'",
    'sh -c "git commit --no-verify -m x"',
    "GIT_AUTHOR_NAME=x git commit -n -m x",
    # merge, rebase and pull have no short form of it, but the long form still bypasses.
    "git merge --no-verify feature",
    "git rebase --no-verify main",
    "git pull --no-verify",
    # A subcommand the guard cannot vouch for, such as an alias, keeps the old rule.
    "git ci -n -m x",
]

# None of these skips a git hook.
HARMLESS = [
    # The forms the issue names: -n is --line-number, --max-count and --dry-run here.
    "git grep -n x",
    "git log -n 5",
    "git push -n origin b",
    # Every other subcommand where git documents a meaning for -n that is not --no-verify.
    "git add -n .",
    "git blame -n README.md",
    "git check-ignore -n x",
    "git cherry-pick -n abc123",
    "git clean -n",
    "git clean -nd",
    "git clone -n https://example.invalid/repo.git",
    "git fetch -n",
    "git format-patch -n -1",
    "git merge -n feature",
    "git mv -n a b",
    "git notes prune -n",
    "git prune -n",
    "git pull -n",
    "git rebase -n main",
    "git reflog expire -n --all",
    "git repack -n",
    "git rev-list -n 1 HEAD",
    "git revert -n abc123",
    "git rm -n x",
    "git shortlog -n",
    "git submodule summary -n 3",
    "git tag -n5 -l",
    "git worktree prune -n",
    # Global options before a harmless subcommand.
    "git -C backend grep -n x",
    "git --no-pager log -n 5",
    "git -c color.ui=never grep -n x",
    "git --git-dir .git log -n 5",
    "git --work-tree . grep -n x",
    # A letter that takes a value swallows the rest of its word, so each n is that value.
    "git commit -mn",
    "git commit -amn",
    'git commit -m"note"',
    "git commit -m -n",
    "git commit -m '-n is harmless in grep'",
    "git commit -uno -m x",
    "git am -Cn patch.mbox",
    "git am -p1n patch.mbox",
    # A short -n cluster on a subcommand with no -n of its own is left alone, as before.
    "git status -uno",
    # After --, a word is a path or a pattern, never an option.
    "git commit -m x -- -n",
    "git grep -n -- --no-verify",
    # A message or a title that only mentions a harmless -n.
    "git commit -m 'fix: stop refusing git grep -n'",
    "gh pr create --title 'fix(hooks): stop refusing git grep -n'",
    # The intent of the old git clean exception: its -n is a dry run.
    "git commit -m x; git clean -n",
    # Commands that are not git.
    "grep -rn x backend",
    "head -n 5 README.md",
    "echo -n x | git commit -F -",
    "git status && echo -n done",
    "git status\nhead -n 5 README.md",
]


def load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard_git", HOOK)
    assert spec is not None and spec.loader is not None, HOOK
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


class GuardGitTests(unittest.TestCase):
    hook: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        cls.hook = load_hook()

    def run_hook(self, command: str) -> tuple[int, str]:
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", io.StringIO(payload(command))),
            mock.patch.object(self.hook, "current_branch", return_value="feature/0-test"),
            contextlib.redirect_stderr(stderr),
        ):
            code = self.hook.main()
        return code, stderr.getvalue()

    def assert_refused_as_no_verify(self, command: str) -> None:
        code, message = self.run_hook(command)
        self.assertEqual(code, REFUSED, f"{command!r} skips the git hooks but was allowed")
        self.assertIn("--no-verify", message, f"{command!r} was refused for another reason")


class BypassesAreRefused(GuardGitTests):
    def test_there_are_bypasses_to_check(self) -> None:
        # Guards against the loop below passing vacuously.
        self.assertGreater(len(BYPASSES), 40)

    def test_every_bypass_is_refused(self) -> None:
        for command in BYPASSES:
            with self.subTest(command=command):
                self.assert_refused_as_no_verify(command)

    def test_a_here_document_does_not_hide_a_bypass_after_it(self) -> None:
        command = "cat > notes.txt <<'EOF'\nharmless\nEOF\ngit commit -n -m x"
        self.assert_refused_as_no_verify(command)

    def test_an_unterminated_quote_does_not_hide_a_bypass(self) -> None:
        # To the shell the apostrophe is inside a comment. Read naively, it opens a quote
        # that swallows the next line.
        self.assert_refused_as_no_verify("git status # don't\ngit commit -n -m x")


class HarmlessShortNIsAllowed(GuardGitTests):
    def test_there_are_harmless_commands_to_check(self) -> None:
        self.assertGreater(len(HARMLESS), 40)

    def test_every_harmless_command_is_allowed(self) -> None:
        for command in HARMLESS:
            with self.subTest(command=command):
                code, message = self.run_hook(command)
                self.assertEqual(code, ALLOWED, f"{command!r} was refused: {message}")

    def test_a_commit_message_in_a_here_document_is_not_inspected(self) -> None:
        # Commit messages that discuss the rule are written this way on purpose.
        command = "git commit -F - <<'EOF'\nfix: refuse git commit -n and --no-verify\nEOF"
        code, message = self.run_hook(command)
        self.assertEqual(code, ALLOWED, message)


class TheRealScript(unittest.TestCase):
    """Claude Code reads the verdict from the exit code, so run the file as it does."""

    def run_script(self, command: str) -> subprocess.CompletedProcess[str]:
        # Outside any repository, so the "committing on main" check cannot interfere.
        with tempfile.TemporaryDirectory() as outside:
            return subprocess.run(
                [sys.executable, str(HOOK)],
                input=payload(command),
                capture_output=True,
                text=True,
                cwd=outside,
                timeout=30,
                check=False,
            )

    def test_a_bypass_exits_2_with_the_reason(self) -> None:
        result = self.run_script("git commit -n -m x")
        self.assertEqual(result.returncode, REFUSED, result.stderr)
        self.assertIn("--no-verify", result.stderr)

    def test_a_harmless_command_exits_0_silently(self) -> None:
        result = self.run_script("git grep -n x")
        self.assertEqual(result.returncode, ALLOWED, result.stderr)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
