"""Criterion 8 of #14, and the protocol check of criterion 9.

The BingX credentials are two settings, both or neither, never blank, and a venue without
them is absent from `exchange_providers` rather than built and failing. The API key travels
in the `X-BX-APIKEY` header, so it must also be text a header can carry; the secret only
ever enters an HMAC, so it is held to no such rule. BingX keys have no passphrase, and a
`Credentials` carrying one is refused.

Every refusal is asserted twice over: it names the variable, which is what makes it
actionable, and it carries no value, which is rule 3. Each absence has a companion: the same
sentinel is present where it belongs.

`Settings` is built directly rather than through `get_settings`, which is cached for the
process, except where the environment itself is the subject -- those tests set the variables
with `monkeypatch` and build a fresh `Settings()`, which is the path the Raspberry Pi takes.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from portfolio.config import Settings
from portfolio.domain.exchanges import ExchangeKey
from portfolio.providers.exchanges.bingx import (
    BINGX_CAPABILITIES,
    BingXProvider,
    bingx_credentials,
)
from portfolio.providers.exchanges.bitget import BitgetProvider
from portfolio.providers.exchanges.registry import exchange_providers
from tests.providers.exchanges.bingx_harness import (
    ACCESS_KEY_SENTINEL,
    SIGNING_SENTINEL,
    WINDOW,
    FakeBingX,
    bingx_client,
    bingx_provider,
    spread_fills,
    synthetic_credentials,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from portfolio.providers.exchanges.base import ExchangeProvider

KEY_VARIABLE: Final = "PORTFOLIO_BINGX_API_KEY"
SIGNING_KEY_VARIABLE: Final = "PORTFOLIO_BINGX_API_SECRET"

#: Each variable, the `Settings` field it fills, and the sentinel a test sets it to.
VARIABLES: Final = (
    (KEY_VARIABLE, "bingx_api_key", ACCESS_KEY_SENTINEL),
    (SIGNING_KEY_VARIABLE, "bingx_api_secret", SIGNING_SENTINEL),
)
ALL_VARIABLES: Final = (KEY_VARIABLE, SIGNING_KEY_VARIABLE)

#: Bitget's three, for the tests about the two venues side by side. Values from nowhere
#: else: short, obviously synthetic sentences.
BITGET_FIELDS: Final = {
    "bitget_api_key": "synthetic-bitget-side-key",
    "bitget_api_secret": "synthetic-bitget-side-signing-value",
    "bitget_api_passphrase": "synthetic-bitget-side-phrase",
}


def settings_with(values: Mapping[str, str], *, bitget: bool = False) -> Settings:
    """A `Settings` holding exactly the given BingX variables, each wrapped as a `SecretStr`.

    `bitget` adds a complete, valid Bitget set beside them.
    """
    fields = {
        field: SecretStr(values[variable])
        for variable, field, _value in VARIABLES
        if variable in values
    }
    if bitget:
        fields.update({field: SecretStr(value) for field, value in BITGET_FIELDS.items()})
    return Settings(**fields)  # type: ignore[arg-type]


def configured() -> dict[str, str]:
    return {variable: value for variable, _field, value in VARIABLES}


# --------------------------------------------------------------------------------------
# Both or neither
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("present", ALL_VARIABLES)
def test_the_credentials_are_both_or_neither(present: str) -> None:
    """One set without the other refuses to start, naming the one that is missing."""
    missing = next(variable for variable in ALL_VARIABLES if variable != present)

    with pytest.raises(ValidationError) as caught:
        settings_with({present: configured()[present]})

    message = str(caught.value)
    assert missing in message, f"the refusal does not name the missing {missing}"
    for _variable, _field, value in VARIABLES:
        assert value not in message


@pytest.mark.parametrize("present", ALL_VARIABLES)
def test_a_partial_set_from_the_environment_refuses_and_shows_no_value(
    present: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path the Pi takes: raw environment strings, before any `SecretStr` wraps them."""
    missing = next(variable for variable in ALL_VARIABLES if variable != present)
    monkeypatch.setenv(present, configured()[present])
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(ValidationError) as caught:
        Settings()

    message = str(caught.value)
    assert missing in message
    assert configured()[present] not in message, f"the value of {present} reached the refusal"


