"""Block an agent from writing sensitive values into a file.

This repository is public. gitleaks catches a secret at commit time and CI catches it at
push time, but by then the value is already in the working tree and, once committed, in the
object database forever. Agents write most of the code here, so the cheapest place to stop
this is the moment the write is attempted.

Wired as a ``PreToolUse`` hook on ``Write|Edit``. Reads the hook payload on stdin and exits
2 with an explanation to deny the write.

Testnet addresses and extended keys are deliberately allowed: test fixtures are required to
use them.
"""

from __future__ import annotations

import json
import re
import sys

# Each pattern is paired with the reason a human needs to hear, because a denial with no
# explanation just gets worked around.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "a Bitcoin mainnet address",
        re.compile(r"\bbc1[02-9ac-hj-np-z]{11,71}\b"),
    ),
    (
        "a Bitcoin mainnet address",
        re.compile(r"(?:^|[^A-Za-z0-9/])([13][a-km-zA-HJ-NP-Z1-9]{25,34})(?:[^A-Za-z0-9]|$)"),
    ),
    (
        "a Kaspa mainnet address",
        re.compile(r"\bkaspa:[qp][a-z0-9]{59,}\b"),
    ),
    (
        "a mainnet extended public key, which reveals every address of a wallet",
        re.compile(r"\b(?:xpub|ypub|zpub)[1-9A-HJ-NP-Za-km-z]{100,112}\b"),
    ),
    (
        "a private LAN IP address, which is infrastructure detail",
        re.compile(
            r"\b(?:10(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3}"
            r"|192\.168(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){2}"
            r"|172\.(?:1[6-9]|2[0-9]|3[01])(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){2})\b"
        ),
    ),
    (
        "a Tailscale tailnet address",
        re.compile(r"\b100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])"
                   r"(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){2}\b"),
    ),
    (
        "a Tailscale key",
        re.compile(r"\btskey-[a-z]+-[A-Za-z0-9-]{16,}"),
    ),
    (
        "an exchange API credential",
        re.compile(
            r"(?i)\b(?:bitget|bingx)[_-]?(?:api[_-]?)?(?:key|secret|passphrase)\b"
            r"\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{16,})['\"]?"
        ),
    ),
    (
        "a CoinGecko API key",
        re.compile(r"\bCG-[A-Za-z0-9]{20,}\b"),
    ),
    (
        "a private key block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
]

# The security tests and the scanner configuration necessarily contain these patterns.
EXEMPT_PATHS = (
    ".gitleaks.toml",
    "guard_sensitive_write.py",
    "secret_scan.py",
)


def content_of(tool_input: dict[str, object]) -> str:
    """Collect every string an Edit or Write call would put into the file."""
    parts: list[str] = []
    for key in ("content", "new_string"):
        value = tool_input.get(key)
        if isinstance(value, str):
            parts.append(value)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                value = edit.get("new_string")
                if isinstance(value, str):
                    parts.append(value)
    return "\n".join(parts)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Fail open on a malformed payload: gitleaks and CI are still behind this. Blocking
        # every write because the hook could not parse its own input would be worse.
        return 0

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    path = str(tool_input.get("file_path", ""))
    if any(path.endswith(exempt) for exempt in EXEMPT_PATHS):
        return 0

    content = content_of(tool_input)
    if not content:
        return 0

    for reason, pattern in PATTERNS:
        if pattern.search(content):
            print(
                f"Refused: this write contains what looks like {reason}.\n"
                f"  File: {path or '(unknown)'}\n\n"
                "This repository is public. Wallet addresses and credentials are runtime "
                "user data: they belong in the database on the host or in an environment "
                "variable, never in the repository.\n"
                "Test fixtures must use testnet values instead: tb1..., bcrt1..., "
                "kaspatest:..., tpub....",
                file=sys.stderr,
            )
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
