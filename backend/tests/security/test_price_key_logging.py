"""Rule 3 at #9's one credential: the CoinGecko key reaches no log record and no message.

This is the first API key in the product. Everything rule 3 says about credentials -- read
from the environment into a `SecretStr`, never persisted, never returned by an endpoint,
never logged -- has had nothing to guard until now, and a guarantee that has never been
exercised is a guarantee nobody knows the state of.

## Why this reads stdout rather than `structlog.testing.capture_logs`

`capture_logs` swaps the whole processor chain out for a `LogCapture`, so `format_exc_info`
never runs and a secret carried inside an exception's text is invisible to the assertion.
That is not hypothetical: it is how a real production leak passed a green gate on #5.
`tests/security/conftest.py` records the reasoning at length. The bytes on stdout are the
artifact that actually gets copied, tailed and pasted into an issue, so that is what these
tests read.

## The three places the key could get out, and all three are exercised

**The success path**, at debug level, where the transport logs `provider_request` with a
target built from the request. **The failure path**, at error level, which is where a 401
lands -- the status most likely to tempt somebody into logging the credential that failed.
And the **exception** the failure produces, which reaches a log the moment anything calls
`logger.exception`.

The sentinel is a sentence rather than a key-shaped string. Rule 3 forbids a real key and
also forbids a plausible fake: a realistic-looking string is what a secret scanner has to
flag, and a fixture that trips the scanner is a fixture somebody weakens the scanner for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest

from portfolio.providers.errors import ProviderError, ProviderResponseError
from portfolio.providers.prices.base import BTC, USD
from portfolio.providers.prices.coingecko import API_KEY_HEADER, CoinGeckoPriceSource
from portfolio.providers.prices.registry import price_sources
from tests.providers.prices.harness import (
    SYNTHETIC_COINGECKO_KEY,
    PriceFake,
    Reply,
    ScriptedVendor,
    coingecko_body,
    price_client,
    price_settings,
)
from tests.security.conftest import assert_carried_something

if TYPE_CHECKING:
    from collections.abc import Sequence

    from portfolio.providers.prices.base import PricePair

    from .conftest import ProductionLoggingInstaller

BTC_USD: Final[PricePair] = (BTC, USD)

#: Something the log must contain, so that an absence assertion is not satisfied by an
#: empty capture. `tests/security/conftest.py` explains why every one of these tests needs
#: a positive marker: twice now a logging test here has passed against nothing at all.
PRICE_LABEL: Final = "asset_prices"


async def request_prices(fake: PriceFake, pairs: Sequence[PricePair] = (BTC_USD,)) -> None:
    """One keyed request against the scripted vendor, swallowing whatever it produces.

    The exception is deliberately caught and dropped: what these tests read is the *log*,
    and a failure arm that propagated would end the test before the capture was read.
    `test_the_key_is_not_in_the_exception_a_refusal_raises` is where the exception itself
    is inspected.
    """
    client = price_client(fake)
    source = CoinGeckoPriceSource(
        client, settings=price_settings(coingecko_api_key=SYNTHETIC_COINGECKO_KEY)
    )
    async with client:
        try:
            await source.fetch(pairs)
        except ProviderError:
            # `ProviderError` and not one of its subclasses: a 401 raises
            # `ProviderResponseError` and a 429 raises `ProviderRateLimitedError`, and
            # catching either one specifically would make the other arm of this module
            # fail for a reason that has nothing to do with what is in the log.
            return


async def test_a_successful_priced_request_logs_no_credential(
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ordinary hourly path, at debug, where the transport logs every request it makes.

    Debug is the level that matters here: the success log is the one an operator turns on
    when something is wrong, which is exactly the moment a leaked credential would be
    copied into a ticket along with the surrounding lines.
    """
    production_logging(log_level="DEBUG")
    fake = PriceFake(
        coingecko=ScriptedVendor(Reply(body=coingecko_body({"bitcoin": {"usd": "86000.10000"}})))
    )

    await request_prices(fake)

    written = capsys.readouterr().out

    assert_carried_something(written, marker=PRICE_LABEL)
    assert SYNTHETIC_COINGECKO_KEY not in written
    assert API_KEY_HEADER not in written, "the header name in a log is a signpost to the value"