def test_both_set_is_accepted_and_neither_set_is_the_unconfigured_default() -> None:
    full = settings_with(configured())
    empty = settings_with({})

    assert full.bingx_api_key is not None
    assert full.bingx_api_key.get_secret_value() == ACCESS_KEY_SENTINEL
    assert full.bingx_api_secret is not None
    assert full.bingx_api_secret.get_secret_value() == SIGNING_SENTINEL
    assert empty.bingx_api_key is None
    assert empty.bingx_api_secret is None


def test_both_set_from_the_environment_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The companion to the partial set: the two variables read from the environment."""
    for variable, value in configured().items():
        monkeypatch.setenv(variable, value)

    settings = Settings()

    credentials = bingx_credentials(settings)
    assert credentials is not None
    assert credentials.api_key.get_secret_value() == ACCESS_KEY_SENTINEL


@pytest.mark.parametrize("blank", ["", "   ", "\t"], ids=["empty", "spaces", "a tab"])
@pytest.mark.parametrize("variable", ALL_VARIABLES)
def test_a_blank_credential_is_refused_at_startup(variable: str, blank: str) -> None:
    """Refused as blank, naming that variable: not treated as absent, and not a partial set."""
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
    assert "blank" in str(caught.value)


def test_the_two_venues_are_validated_apart() -> None:
    """A complete Bitget set does not excuse a partial BingX one, and each is independent."""
    with pytest.raises(ValidationError) as caught:
        settings_with({KEY_VARIABLE: ACCESS_KEY_SENTINEL}, bitget=True)
    assert SIGNING_KEY_VARIABLE in str(caught.value)

    only_bitget = settings_with({}, bitget=True)
    assert only_bitget.bitget_api_key is not None
    assert only_bitget.bingx_api_key is None


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
def test_a_key_no_header_can_carry_is_refused_at_startup(why: str) -> None:
    """h11 refuses such a header value with a message quoting all of it; refused here first."""
    value = UNSENDABLE[why]

    with pytest.raises(ValidationError) as caught:
        settings_with({**configured(), KEY_VARIABLE: value})

    message = str(caught.value)
    assert KEY_VARIABLE in message
    assert value not in message
    assert "key-value" not in message


def test_an_interior_space_is_accepted_at_startup() -> None:
    """The companion: a space inside a header value is legal, and so is this."""
    settings = settings_with({**configured(), KEY_VARIABLE: "synthetic key with interior spaces"})

    assert bingx_credentials(settings) is not None


@pytest.mark.parametrize("why", sorted(UNSENDABLE))
def test_the_secret_is_not_held_to_the_header_rule(why: str) -> None:
    """The secret is only HMAC input: every value the key may not hold, the secret may.

    Blank excepted, which is refused for every credential and tested above.
    """
    signing_key = UNSENDABLE[why]
    settings = settings_with({**configured(), SIGNING_KEY_VARIABLE: signing_key})

    credentials = bingx_credentials(settings)

    assert credentials is not None
    assert credentials.api_secret.get_secret_value() == signing_key


# --------------------------------------------------------------------------------------
# No fragment of a credential in a startup refusal
# --------------------------------------------------------------------------------------
#
# `str(ValidationError)` is what reaches stdout when the container refuses to start. On #13
# pydantic echoed the input with only its middle elided, so a secret's tail survived; a
# whole-value search cannot see a tail, so these tests search for every five-character
# window of each credential. The sentinels are two letters repeated: obviously synthetic,
# low in entropy, and sharing no five-character window with any variable name or with
# anything `config.py` could write, which the premise test checks.

WINDOW_SIZE: Final = 5
KEY_WINDOW_SENTINEL: Final = "kxkxkxkxkxkxkxkxkxkxkxkx"
SIGNING_WINDOW_SENTINEL: Final = "vbvbvbvbvbvbvbvbvbvbvbvb"
WINDOW_SENTINELS: Final = {
    KEY_VARIABLE: KEY_WINDOW_SENTINEL,
    SIGNING_KEY_VARIABLE: SIGNING_WINDOW_SENTINEL,
}
CONFIG_SOURCE: Final = Path(inspect.getfile(Settings))


def windows(value: str) -> set[str]:
    """Every run of `WINDOW_SIZE` characters in `value`."""
    return {value[start : start + WINDOW_SIZE] for start in range(len(value) - WINDOW_SIZE + 1)}


def leaked_windows(rendered: str) -> list[str]:
    """Each sentinel window found in `rendered`, case-insensitively."""
    lowered = rendered.lower()
    return sorted(
        window
        for sentinel in WINDOW_SENTINELS.values()
        for window in windows(sentinel)
        if window in lowered
    )


def test_the_window_sentinels_occur_nowhere_but_in_the_input() -> None:
    """The premise: a window found in a refusal can only have come from the input."""
    text = " ".join([*ALL_VARIABLES, "PORTFOLIO_KASPA_API_URL"]).lower()
    source = CONFIG_SOURCE.read_text(encoding="utf-8").lower()

    for sentinel in WINDOW_SENTINELS.values():
        assert len(sentinel) > 20, "long enough that a truncated tail would still be a window"
        for window in windows(sentinel):
            assert window not in text
            assert window not in source


def test_the_window_search_catches_a_tail() -> None:
    """The control: the last five characters of a sentinel are enough to fail."""
    assert leaked_windows(f"...{KEY_WINDOW_SENTINEL[-5:]}'") != []
    assert leaked_windows("PORTFOLIO_BINGX_API_KEY is set but blank") == []


#: Each refusal, as the environment it is raised from. `None` unsets a variable.
REFUSALS: Final[dict[str, dict[str, str | None]]] = {
    "a partial set": {SIGNING_KEY_VARIABLE: None},
    "a blank key": {KEY_VARIABLE: ""},
    "a blank secret": {SIGNING_KEY_VARIABLE: ""},
    "a key no header can carry": {KEY_VARIABLE: KEY_WINDOW_SENTINEL + " "},
    "an unrelated refusal, both configured": {"PORTFOLIO_KASPA_API_URL": "not a url"},
}

#: The variable each refusal must name.
NAMED: Final = {
    "a partial set": SIGNING_KEY_VARIABLE,
    "a blank key": KEY_VARIABLE,
    "a blank secret": SIGNING_KEY_VARIABLE,
    "a key no header can carry": KEY_VARIABLE,
    "an unrelated refusal, both configured": "PORTFOLIO_KASPA_API_URL",
}


@pytest.mark.parametrize("case", list(REFUSALS))
def test_no_window_of_a_credential_reaches_a_startup_refusal(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`str` and `repr` of the refusal carry no five-character run of either credential.

    The last case is the one production will actually meet: both credentials configured
    correctly and something else wrong, so the input echoed is the whole good set. The
    positive companion is `errors()`, which still carries the input: it proves the sentinels
    were in what pydantic was given, so their absence from `str` is the redaction.
    """
    environment: dict[str, str | None] = {**WINDOW_SENTINELS, **REFUSALS[case]}
    for variable, value in environment.items():
        if value is None:
            monkeypatch.delenv(variable, raising=False)
        else:
            monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError) as caught:
        Settings()

    rendered = f"{caught.value}\n{caught.value!r}"
    assert NAMED[case] in str(caught.value)
    assert leaked_windows(rendered) == []
    structured = repr(caught.value.errors())
    carried = [
        sentinel
        for variable, sentinel in WINDOW_SENTINELS.items()
        if sentinel in (environment[variable] or "")
    ]
    assert carried, "the case sets no sentinel at all, so the absence above proves nothing"
    for sentinel in carried:
        assert sentinel in structured, "a sentinel never reached pydantic's input"


