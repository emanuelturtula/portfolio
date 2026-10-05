"""Behaviour tests for the ``PreToolUse`` write guard, ``.claude/hooks/guard_sensitive_write.py``.

The guard refuses a write that would put an extended private key into the repository, on
every network: mainnet and test, single-signature and multisig. A private key controls the
funds of a wallet, so unlike an extended public key there is no testnet form a fixture may
use. The other direction is pinned too: a ``tpub``, ``upub`` or ``vpub`` is what the
fixtures for spec 031 are required to use, and refusing one would block that work.

A refusal only counts if it is for the right reason. Each refused case must name the
private-key rule in its message, so a write caught by an unrelated rule cannot pass for one
caught by this one.

No key-shaped string exists in this file. Every candidate is assembled at run time from a
prefix and a generated body, so the shape is in neither the source nor its ``.pyc``, where
a working-tree gitleaks scan would find it.

The guard exempts three files, the gitleaks configuration and the two scanners, because they
must contain the very patterns it refuses. It recognises them by their whole file name, and
that is pinned in both directions: the three stay exempt whichever separator the path uses,
and a file whose name merely ends in one of theirs, such as ``test_guard_sensitive_write.py``,
is inspected like any other. Until that was fixed the exemption was a suffix match, which is
why this file is not called ``test_guard_sensitive_write.py`` itself.

The hook is loaded from its file and driven through ``main()`` exactly as Claude Code
drives it, JSON on stdin and a verdict in the exit code, but in-process. One test runs the
real script as a subprocess to prove the exit-code contract end to end.

Plain ``unittest``, no dependencies, so this runs even when the backend does not install.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import itertools
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / ".claude" / "hooks" / "guard_sensitive_write.py"

REFUSED = 2
ALLOWED = 0

PRIVATE_KEY_REASON = "an extended private key"
PUBLIC_KEY_REASON = "a mainnet extended public key"

# Every SLIP-0132 private prefix, by family. Spec 031, R2.
PRIVATE_PREFIXES = {
    "mainnet single-signature": ("xprv", "yprv", "zprv"),
    "test single-signature": ("tprv", "uprv", "vprv"),
    "mainnet multisig": ("Yprv", "Zprv"),
    "test multisig": ("Uprv", "Vprv"),
}

# The public prefixes test fixtures are required to use.
TEST_PUBLIC_PREFIXES = ("tpub", "upub", "vpub")

BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# A serialised extended key is 111 characters: the prefix and 107 more. The rules accept
# 100 to 112 after the prefix, and both bounds are pinned.
BODY_LENGTHS = (100, 107, 112)
REAL_BODY_LENGTH = 107

FIXTURE_PATH = "backend/tests/fixtures/keys.py"

# The files the guard exempts, where they sit in the repository.
EXEMPT_FILES = (
    ".gitleaks.toml",
    ".claude/hooks/guard_sensitive_write.py",
    "scripts/secret_scan.py",
)

# Names that end in an exempt file's name without being it. A suffix match exempted each one.
LOOK_ALIKES = (
    "tests/agent/test_guard_sensitive_write.py",
    "backend/tests/test_secret_scan.py",
    "notes.gitleaks.toml",
)


def key_shaped(prefix: str, length: int = REAL_BODY_LENGTH) -> str:
    """Build a key-shaped string at run time, cycling the whole Base58 alphabet."""
    return "".join((prefix, *itertools.islice(itertools.cycle(BASE58), length)))


def spellings(relative: str) -> dict[str, str]:
    """``relative`` spelled as each kind of path a ``Write`` or ``Edit`` call may carry.

    Claude Code on Windows sends absolute backslash paths, and a path can mix both
    separators when a relative one is appended to a Windows root.
    """
    parts = relative.split("/")
    return {
        "relative, forward slashes": "/".join(parts),
        "relative, backslashes": "\\".join(parts),
        "absolute POSIX": "/".join(("", "work", "portfolio", *parts)),
        "absolute Windows": "\\".join(("C:", "work", "portfolio", *parts)),
        "absolute Windows, mixed": "\\".join(("C:", "work", "portfolio", "/".join(parts))),
    }


def load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard_sensitive_write", HOOK)
    assert spec is not None and spec.loader is not None, HOOK
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(content: str, path: str = FIXTURE_PATH) -> dict[str, object]:
    return {"file_path": path, "content": content}


def payload(tool_input: dict[str, object]) -> str:
    return json.dumps({"tool_name": "Write", "tool_input": tool_input})


class GuardSensitiveWriteTests(unittest.TestCase):
    hook: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        cls.hook = load_hook()

    def run_hook(self, tool_input: dict[str, object]) -> tuple[int, str]:
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", io.StringIO(payload(tool_input))),
            contextlib.redirect_stderr(stderr),
        ):
            code = self.hook.main()
        return code, stderr.getvalue()

    def assert_refused_for(self, tool_input: dict[str, object], reason: str) -> None:
        code, message = self.run_hook(tool_input)
        self.assertEqual(code, REFUSED, f"{reason} was allowed")
        self.assertIn(reason, message, "refused, but for another reason")


class PrivateKeysAreRefused(GuardSensitiveWriteTests):
    def test_every_private_prefix_is_covered(self) -> None:
        # Guards against the loops below passing vacuously, or a family going missing.
        prefixes = [prefix for family in PRIVATE_PREFIXES.values() for prefix in family]
        self.assertEqual(len(prefixes), 10)
        self.assertEqual(len(set(prefixes)), 10)

    def test_a_private_key_is_refused_on_every_network(self) -> None:
        for family, prefixes in PRIVATE_PREFIXES.items():
            for prefix in prefixes:
                for length in BODY_LENGTHS:
                    with self.subTest(family=family, prefix=prefix, length=length):
                        content = f"KEY = {key_shaped(prefix, length)!r}\n"
                        self.assert_refused_for(write(content), PRIVATE_KEY_REASON)

    def test_a_private_key_in_an_edit_is_refused(self) -> None:
        tool_input = {
            "file_path": FIXTURE_PATH,
            "old_string": "KEY = None",
            "new_string": f"KEY = {key_shaped('tprv')!r}",
        }
        self.assert_refused_for(tool_input, PRIVATE_KEY_REASON)

    def test_a_private_key_in_one_of_several_edits_is_refused(self) -> None:
        tool_input = {
            "file_path": FIXTURE_PATH,
            "edits": [
                {"old_string": "a", "new_string": "harmless"},
                {"old_string": "b", "new_string": key_shaped("Vprv")},
            ],
        }
        self.assert_refused_for(tool_input, PRIVATE_KEY_REASON)


class PublicKeysAreAllowed(GuardSensitiveWriteTests):
    def test_a_test_network_public_key_is_allowed(self) -> None:
        for prefix in TEST_PUBLIC_PREFIXES:
            for length in BODY_LENGTHS:
                with self.subTest(prefix=prefix, length=length):
                    content = f"KEY = {key_shaped(prefix, length)!r}\n"
                    code, message = self.run_hook(write(content))
                    self.assertEqual(code, ALLOWED, message)

    def test_naming_a_private_prefix_is_allowed(self) -> None:
        # Spec 031 and the code that refuses a private key at registration both name them.
        content = "A key that starts with xprv, tprv or Vprv is refused as private_key.\n"
        code, message = self.run_hook(write(content, "docs/specs/031-bitcoin-extended-keys.md"))
        self.assertEqual(code, ALLOWED, message)

    def test_a_mainnet_public_key_is_still_refused(self) -> None:
        self.assert_refused_for(write(key_shaped("xpub")), PUBLIC_KEY_REASON)


class OnlyTheScannerFilesAreExempt(GuardSensitiveWriteTests):
    """The exemption matches a file's whole name, never a suffix of it."""

    content = f"KEY = {key_shaped('xprv')!r}\n"

    def test_the_content_is_refused_in_an_ordinary_file(self) -> None:
        # Without this, the exempt cases below would pass just as well if nothing refused
        # the content at all.
        self.assert_refused_for(write(self.content), PRIVATE_KEY_REASON)

    def test_the_exempt_names_are_exactly_those_of_the_scanner_files(self) -> None:
        names = [relative.rpartition("/")[2] for relative in EXEMPT_FILES]
        self.assertEqual(sorted(self.hook.EXEMPT_FILE_NAMES), sorted(names))
        for relative in EXEMPT_FILES:
            with self.subTest(file=relative):
                self.assertTrue((REPO_ROOT / relative).is_file(), f"{relative} has moved")

    def test_no_other_tracked_file_has_an_exempt_name(self) -> None:
        # The exemption follows a name wherever the file sits, so a second file with one of
        # these names would be exempt as well. Adding one has to be a deliberate act.
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.split("\0")
        names = self.hook.EXEMPT_FILE_NAMES
        exempt = [path for path in tracked if self.hook.file_name(path) in names]
        self.assertEqual(sorted(exempt), sorted(EXEMPT_FILES))

    def test_the_scanner_files_are_exempt_with_either_separator(self) -> None:
        for relative in EXEMPT_FILES:
            for spelling, path in spellings(relative).items():
                with self.subTest(file=relative, spelling=spelling):
                    code, message = self.run_hook(write(self.content, path))
                    self.assertEqual(code, ALLOWED, message)

    def test_a_name_that_only_ends_in_an_exempt_name_is_inspected(self) -> None:
        for relative in LOOK_ALIKES:
            for spelling, path in spellings(relative).items():
                with self.subTest(file=relative, spelling=spelling):
                    self.assert_refused_for(write(self.content, path), PRIVATE_KEY_REASON)

    def test_a_name_that_differs_only_in_case_is_inspected(self) -> None:
        # The suffix match was case-sensitive, so folding case would loosen the exemption.
        for relative in EXEMPT_FILES:
            directory, _, name = relative.rpartition("/")
            path = "/".join(part for part in (directory, name.upper()) if part)
            with self.subTest(path=path):
                self.assert_refused_for(write(self.content, path), PRIVATE_KEY_REASON)

    def test_a_look_alike_edit_is_inspected(self) -> None:
        tool_input = {
            "file_path": spellings(LOOK_ALIKES[0])["absolute Windows"],
            "old_string": "KEY = None",
            "new_string": self.content,
        }
        self.assert_refused_for(tool_input, PRIVATE_KEY_REASON)


