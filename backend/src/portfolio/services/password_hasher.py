"""The Argon2id wrapper: one place that knows how a password becomes a stored string.

Not pure, and it could not be: every hash draws a fresh salt from the CSPRNG, so the same
password hashes differently every time. That is the point -- it is what makes a stolen
database a set of independent problems rather than one.

Argon2id rather than bcrypt or PBKDF2 because it is memory hard: the attacker's advantage
from custom silicon is bounded by memory bandwidth rather than by clock speed. The three
cost parameters are settings, not constants, because the only number that matters is the
one measured on the machine this runs on -- a Raspberry Pi 5, not a CI runner and not a
laptop. `python -m portfolio hash-benchmark` is how that measurement is taken.

The OWASP floor (`memory_cost >= 19456` KiB, `time_cost >= 2`) lives in
`domain.passwords` with the rest of the password policy. A production process refuses to
start below it, and a test pins the shipped defaults against it, so that a future
"logging in feels slow" change cannot quietly drop the parameters to the library's
minimum.
"""

from __future__ import annotations

import secrets
from functools import cached_property
from typing import Final

from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2 import Type
from argon2.exceptions import InvalidHashError, VerificationError

# Argon2id defaults that argon2-cffi does not expose as constants. Named so that a hash
# written today can be read back by a version that changes its own defaults.
HASH_LENGTH: Final = 32
SALT_LENGTH: Final = 16


class PasswordHasher:
    """Hash and verify passwords with Argon2id at the configured cost.

    One instance per process rather than one per request: `dummy_hash` is computed once,
    lazily, and reused, which is what lets a login against an unknown username perform the
    same work as one against a known one.
    """

    def __init__(self, *, time_cost: int, memory_cost: int, parallelism: int) -> None:
        self.time_cost = time_cost
        self.memory_cost = memory_cost
        self.parallelism = parallelism
        self._hasher = Argon2PasswordHasher(
            time_cost=time_cost,
            memory_cost=memory_cost,
            parallelism=parallelism,
            hash_len=HASH_LENGTH,
            salt_len=SALT_LENGTH,
            type=Type.ID,
        )

    def hash(self, password: str) -> str:
        """Return the PHC-format encoded hash: algorithm, parameters, salt and digest."""
        return self._hasher.hash(password)

    def verify(self, encoded_hash: str, password: str) -> bool:
        """Whether a password matches an encoded hash.

        A wrong password, a corrupted hash and a hash written by another algorithm are all
        the same answer here -- `False` -- because a caller that has to tell them apart
        will eventually tell a client apart too, and that is the distinction an attacker
        wants. The exceptions are caught rather than propagated for that reason.
        """
        try:
            return self._hasher.verify(encoded_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, encoded_hash: str) -> bool:
        """Whether this hash was written at a cost lower than the one configured now.

        Raising the parameters on the Pi has to take effect for the owner's existing
        password, and the only moment the plaintext is available to rehash it is the next
        successful login.
        """
        return self._hasher.check_needs_rehash(encoded_hash)

    @cached_property
    def dummy_hash(self) -> str:
        """An encoded hash of a random password, for verifying against a user that is absent.

        Login must take the same time whether or not the username exists, and the only
        reliable way to spend the same time is to do the same work. The password hashed
        here is drawn from the CSPRNG and then discarded, so nothing can match it.

        Computed on first use rather than at import: the parameters come from settings,
        and paying ~250 ms at import would tax every process start and every test session
        for something only an unknown-username login needs.
        """
        return self.hash(secrets.token_urlsafe(32))