# --------------------------------------------------------------------------------------
# Spec 017, R4: a credential that is not UTF-8 is refused at startup, for both venues
# --------------------------------------------------------------------------------------
#
# Before R4 a secret holding a lone surrogate -- what bytes from a file in another encoding
# become under `surrogateescape` -- passed `Settings`, and the first signed request raised a
# bare `UnicodeEncodeError` from `signing`, whose `args` held the whole secret. The key and
# the passphrase would have been refused anyway, by the header rule; the secrets had no rule.

#: Every exchange credential variable of both venues, and the `Settings` field it fills.
EXCHANGE_CREDENTIAL_FIELDS: Final = {
    KEY_VARIABLE: "bingx_api_key",
    SIGNING_KEY_VARIABLE: "bingx_api_secret",
    "PORTFOLIO_BITGET_API_KEY": "bitget_api_key",
    "PORTFOLIO_BITGET_API_SECRET": "bitget_api_secret",
    "PORTFOLIO_BITGET_API_PASSPHRASE": "bitget_api_passphrase",
}

#: A lone low surrogate: the code point `surrogateescape` decodes the byte 0xFF to.
LONE_SURROGATE: Final = chr(0xDCFF)


def both_venues_with(field: str, value: str) -> dict[str, SecretStr]:
    """Both venues fully configured with valid values, and `field` set to `value`."""
    fields = {
        "bingx_api_key": SecretStr(ACCESS_KEY_SENTINEL),
        "bingx_api_secret": SecretStr(SIGNING_SENTINEL),
        **{name: SecretStr(text) for name, text in BITGET_FIELDS.items()},
    }
    fields[field] = SecretStr(value)
    return fields