class TheRealScript(unittest.TestCase):
    """Claude Code reads the verdict from the exit code, so run the file as it does."""

    def run_script(
        self, content: str, path: str = FIXTURE_PATH
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=payload(write(content, path)),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_a_private_key_exits_2_with_the_reason(self) -> None:
        result = self.run_script(key_shaped("tprv"))
        self.assertEqual(result.returncode, REFUSED, result.stderr)
        self.assertIn(PRIVATE_KEY_REASON, result.stderr)

    def test_a_test_network_public_key_exits_0_silently(self) -> None:
        result = self.run_script(key_shaped("tpub"))
        self.assertEqual(result.returncode, ALLOWED, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_a_look_alike_windows_path_exits_2_with_the_reason(self) -> None:
        path = spellings(LOOK_ALIKES[0])["absolute Windows"]
        result = self.run_script(key_shaped("tprv"), path)
        self.assertEqual(result.returncode, REFUSED, result.stderr)
        self.assertIn(PRIVATE_KEY_REASON, result.stderr)
        self.assertIn(path, result.stderr)

    def test_a_scanner_file_windows_path_exits_0_silently(self) -> None:
        path = spellings(EXEMPT_FILES[1])["absolute Windows"]
        result = self.run_script(key_shaped("tprv"), path)
        self.assertEqual(result.returncode, ALLOWED, result.stderr)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
