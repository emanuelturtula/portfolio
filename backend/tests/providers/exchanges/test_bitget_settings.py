"""Criterion 8 of #13, and the protocol check of criterion 9.

The Bitget credentials are three settings, all or none, never blank, and a venue without
them is absent from `exchange_providers` rather than built and failing. The API key and the
passphrase travel in headers, so they must also be text a header can carry.

Every refusal is asserted twice over: it names the variable, which is what makes it
actionable, and it carries no value, which is rule 3. The values are sentinels, so their
absence is searched for literally, and each absence has a companion: the same sentinel is
present where it belongs.

`Settings` is built directly rather than through `get_settings`, which is cached for the
process, except where the environment itself is the subject -- those tests set the variables
with `monkeypatch` and build a fresh `Settings()`, which is the path the Raspberry Pi takes.
"""

from __future__ import annotations

import inspect
import itertools
import sys
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from portfolio.config import Settings
from portfolio.domain.exchanges import ExchangeKey
from portfolio.providers.exchanges.bitget import (
    BITGET_CAPABILITIES,
    BitgetProvider,
    bitget_credentials,
)
from portfolio.providers.exchanges.registry import exchange_providers
from tests.providers.exchanges.bitget_harness import (
    ACCESS_KEY_SENTINEL,
    PHRASE_SENTINEL,
    SIGNING_SENTINEL,
    FakeBitget,
    bitget_client,
    synthetic_credentials,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from portfolio.providers.exchanges.base import ExchangeProvider

KEY_VARIABLE: Final = "PORTFOLIO_BITGET_API_KEY"
SIGNING_KEY_VARIABLE: Final = "PORTFOLIO_BITGET_API_SECRET"
PHRASE_VARIABLE: Final = "PORTFOLIO_BITGET_API_PASSPHRASE"

#: Each variable, the `Settings` field it fills, and the sentinel a test sets it to.
VARIABLES: Final = (
    (KEY_VARIABLE, "bitget_api_key", ACCESS_KEY_SENTINEL),
    (SIGNING_KEY_VARIABLE, "bitget_api_secret", SIGNING_SENTINEL),
    (PHRASE_VARIABLE, "bitget_api_passphrase", PHRASE_SENTINEL),
)
ALL_VARIABLES: Final = tuple(variable for variable, _field, _value in VARIABLES)

#: Every non-empty proper subset of the three: the configurations that must refuse to start.
PARTIAL_SETS: Final = [
    frozenset(chosen) for size in (1, 2) for chosen in itertools.combinations(ALL_VARIABLES, size)
]


def settings_with(values: Mapping[str, str]) -> Settings:
    """A `Settings` holding exactly the given variables, each wrapped as a `SecretStr`."""
    fields = {
        field: SecretStr(values[variable])
        for variable, field, _value in VARIABLES
        if variable in values
    }
    return Settings(**fields)  # type: ignore[arg-type]


def configured() -> dict[str, str]:
    return {variable: value for variable, _field, value in VARIABLES}


# --------------------------------------------------------------------------------------
# All or none
# --------------------------------------------------------------------------------------


def test_there_are_six_partial_sets() -> None:
    """The premise: three singletons and three pairs."""
    assert len(PARTIAL_SETS) == 6
    assert len(set(PARTIAL_SETS)) == 6


@pytest.mark.parametrize(
    "present", PARTIAL_SETS, ids=lambda present: "+".join(sorted(present)) or "none"
)
def test_the_credentials_are_all_or_none(present: frozenset[str]) -> None:
    values = {variable: value for variable, value in configured().items() if variable in present}

    with pytest.raises(ValidationError) as caught:
        settings_with(values)

    message = str(caught.value)
    for variable in ALL_VARIABLES:
        if variable not in present:
            assert variable in message, f"the refusal does not name the missing {variable}"
    for _variable, _field, value in VARIABLES:
        assert value not in message


@pytest.mark.parametrize(
    "present", PARTIAL_SETS, ids=lambda present: "+".join(sorted(present)) or "none"
)
def test_a_partial_set_from_the_environment_refuses_and_shows_no_value(
    present: frozenset[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path the Pi takes: raw environment strings, before any `SecretStr` wraps them.

    `str(ValidationError)` is what reaches the log when the process refuses to start, and
    pydantic renders an `input_value` in it. The values here are short enough that nothing
    is elided, so this is the shape where a value would show if it were carried.
    """
    for variable, value in configured().items():
        if variable in present:
            monkeypatch.setenv(variable, value)
        else:
            monkeypatch.delenv(variable, raising=False)

    with pytest.raises(ValidationError) as caught:
        Settings()

    message = str(caught.value)
    assert any(variable in message for variable in ALL_VARIABLES if variable not in present)
    for variable, value in configured().items():
        if variable in present:
            assert value not in message, f"the value of {variable} reached the refusal"


def test_all_three_set_is_accepted_and_none_set_is_the_unconfigured_default() -> None:
    """The two configurations that start: every variable, or none of them."""
    full = settings_with(configured())
    empty = settings_with({})

    assert full.bitget_api_key is not None
    assert full.bitget_api_key.get_secret_value() == ACCESS_KEY_SENTINEL
    assert empty.bitget_api_key is None
    assert empty.bitget_api_secret is None
    assert empty.bitget_api_passphrase is None


@pytest.mark.parametrize("blank", ["", "   ", "\t"], ids=["empty", "spaces", "a tab"])
@pytest.mark.parametrize("variable", ALL_VARIABLES)
def test_a_blank_credential_is_refused_at_startup(variable: str, blank: str) -> None:
    """Refused as blank, naming that variable -- not treated as absent, and not a partial set."""
    values = {**configured(), variable: blank}

    with pytest.raises(ValidationError) as caught:
        settings_with(values)

    message = str(caught.value)
    assert variable in message
    assert "blank" in message
    for other, value in configured().items():
        if other != variable:
            assert value not in message


@pytest.mark.parametrize("variable", ALL_VARIABLES)
def test_a_blank_credential_alone_is_refused_too(variable: str) -> None:
    """One blank variable and nothing else is not "unconfigured": somebody set it."""
    with pytest.raises(ValidationError) as caught:
        settings_with({variable: ""})

    assert variable in str(caught.value)


# --------------------------------------------------------------------------------------
# Text a header can carry
# --------------------------------------------------------------------------------------

#: Built with `chr`, so the source stays ASCII.
E_ACUTE: Final = chr(0xE9)

UNSENDABLE: Final = {
    "a newline": "synthetic\nkey-value",
    "a carriage return": "synthetic\rkey-value",
    "a NUL": "synthetic\x00key-value",
    "a tab": "synthetic\tkey-value",
    "a DEL": "synthetic\x7fkey-value",
    "a leading space": " synthetic-key-value",
    "a trailing space": "synthetic-key-value ",
    "a non-ASCII letter": "synthetic-k" + E_ACUTE + "y-value",
}


@pytest.mark.parametrize("why", sorted(UNSENDABLE))
@pytest.mark.parametrize("variable", [KEY_VARIABLE, PHRASE_VARIABLE])
def test_a_key_or_passphrase_no_header_can_carry_is_refused_at_startup(
    variable: str, why: str
) -> None:
    """h11 refuses such a header value with a message quoting all of it; refused here first."""
    value = UNSENDABLE[why]

    with pytest.raises(ValidationError) as caught:
        settings_with({**configured(), variable: value})

    message = str(caught.value)
    assert variable in message
    assert value not in message
    assert "key-value" not in message


@pytest.mark.parametrize("variable", [KEY_VARIABLE, PHRASE_VARIABLE])
def test_an_interior_space_is_accepted_at_startup(variable: str) -> None:
    """The companion: a space inside a header value is legal, and so is this."""
    settings = settings_with({**configured(), variable: "synthetic key with interior spaces"})

    credentials = bitget_credentials(settings)

    assert credentials is not None


def test_the_secret_is_not_held_to_the_header_rule() -> None:
    """The secret is only HMAC input: spaces round it and a non-ASCII letter are its business."""
    signing_key = " synthetic s" + E_ACUTE + "cret with spaces "
    settings = settings_with({**configured(), SIGNING_KEY_VARIABLE: signing_key})

    credentials = bitget_credentials(settings)

    assert credentials is not None
    assert credentials.api_secret.get_secret_value() == signing_key


# --------------------------------------------------------------------------------------
# From settings to credentials to the registry
# --------------------------------------------------------------------------------------


def test_bitget_credentials_carry_the_three_values_or_are_none() -> None:
    credentials = bitget_credentials(settings_with(configured()))

    assert credentials is not None
    assert credentials.api_key.get_secret_value() == ACCESS_KEY_SENTINEL
    assert credentials.api_secret.get_secret_value() == SIGNING_SENTINEL
    assert credentials.passphrase is not None
    assert credentials.passphrase.get_secret_value() == PHRASE_SENTINEL
    assert bitget_credentials(settings_with({})) is None


@pytest.mark.parametrize(
    "present", PARTIAL_SETS, ids=lambda present: "+".join(sorted(present)) or "none"
)
def test_bitget_credentials_refuse_a_partial_set_that_skipped_validation(
    present: frozenset[str],
) -> None:
    """`Settings.model_construct` skips the validator; the credentials still refuse to build.

    The one route to a partial set past startup, and the refusal names all three variables
    and none of the values.
    """
    fields = {
        field: SecretStr(value) for variable, field, value in VARIABLES if variable in present
    }
    unvalidated = Settings.model_construct(**fields)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="all or none") as caught:
        bitget_credentials(unvalidated)

    message = str(caught.value)
    for variable in ALL_VARIABLES:
        assert variable in message
    for _variable, _field, value in VARIABLES:
        assert value not in message


async def test_an_unconfigured_venue_is_absent_not_built() -> None:
    """No credentials: an empty table, and nothing asked of the network building it."""
    fake = FakeBitget()

    async with bitget_client(fake) as client:
        unconfigured = exchange_providers(client, settings=settings_with({}))
        configured_table = exchange_providers(client, settings=settings_with(configured()))

    assert dict(unconfigured) == {}
    assert list(configured_table) == [ExchangeKey.BITGET]
    provider = configured_table[ExchangeKey.BITGET]
    assert isinstance(provider, BitgetProvider)
    assert provider.capabilities == BITGET_CAPABILITIES
    assert isinstance(configured_table, MappingProxyType)
    assert isinstance(unconfigured, MappingProxyType)
    assert fake.requests == [], "building the table made a request"


async def test_the_table_cannot_be_edited_after_it_is_built() -> None:
    fake = FakeBitget()

    async with bitget_client(fake) as client:
        table = exchange_providers(client, settings=settings_with(configured()))

        with pytest.raises(TypeError):
            table[ExchangeKey.BINGX] = table[ExchangeKey.BITGET]  # type: ignore[index]


def test_the_provider_refuses_credentials_without_a_passphrase() -> None:
    """Bitget signs with a passphrase header; credentials without one cannot sign a request."""
    credentials = synthetic_credentials(passphrase=None)

    with pytest.raises(ValueError, match="passphrase") as caught:
        BitgetProvider(httpx.AsyncClient(), credentials)

    rendered = f"{caught.value}{caught.value!r}"
    assert ACCESS_KEY_SENTINEL not in rendered
    assert SIGNING_SENTINEL not in rendered


def test_the_provider_accepts_the_three() -> None:
    """The companion: with a passphrase, the same construction succeeds."""
    provider = BitgetProvider(httpx.AsyncClient(), synthetic_credentials())

    assert provider.capabilities == BITGET_CAPABILITIES


def test_no_rendering_of_the_provider_carries_a_credential() -> None:
    provider = BitgetProvider(httpx.AsyncClient(), synthetic_credentials())

    rendered = f"{provider}{provider!r}"

    for value in (ACCESS_KEY_SENTINEL, SIGNING_SENTINEL, PHRASE_SENTINEL):
        assert value not in rendered


# --------------------------------------------------------------------------------------
# Criterion 9: the protocol, checked by `mypy --strict`
# --------------------------------------------------------------------------------------


def test_the_protocol_is_satisfied() -> None:
    """The run-time half of the check. The static half is `_CONFORMS` below.

    `ExchangeProvider` is not `@runtime_checkable`, so `isinstance` could compare only
    names. `mypy --strict` over this module decides whether the assignment type checks: a
    missing member, a wrong signature or a plain `def` where the protocol says `async def`
    fails the gate. This test asserts the line is still here, so deleting it is a failure
    and not a quiet loss of the check.
    """
    source = inspect.getsource(sys.modules[__name__])

    assert "_CONFORMS: ExchangeProvider = BitgetProvider(" in source
    assert _CONFORMS.capabilities == BITGET_CAPABILITIES


_CONFORMS: ExchangeProvider = BitgetProvider(httpx.AsyncClient(), synthetic_credentials())
"""The static check. Do not delete, and do not replace with an `isinstance` assertion."""