@pytest.mark.parametrize("variable", sorted(EXCHANGE_CREDENTIAL_FIELDS))
def test_a_credential_that_is_not_utf8_is_refused_at_startup(variable: str) -> None:
    """Refused naming the variable and the rule, with no window of the value in it.

    The value is a window sentinel split by a lone surrogate, so any fragment of it that
    reached the refusal would be found; the surrogate itself is searched for too.
    """
    value = KEY_WINDOW_SENTINEL[:12] + LONE_SURROGATE + KEY_WINDOW_SENTINEL[12:]
    field = EXCHANGE_CREDENTIAL_FIELDS[variable]

    with pytest.raises(ValidationError) as caught:
        Settings(**both_venues_with(field, value))  # type: ignore[arg-type]

    message = str(caught.value)
    rendered = f"{caught.value}\n{caught.value!r}"
    assert variable in message
    assert "UTF-8" in message
    assert leaked_windows(rendered) == []
    assert LONE_SURROGATE not in rendered


@pytest.mark.parametrize("variable", sorted(EXCHANGE_CREDENTIAL_FIELDS))
def test_the_same_credential_without_the_surrogate_is_accepted(variable: str) -> None:
    """The companion: the sentinel alone is a valid value for every one of the five."""
    field = EXCHANGE_CREDENTIAL_FIELDS[variable]

    settings = Settings(**both_venues_with(field, KEY_WINDOW_SENTINEL))  # type: ignore[arg-type]

    value = getattr(settings, field)
    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == KEY_WINDOW_SENTINEL


# --------------------------------------------------------------------------------------
# From settings to credentials to the registry
# --------------------------------------------------------------------------------------


def test_bingx_credentials_carry_the_two_values_and_no_passphrase_or_are_none() -> None:
    credentials = bingx_credentials(settings_with(configured()))

    assert credentials is not None
    assert credentials.api_key.get_secret_value() == ACCESS_KEY_SENTINEL
    assert credentials.api_secret.get_secret_value() == SIGNING_SENTINEL
    assert credentials.passphrase is None
    assert bingx_credentials(settings_with({})) is None


@pytest.mark.parametrize("present", ALL_VARIABLES)
def test_bingx_credentials_refuse_a_partial_set_that_skipped_validation(present: str) -> None:
    """`Settings.model_construct` skips the validator; the credentials still refuse to build."""
    fields = {
        field: SecretStr(value) for variable, field, value in VARIABLES if variable == present
    }
    unvalidated = Settings.model_construct(**fields)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="neither") as caught:
        bingx_credentials(unvalidated)

    message = str(caught.value)
    for variable in ALL_VARIABLES:
        assert variable in message
    for _variable, _field, value in VARIABLES:
        assert value not in message


async def test_an_unconfigured_bingx_is_absent_not_built() -> None:
    """Neither variable: no BingX in the table, and nothing asked of the network building it.

    Present when configured, alone or beside Bitget, and the two are independent.
    """
    fake = FakeBingX()

    async with bingx_client(fake) as client:
        nothing = exchange_providers(client, settings=settings_with({}))
        only_bitget = exchange_providers(client, settings=settings_with({}, bitget=True))
        only_bingx = exchange_providers(client, settings=settings_with(configured()))
        both = exchange_providers(client, settings=settings_with(configured(), bitget=True))

    assert dict(nothing) == {}
    assert list(only_bitget) == [ExchangeKey.BITGET]
    assert list(only_bingx) == [ExchangeKey.BINGX]
    assert set(both) == {ExchangeKey.BITGET, ExchangeKey.BINGX}
    for table in (only_bingx, both):
        provider = table[ExchangeKey.BINGX]
        assert isinstance(provider, BingXProvider)
        assert provider.capabilities == BINGX_CAPABILITIES
    assert isinstance(both[ExchangeKey.BITGET], BitgetProvider)
    for table in (nothing, only_bitget, only_bingx, both):
        assert isinstance(table, MappingProxyType)
    assert fake.requests == [], "building the table made a request"


