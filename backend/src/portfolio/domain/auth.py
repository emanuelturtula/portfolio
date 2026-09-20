"""Session arithmetic: how long a session lives, and when a token is still good.

Pure by construction. There is no clock call anywhere in this module -- every function
that needs "now" takes it as an argument -- which is what makes a session that expires in
thirty days testable without waiting thirty days, and what keeps the rule in one place
instead of spread across a repository and a middleware.

A session has two independent expiries and needs **both** to hold:

* the *absolute* expiry is written once, when the session is created, and nothing moves
  it. It is the ceiling: a stolen cookie is useless after it, no matter how busy the
  thief keeps the session;
* the *idle* window slides forward with activity. It is what logs out a browser left open
  on a machine that is no longer the owner's.

Only the SHA-256 hash of a token is ever stored, so a leaked database file hands the
reader no usable cookie. SHA-256 rather than Argon2id is deliberate and is not the same
decision as the one made for passwords: a token is 32 bytes from a CSPRNG, so there is
nothing to guess offline, and the hash only has to be preimage resistant. Running a
memory-hard hash on every authenticated request would add its full cost to every page
load.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from datetime import datetime

# `secrets.token_urlsafe(32)` is 32 bytes of entropy rendered as 43 base64url characters.
# The draw itself is a CSPRNG call and therefore lives in the service layer; the size is
# part of the session rule, so it is named here.
SESSION_TOKEN_BYTES: Final = 32

# How stale `last_seen_at` must be before a read turns into a write. A page that polls
# every few seconds would otherwise make every authenticated read a database write, to
# move a timestamp that is compared against a seven day window. One minute of resolution
# loss against that window is nothing; the write amplification is not.
LAST_SEEN_REFRESH_INTERVAL: Final = timedelta(seconds=60)


def hash_token(token: str) -> str:
    """Return the lowercase hex SHA-256 of a session token.

    Deterministic, unsalted and fast, all three on purpose: the value is looked up by
    equality on a unique index on every authenticated request.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionLifetime:
    """The two windows that together decide whether a session is still valid."""

    idle: timedelta
    absolute: timedelta

    @classmethod
    def from_days(cls, *, idle_days: int, absolute_days: int) -> SessionLifetime:
        """Build a lifetime from the two settings, which are expressed in whole days."""
        return cls(idle=timedelta(days=idle_days), absolute=timedelta(days=absolute_days))

    def absolute_expiry(self, created_at: datetime) -> datetime:
        """The hard ceiling for a session created at `created_at`. Never recomputed."""
        return created_at + self.absolute

    def is_valid(self, *, now: datetime, last_seen_at: datetime, expires_at: datetime) -> bool:
        """Whether a session is usable: inside the ceiling *and* inside the idle window."""
        return now < expires_at and now - last_seen_at < self.idle


def should_refresh_last_seen(
    *,
    now: datetime,
    last_seen_at: datetime,
    interval: timedelta = LAST_SEEN_REFRESH_INTERVAL,
) -> bool:
    """Whether this request should pay for a write to slide the idle window."""
    return now - last_seen_at >= interval
