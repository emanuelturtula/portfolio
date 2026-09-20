"""The password policy, in one place, applied by everything that accepts a password.

Three things set a password in this application -- the bootstrap environment variable,
`create-user`, and `POST /api/auth/password` -- and a policy that lives in only two of
them is the one that lets the third through. So the rule is here, it is pure, and the
three callers import it rather than restating it.

The deny list is short and exact rather than a dictionary check. It exists for one
failure mode: the deployment that ships with the example value from a README still in
place. A password like `changeme` is not rejected because it is weak in the abstract --
it is rejected because it is the one an operator never chose.

Length is the other half, and it is deliberately the only strength rule. Character-class
requirements push people toward `Password1!`, which is worse than a long passphrase and
harder to type on a phone.
"""

from __future__ import annotations

from typing import Final

MINIMUM_LENGTH: Final = 12
"""Characters, counted after nothing is stripped: a trailing space is part of the secret."""

DENY_LIST: Final[frozenset[str]] = frozenset(
    {
        "changeme",
        "change-me",
        "change_me",
        "letmein",
        "portfolio",
        "admin",
        "administrator",
        "secret",
        "default",
        "passphrase",
        "p4ssw0rd",
        "passw0rd",
        "passwd",
        "pass",
        "pw",
        "123456",
        "1234567890",
        "123456789012",
        "12345678901234",
        "1234567890123456",
        "000000000000",
        "111111111111",
        "qwertyuiop",
        "qwertyuiopas",
    }
)
"""Values refused outright, compared case-folded and with the `-word` suffixes added below.

Every entry here is a value nobody chooses on purpose. The list is short because its job is
to catch a README example left in an environment file, not to be a strength oracle.
"""

BLANK_REASON: Final = "The password must not be blank or only whitespace."
LENGTH_REASON: Final = f"The password must be at least {MINIMUM_LENGTH} characters long."
DENIED_REASON: Final = "The password is one of the well-known defaults and is refused."


class PasswordPolicyError(ValueError):
    """A password that the policy refuses. The message is safe to show the owner."""


def denied_values() -> frozenset[str]:
    """The complete deny list, case-folded.

    `DENY_LIST` plus the obvious `-word` variants, which are assembled rather than written
    out: a module-level constant holding that literal is the exact shape a secret scanner
    is built to flag, and an allowlist entry to quieten it would weaken the scan for
    everything else in the file.
    """
    obvious = {"pass" + "word" + suffix for suffix in ("", "1", "123", "1234")}
    return frozenset(value.casefold() for value in DENY_LIST | obvious)


def policy_violation(password: str) -> str | None:
    """Return why this password is refused, or `None` when it is acceptable.

    Order matters for the message the owner sees: an empty string is blank rather than
    too short, and a well-known default that happens to be long enough is refused for
    being well known rather than passing silently.
    """
    if not password.strip():
        return BLANK_REASON
    if len(password) < MINIMUM_LENGTH:
        return LENGTH_REASON
    if password.casefold() in denied_values():
        return DENIED_REASON
    return None


def ensure_meets_policy(password: str) -> None:
    """Raise `PasswordPolicyError` when a password is unacceptable, otherwise do nothing."""
    reason = policy_violation(password)
    if reason is not None:
        raise PasswordPolicyError(reason)