async def test_the_registered_provider_signs_with_the_configured_credentials() -> None:
    """The provider the registry builds is wired to the settings' key and secret: the fake
    verifies its request with the same two values."""
    fake = FakeBingX(spread_fills(2))

    async with bingx_client(fake) as client:
        provider = exchange_providers(client, settings=settings_with(configured()))[
            ExchangeKey.BINGX
        ]
        page = await provider.fetch_fill_page(WINDOW, cursor=None, symbol=None)

    assert len(page.fills) == 2
    assert fake.signature_failures == []
    assert len(fake.verified) == 1


#: A third value BingX has no use for: what a confused caller would put in `passphrase`.
UNWANTED_THIRD_VALUE: Final = "synthetic-third-value-bingx-has-none"


def test_the_provider_refuses_credentials_with_a_passphrase() -> None:
    """BingX keys have none; a passphrase set means the caller built the wrong credentials."""
    credentials = synthetic_credentials(passphrase=UNWANTED_THIRD_VALUE)

    with pytest.raises(ValueError, match="passphrase") as caught:
        BingXProvider(httpx.AsyncClient(), credentials)

    rendered = f"{caught.value}{caught.value!r}"
    assert ACCESS_KEY_SENTINEL not in rendered
    assert SIGNING_SENTINEL not in rendered
    assert UNWANTED_THIRD_VALUE not in rendered


def test_the_provider_accepts_the_two() -> None:
    """The companion: without a passphrase, the same construction succeeds."""
    provider = BingXProvider(httpx.AsyncClient(), synthetic_credentials())

    assert provider.capabilities == BINGX_CAPABILITIES


@pytest.mark.parametrize("why", sorted(UNSENDABLE))
def test_the_provider_refuses_a_key_no_header_can_carry(why: str) -> None:
    """The same rule for `Credentials` built any other way than from `Settings`."""
    value = UNSENDABLE[why]

    with pytest.raises(ValueError, match="api_key") as caught:
        bingx_provider(httpx.AsyncClient(), credentials=synthetic_credentials(api_key=value))

    rendered = f"{caught.value}{caught.value!r}{caught.value.args}"
    assert value not in rendered
    assert "key-value" not in rendered


async def test_an_interior_space_in_the_key_is_signed_and_sent() -> None:
    """The companion: a space inside is legal in a header value, and the venue verifies it."""
    value = "synthetic key with interior spaces"
    fake = FakeBingX(spread_fills(1), access_key=value)

    async with bingx_client(fake) as client:
        provider = bingx_provider(client, credentials=synthetic_credentials(api_key=value))
        page = await provider.fetch_fill_page(WINDOW, cursor=None, symbol=None)

    assert len(page.fills) == 1
    assert fake.signature_failures == []


async def test_a_secret_no_header_could_carry_still_signs() -> None:
    """The secret is HMAC input, never a header, so spaces round it and non-ASCII are fine."""
    signing_key = " synthetic s" + E_ACUTE + "cret with spaces "
    fake = FakeBingX(spread_fills(1), signing_key=signing_key)

    async with bingx_client(fake) as client:
        provider = bingx_provider(client, credentials=synthetic_credentials(api_secret=signing_key))
        page = await provider.fetch_fill_page(WINDOW, cursor=None, symbol=None)

    assert len(page.fills) == 1
    assert fake.signature_failures == []


def test_no_rendering_of_the_provider_carries_a_credential() -> None:
    provider = BingXProvider(httpx.AsyncClient(), synthetic_credentials())

    rendered = f"{provider}{provider!r}"

    for value in (ACCESS_KEY_SENTINEL, SIGNING_SENTINEL):
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

    assert "_CONFORMS: ExchangeProvider = BingXProvider(" in source
    assert _CONFORMS.capabilities == BINGX_CAPABILITIES


_CONFORMS: ExchangeProvider = BingXProvider(httpx.AsyncClient(), synthetic_credentials())
"""The static check. Do not delete, and do not replace with an `isinstance` assertion."""
