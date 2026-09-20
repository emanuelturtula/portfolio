"""Criterion 1: Argon2id, at parameters that are configurable but cannot be gutted."""

from __future__ import annotations

import re
from typing import Final

import pytest

from portfolio.config import Settings
from portfolio.services.password_hasher import (
    HASH_LENGTH,
    OWASP_MINIMUM_MEMORY_COST,
    OWASP_MINIMUM_TIME_COST,
    PasswordHasher,
)
from tests.auth.conftest import OWNER_PHRASE, WRONG_PHRASE

# The PHC string format Argon2 encodes into: algorithm, version, parameters, salt, digest.
PHC_PATTERN: Final = re.compile(r"^\$argon2id\$v=19\$m=(\d+),t=(\d+),p=(\d+)\$[^$]+\$[^$]+$")

# Cheap on purpose, for the tests that only need *a* hash. The floor below is asserted
# against the shipped defaults, which no environment override in this suite can reach.
FAST_PARAMETERS: Final[dict[str, int]] = {"time_cost": 1, "memory_cost": 64, "parallelism": 1}


@pytest.fixture
def hasher() -> PasswordHasher:
    """A hasher at deliberately cheap parameters."""
    return PasswordHasher(**FAST_PARAMETERS)


def test_hash_is_argon2id_with_configured_parameters() -> None:
    """The stored string names the algorithm and the cost it was produced at.

    That is what makes an upgrade possible at all: the parameters travel with the hash, so
    a value written at a lower cost can still be verified after the cost is raised.
    """
    hasher = PasswordHasher(time_cost=2, memory_cost=128, parallelism=2)

    encoded = hasher.hash(OWNER_PHRASE)

    match = PHC_PATTERN.match(encoded)
    assert match is not None, encoded
    assert (match.group(1), match.group(2), match.group(3)) == ("128", "2", "2")
    assert hasher.verify(encoded, OWNER_PHRASE)


def test_two_hashes_of_one_password_differ(hasher: PasswordHasher) -> None:
    """A fresh salt per hash: two accounts with one password must not look alike."""
    assert hasher.hash(OWNER_PHRASE) != hasher.hash(OWNER_PHRASE)


def test_configured_parameters_meet_the_owasp_floor() -> None:
    """The shipped defaults sit at or above OWASP's minimum configuration for Argon2id.

    Asserted against the field defaults rather than against a live `Settings()`, and that
    distinction is the whole value of the test: this suite lowers the parameters through
    the environment so it can hash thousands of times, and a check that read the live
    value would be a check this suite has already switched off.

    What it catches is the commit that makes logging in feel faster by editing the
    defaults -- which is not a performance fix, it is removing the control.
    """
    defaults = Settings.model_fields

    assert defaults["argon2_memory_cost"].default >= OWASP_MINIMUM_MEMORY_COST
    assert defaults["argon2_time_cost"].default >= OWASP_MINIMUM_TIME_COST
    assert defaults["argon2_parallelism"].default >= 1


def test_verify_rejects_a_wrong_password(hasher: PasswordHasher) -> None:
    """The obvious half, which would be embarrassing to leave unasserted."""
    encoded = hasher.hash(OWNER_PHRASE)

    assert hasher.verify(encoded, OWNER_PHRASE)
    assert not hasher.verify(encoded, WRONG_PHRASE)


@pytest.mark.parametrize("stored", ["", "not-a-hash", "$argon2id$broken", "$2b$12$abcdefgh"])
def test_verify_rejects_a_hash_it_cannot_parse(hasher: PasswordHasher, stored: str) -> None:
    """A corrupted or foreign hash is `False`, not an exception.

    A caller that had to tell "wrong password" from "unreadable hash" apart would
    eventually tell a client them apart too, and that difference is what an attacker is
    listening for.
    """
    assert not hasher.verify(stored, OWNER_PHRASE)


def test_hash_needs_update_when_parameters_rise(hasher: PasswordHasher) -> None:
    """Tuning on the Pi has to reach the password that is already stored."""
    encoded = hasher.hash(OWNER_PHRASE)
    assert not hasher.needs_rehash(encoded)

    raised = PasswordHasher(
        time_cost=FAST_PARAMETERS["time_cost"] + 1,
        memory_cost=FAST_PARAMETERS["memory_cost"],
        parallelism=FAST_PARAMETERS["parallelism"],
    )

    assert raised.needs_rehash(encoded)
    # Still verifiable at the old cost: the parameters travel with the hash.
    assert raised.verify(encoded, OWNER_PHRASE)


def test_the_dummy_hash_is_computed_once_and_matches_nothing(hasher: PasswordHasher) -> None:
    """What the unknown-username path verifies against, so that both paths do equal work.

    Cached, because recomputing it per request would double the cost of a failed login;
    random, because anything derived from a constant would be a hash an attacker could
    recognise in a database dump.
    """
    first = hasher.dummy_hash

    assert first is hasher.dummy_hash
    assert PHC_PATTERN.match(first) is not None
    assert not hasher.verify(first, OWNER_PHRASE)
    assert PasswordHasher(**FAST_PARAMETERS).dummy_hash != first


def test_the_digest_length_is_pinned(hasher: PasswordHasher) -> None:
    """Named constants, because a library default that moves would orphan every hash."""
    encoded = hasher.hash(OWNER_PHRASE)
    digest = encoded.rsplit("$", 1)[-1]

    # Base64 without padding: four characters per three bytes, rounded up.
    assert len(digest) == (HASH_LENGTH * 4 + 2) // 3
