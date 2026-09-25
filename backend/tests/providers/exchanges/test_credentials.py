"""Criterion 5: `Credentials` is `SecretStr`-backed and no rendering of it can leak.

Rule 3 names API keys as well as secrets, so all three fields are sentinels here and all
three are searched for. Every absence assertion has a positive companion: that the object
really does hold the sentinel (`get_secret_value()` returns it), and that the rendering
searched is a real, non-empty rendering of this object -- an empty string contains no secret
and proves nothing.

The sentinels are sentences, deliberately low-entropy and obviously fake. A realistic-looking
key is what a secret scanner has to flag, and a fixture that trips the scanner is a fixture
somebody weakens the scanner for.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Final

import pytest
import structlog
from pydantic import SecretStr

from portfolio.providers.exchanges.credentials import Credentials
from tests.security.conftest import assert_carried_something

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.providers.exchanges.conftest import LoggingInstaller

KEY_SENTINEL: Final = "sentinel-api-key-value"
SIGNING_SENTINEL: Final = "sentinel-api-signing-value"
PHRASE_SENTINEL: Final = "sentinel-passphrase-value"
SENTINELS: Final = (KEY_SENTINEL, SIGNING_SENTINEL, PHRASE_SENTINEL)

FIELDS: Final = ("api_key", "api_secret", "passphrase")


def with_passphrase() -> Credentials:
    return Credentials(
        api_key=SecretStr(KEY_SENTINEL),
        api_secret=SecretStr(SIGNING_SENTINEL),
        passphrase=SecretStr(PHRASE_SENTINEL),
    )


def without_passphrase() -> Credentials:
    return Credentials(api_key=SecretStr(KEY_SENTINEL), api_secret=SecretStr(SIGNING_SENTINEL))


def renderings(credentials: Credentials) -> dict[str, str]:
    """Every way a `Credentials` object turns into text in ordinary code, by name.

    Named so a failure says which door the secret came through. `dataclasses.asdict` is here
    because it is what a well-meaning "log the config" helper reaches for, and it recurses
    into the fields -- so it is only safe while every field is itself a `SecretStr`.
    """
    as_dict = dataclasses.asdict(credentials)
    as_tuple = dataclasses.astuple(credentials)
    return {
        "str": str(credentials),
        "repr": repr(credentials),
        "format": format(credentials),
        "f-string": f"{credentials}",
        "f-string !r": f"{credentials!r}",
        "f-string !s": f"{credentials!s}",
        "percent %s": "%s" % (credentials,),  # noqa: UP031 - the spelling is what is tested
        "percent %r": "%r" % (credentials,),  # noqa: UP031 - the spelling is what is tested
        "asdict repr": repr(as_dict),
        "asdict str": str(as_dict),
        "astuple repr": repr(as_tuple),
        "container repr": repr([credentials, {"credentials": credentials}]),
    }


@pytest.mark.parametrize("build", [with_passphrase, without_passphrase])
def test_no_rendering_contains_a_secret(build: Callable[[], Credentials]) -> None:
    """No sentinel in any rendering; every rendering is real; the sentinels are really held."""
    credentials = build()

    for name, text in renderings(credentials).items():
        assert text.strip(), f"{name} rendered nothing, so its absence check proves nothing"
        for sentinel in SENTINELS:
            assert sentinel not in text, f"{name} leaked a credential: {text[:200]}"

    # The positive companion: the object is not masking by having been handed nothing.
    assert credentials.api_key.get_secret_value() == KEY_SENTINEL
    assert credentials.api_secret.get_secret_value() == SIGNING_SENTINEL
    # And the renderings are of *this* type, not of some wrapper that swallowed it.
    assert "Credentials" in repr(credentials)
    assert "api_key" in repr(credentials)
    assert "api_secret" in repr(credentials)


def test_whether_a_passphrase_exists_is_visible_but_its_value_is_not() -> None:
    """`passphrase=None` is configuration, not a secret, so the rendering says so."""
    present = with_passphrase()
    absent = without_passphrase()

    assert present.passphrase is not None
    assert present.passphrase.get_secret_value() == PHRASE_SENTINEL
    assert absent.passphrase is None

    assert "passphrase=None" in repr(absent)
    assert "passphrase=None" not in repr(present)
    assert "passphrase" in repr(present)
    assert PHRASE_SENTINEL not in repr(present)


def test_every_field_is_held_as_a_secret_str() -> None:
    """`SecretStr`-backed, literally: each held value is a `SecretStr`, not an unwrapped `str`.

    The realistic regression is small and sounds sensible -- unwrap in `__post_init__`
    "because the signer needs the raw value anyway" -- and it would leave every rendering
    test above passing only as long as `__repr__` stayed hand-written.
    """
    credentials = with_passphrase()

    for field in FIELDS:
        assert isinstance(getattr(credentials, field), SecretStr), field
    assert {field.name for field in dataclasses.fields(credentials)} == set(FIELDS)


def test_credentials_are_frozen() -> None:
    """A credential that can be reassigned after construction skips `__post_init__`'s checks."""
    credentials = with_passphrase()

    with pytest.raises(dataclasses.FrozenInstanceError):
        credentials.api_key = SecretStr("replacement")  # type: ignore[misc]

    assert credentials.api_key.get_secret_value() == KEY_SENTINEL


