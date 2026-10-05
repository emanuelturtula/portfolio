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
a working-tree gitleaks scan would find it. The file is also named so that it does not end
in ``guard_sensitive_write.py``, a suffix the guard exempts, which means the guard inspects
every write to this file like any other.

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


def key_shaped(prefix: str, length: int = REAL_BODY_LENGTH) -> str:
    """Build a key-shaped string at run time, cycling the whole Base58 alphabet."""
    return "".join((prefix, *itertools.islice(itertools.cycle(BASE58), length)))


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


class TheRealScript(unittest.TestCase):
    """Claude Code reads the verdict from the exit code, so run the file as it does."""

    def run_script(self, content: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=payload(write(content)),
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


if __name__ == "__main__":
    unittest.main()