async def test_a_refused_request_logs_no_credential_either(
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 401 is what a wrong or expired key produces, and it is the tempting one.

    "The credential was rejected" is a line somebody writes with the credential in it,
    because at that moment it is the thing they want to see. The transport logs a target
    built from the scheme, the host and the endpoint label, and nothing else -- so there is
    no path for the header to reach the line. Asserted rather than reasoned about, because
    this is the one status where the reasoning is under pressure.
    """
    production_logging(log_level="DEBUG")
    fake = PriceFake(coingecko=ScriptedVendor(Reply(status=401)))

    await request_prices(fake)

    written = capsys.readouterr().out

    assert_carried_something(written, marker="provider_request_failed")
    assert SYNTHETIC_COINGECKO_KEY not in written


async def test_a_throttled_request_logs_no_credential_across_every_retry(
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Three attempts, two retry warnings and one error, and none of them carries the key.

    The retry path is the one that writes the *most* lines about a single request, which
    makes it the one where a leak would be repeated rather than isolated. `429` is also
    what a Demo key over its per-minute limit produces, so this is a realistic shape and
    not merely a thorough one.
    """
    production_logging(log_level="DEBUG")
    fake = PriceFake(coingecko=ScriptedVendor(Reply(status=429)))

    await request_prices(fake)

    written = capsys.readouterr().out

    assert_carried_something(written, marker="provider_request_retry")
    assert SYNTHETIC_COINGECKO_KEY not in written
    assert written.count("provider_request_retry") >= 1


async def test_the_key_is_not_in_the_exception_a_refusal_raises() -> None:
    """An exception is a string that reaches a log the moment anything calls `.exception`.

    `redact_sensitive` cannot help there: it matches key names, and the field would be
    called `exception`. So the only thing that keeps a credential out of that path is the
    message never having carried it, which is what this asserts -- over the message, the
    repr, the args, and the chained cause.
    """
    fake = PriceFake(coingecko=ScriptedVendor(Reply(status=401)))
    client = price_client(fake)
    source = CoinGeckoPriceSource(
        client, settings=price_settings(coingecko_api_key=SYNTHETIC_COINGECKO_KEY)
    )

    async with client:
        with pytest.raises(ProviderResponseError) as caught:
            await source.fetch([BTC_USD])

    rendered = f"{caught.value}{caught.value!r}{caught.value.args}{caught.value.__cause__!r}"

    assert SYNTHETIC_COINGECKO_KEY not in rendered


async def test_the_key_is_not_in_the_settings_repr_or_in_a_source_that_holds_it() -> None:
    """`SecretStr` is what masks it, and this is the test that says it is still one.

    The realistic edit that undoes this is small and sounds sensible: unwrap the secret in
    `__init__` "because it is needed at request time anyway". Every other test in the price
    suite stays green through it. `Settings` is checked in the same test because the source
    reads the key out of it, so both ends of the one value are covered together.
    """
    settings = price_settings(coingecko_api_key=SYNTHETIC_COINGECKO_KEY)
    fake = PriceFake()
    client = price_client(fake)
    source = CoinGeckoPriceSource(client, settings=settings)

    async with client:
        rendered = "".join(
            (
                repr(settings),
                str(settings),
                repr(settings.model_dump()),
                repr(source),
                repr(vars(source)),
            )
        )

    assert SYNTHETIC_COINGECKO_KEY not in rendered
    assert settings.coingecko_api_key is not None
    assert settings.coingecko_api_key.get_secret_value() == SYNTHETIC_COINGECKO_KEY


def test_the_key_is_never_persisted_and_no_column_could_hold_it() -> None:
    """Rule 3's "never persisted", checked against the schema rather than against a habit.

    No table in this application has a column for a vendor credential, and the assertion is
    over every mapped column rather than over the `prices` table alone -- the realistic way
    a credential gets persisted is somebody adding a `source_api_key` beside the thing it
    belongs to, which would be a new column on an existing table rather than a new table.
    """
    from portfolio.db.models import metadata

    columns = {
        f"{table.name}.{column.name}"
        for table in metadata.tables.values()
        for column in table.columns
    }

    # `key` on its own is too wide to be useful: `wallets.chain_key` is a chain name and
    # `assets.symbol`-style identifiers are everywhere. The fragments here are the ones a
    # credential column is actually spelled with, which is the trade a name-based scan
    # always makes -- and the reason it is a second line of defence rather than the first.
    suspicious = sorted(
        name
        for name in columns
        if any(
            fragment in name.lower()
            for fragment in ("api_key", "apikey", "secret", "token", "credential", "password")
        )
    )

    # Two matches, neither a credential this application could leak. `users.password_hash`
    # is an Argon2id hash and `sessions.token_hash` is a hash of a session cookie: the whole
    # point of storing either is that a leaked database file hands the reader nothing
    # usable. Pinned by name so a third entry has to be argued for in a diff.
    assert suspicious == ["sessions.token_hash", "users.password_hash"]


def test_the_unkeyed_deployment_has_no_object_holding_a_credential_at_all() -> None:
    """Criterion 5 restated as a security property rather than as a configuration one.

    With no key the CoinGecko source is not constructed, so there is nothing in the process
    holding a credential and nothing that could put one in a log line. That is a stronger
    statement than "it is skipped", and it is the reason the spec chose absence over a
    branch: a skipped source still exists, still holds whatever it was given, and is one
    edit away from being asked.
    """
    sources = price_sources(price_client(PriceFake()), settings=price_settings())

    assert not any(isinstance(source, CoinGeckoPriceSource) for source in sources)
    assert all(SYNTHETIC_COINGECKO_KEY not in f"{source!r}{vars(source)!r}" for source in sources)