def test_a_logged_credentials_object_is_masked(
    production_logging: LoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Through the real JSON pipeline, three ways a credentials object reaches a log line.

    As a bound value under a key the redactor does **not** recognise (`venue_auth` contains
    none of its fragments, so only the object's own rendering stands between the value and
    stdout), interpolated into the event text, and carried inside an exception that is
    logged with `.exception()`. Read off stdout, never out of `capture_logs`.
    """
    production_logging()
    credentials = with_passphrase()
    logger = structlog.get_logger("tests.exchanges.credentials")

    logger.warning("exchange_credentials_bound", venue_auth=credentials)
    logger.warning(f"exchange_credentials_interpolated {credentials}")
    try:
        message = f"refused while holding {credentials!r}"
        raise RuntimeError(message)
    except RuntimeError:
        logger.exception("exchange_credentials_in_exception")

    written = capsys.readouterr().out

    assert_carried_something(written, marker="exchange_credentials_bound")
    assert "exchange_credentials_interpolated" in written
    assert "exchange_credentials_in_exception" in written
    assert "RuntimeError" in written, "the exception text never reached the line"
    # The object's rendering reached each line: the absence below is about masking, not
    # about the object having been dropped.
    assert written.count("Credentials(") >= 3, written[:600]
    for sentinel in SENTINELS:
        assert sentinel not in written


@pytest.mark.parametrize("field", FIELDS)
def test_a_plain_string_secret_is_refused_naming_the_field_not_the_value(field: str) -> None:
    """A plain `str` is a `TypeError`: nothing would mask it in any rendering."""
    arguments: dict[str, object] = {
        "api_key": SecretStr("synthetic-not-a-real-key"),
        "api_secret": SecretStr("synthetic-not-a-real-value"),
        field: KEY_SENTINEL,
    }

    with pytest.raises(TypeError) as caught:
        Credentials(**arguments)  # type: ignore[arg-type]

    message = f"{caught.value}{caught.value!r}{caught.value.args}"
    assert field in str(caught.value)
    assert KEY_SENTINEL not in message


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("blank", ["", "   ", "\t\n"], ids=["empty", "spaces", "whitespace"])
def test_a_blank_secret_is_refused_naming_the_field(field: str, blank: str) -> None:
    """A blank credential is a missing one: refused at construction, not at the first 401."""
    arguments: dict[str, SecretStr] = {
        "api_key": SecretStr("synthetic-not-a-real-key"),
        "api_secret": SecretStr("synthetic-not-a-real-value"),
        field: SecretStr(blank),
    }

    with pytest.raises(ValueError, match=field) as caught:
        Credentials(**arguments)

    assert "synthetic-not-a-real" not in str(caught.value)


def test_a_plain_string_or_blank_secret_is_refused() -> None:
    """The spec's named case, and its positive companion: a valid set constructs."""
    with pytest.raises(TypeError, match="api_secret"):
        Credentials(api_key=SecretStr(KEY_SENTINEL), api_secret=SIGNING_SENTINEL)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="api_key"):
        Credentials(api_key=SecretStr(""), api_secret=SecretStr(SIGNING_SENTINEL))

    assert without_passphrase().api_secret.get_secret_value() == SIGNING_SENTINEL
