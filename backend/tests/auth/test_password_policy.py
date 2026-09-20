"""The password policy itself: pure, and applied identically by all three callers.

Three things set a password -- the bootstrap variable, `create-user`, and the password
endpoint -- and each of those has its own test that the policy is applied. These are the
tests of what the policy actually says.
"""

from __future__ import annotations

import pytest

from portfolio.domain.passwords import (
    BLANK_REASON,
    DENIED_REASON,
    LENGTH_REASON,
    MINIMUM_LENGTH,
    PasswordPolicyError,
    denied_values,
    ensure_meets_policy,
    policy_violation,
)


@pytest.mark.parametrize("value", ["", "   ", "\t\n", " " * (MINIMUM_LENGTH + 5)])
def test_a_blank_password_is_refused_before_it_is_measured(value: str) -> None:
    """Whitespace long enough to pass the length check is still blank.

    The order of the checks is what this asserts: a run of twenty spaces satisfies the
    length rule, so measuring first would let it through with no message worth reading.
    """
    assert policy_violation(value) == BLANK_REASON


@pytest.mark.parametrize("length", [1, MINIMUM_LENGTH - 1])
def test_a_short_password_is_refused(length: int) -> None:
    """Length is the only strength rule, deliberately.

    Character-class requirements push people toward `Password1!`, which is weaker than a
    long passphrase and harder to type on a phone.
    """
    assert policy_violation("a" * length) == LENGTH_REASON


def test_a_password_of_exactly_the_minimum_length_is_accepted() -> None:
    """The boundary, in the direction that a `<=` typo would break."""
    assert policy_violation("abcdefghijkl") is None
    assert len("abcdefghijkl") == MINIMUM_LENGTH


@pytest.mark.parametrize("value", sorted(denied_values()))
def test_every_denied_value_is_refused_whatever_its_case(value: str) -> None:
    """The deny list is compared case-folded, so capitalising a default does not help.

    Short entries would be caught by the length rule anyway; the ones that matter are the
    long digit runs, which pass every other check.
    """
    for spelling in (value, value.upper(), value.capitalize()):
        assert policy_violation(spelling) in {DENIED_REASON, LENGTH_REASON}

    padded = value + "x" * MINIMUM_LENGTH
    assert policy_violation(padded) is None, "the list is exact, not a substring match"


def test_a_long_digit_run_is_refused_although_it_is_long_enough() -> None:
    """The case the length rule cannot catch, and the reason the list exists at all."""
    assert policy_violation("123456789012") == DENIED_REASON


def test_an_acceptable_passphrase_is_accepted() -> None:
    """A policy that refused everything would pass every test above."""
    assert policy_violation("a correct horse battery staple") is None


def test_ensure_raises_with_the_reason_as_its_message() -> None:
    """The message reaches the operator and the API response, so it has to be the reason."""
    with pytest.raises(PasswordPolicyError, match="12 characters"):
        ensure_meets_policy("short")


def test_ensure_is_silent_on_an_acceptable_password() -> None:
    """Silence is the success value: an acceptable password raises nothing at all."""
    ensure_meets_policy("a correct horse battery staple")
