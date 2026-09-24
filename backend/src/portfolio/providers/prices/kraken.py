"""Every configured pair in one call to Kraken's public ticker, with prices as strings.

The primary source, and the reason the measured monthly budget is 720 requests rather than
thousands. Measured against the live service on **2026-09-23**:

```
GET https://api.kraken.com/0/public/Ticker?pair=XXBTZUSD,XXBTZEUR,KASUSD,KASEUR
-> 200, {"error": [], "result": {"XXBTZUSD": {...}, "XXBTZEUR": {...},
                                 "KASUSD": {...}, "KASEUR": {...}}}
```

**Confirmed by that measurement:** all four pairs come back from one request, no API key is
involved, and every price is a JSON **string** -- the last traded price is `c[0]`, where `c`
is `[price, lot volume]`.

**Assumed, and written down as an assumption:** that the key of each entry in `result` is
the same pair code the request asked for. The codes below are Kraken's own canonical
spellings, so the vendor has nothing to normalise them into -- but the documentation does
not promise the identity, and a vendor that started answering under a different key would
break the correlation. **The failure mode of that assumption is a pair going unanswered and
falling over to Coinbase, never a price attached to the wrong asset**, because an entry
whose key is not one we asked for is refused rather than matched by position.

**Not confirmed:** any rate limit on this endpoint. Kraken documents call-rate limits for
its private endpoints and a counter for its order-book endpoints; nothing states a monthly
quota for the public ticker, and none was measured. The shared `HostRateLimiter` floor of
one request per second per host therefore applies, which is orders of magnitude above what
an hourly refresh needs.

There is no `Retry-After` handling here and no retry decision: both belong to the shared
transport, and a 429 that survives it arrives as `ProviderRateLimitedError` and moves the
failover on to the next source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from portfolio.providers.base import require_json_object
from portfolio.providers.endpoints import PRIMARY, EndpointSet
from portfolio.providers.errors import ProviderResponseError
from portfolio.providers.http import ASSET_PRICES
from portfolio.providers.prices.base import (
    BTC,
    EUR,
    KAS,
    USD,
    PriceQuote,
    require_price,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from decimal import Decimal

    import httpx

    from portfolio.config import Settings
    from portfolio.providers.prices.base import PricePair

__all__ = [
    "KRAKEN",
    "KRAKEN_API_URL",
    "PAIR_CODES",
    "TICKER_PATH",
    "KrakenPriceSource",
    "parse_ticker",
]

KRAKEN: Final = "kraken"
"""What this source is called in `prices.source` and in a log. The brand, never a host."""

KRAKEN_API_URL: Final = "https://api.kraken.com"
"""The public API root, a module constant rather than a setting.

Every other base URL in this application is configurable because a self-hoster can run
their own instance of the software behind it -- an Esplora index, a kaspa-rest-server. There
is no self-hosted Kraken and no second operator of this API, so a `PORTFOLIO_KRAKEN_URL`
would be a variable whose only correct value is this one, plus a startup validation path and
a row in the operations table for it. `docs/providers.md` records promoting these to
settings as work a real need should drive.
"""

TICKER_PATH: Final = "/0/public/Ticker"
"""Confirmed against the live service on 2026-09-23. Version `0` is in the path, not a header."""

VENDOR: Final = "Kraken"
"""What the upstream is called in an exhaustion message: the brand, never a host."""

PAIR_CODES: Final[Mapping[PricePair, str]] = {
    (BTC, USD): "XXBTZUSD",
    (BTC, EUR): "XXBTZEUR",
    (KAS, USD): "KASUSD",
    (KAS, EUR): "KASEUR",
}
"""Our `(symbol, currency)` to Kraken's pair code. All four measured on 2026-09-23.

Kraken's older assets carry the `X`/`Z` class prefixes -- `XXBT` is bitcoin, `ZUSD` is the
US dollar -- and newer listings such as KAS do not. **The codes are not derivable from the
symbols**, which is why this is a table and not a format string: `XXBTZUSD` and `KASUSD`
follow different rules, and a rule inferred from one of them would be wrong for the other.

This mapping is also the source's `pairs` declaration, so a pair added to the table becomes
a pair this source will be asked for, in one edit.
"""

ERROR_FIELD: Final = "error"
RESULT_FIELD: Final = "result"
LAST_TRADE_FIELD: Final = "c"
"""The response field names, written down once. `c` is `[price, lot volume]`, and the price
is `c[0]`: the last trade, which is the number a portfolio wants -- a mid-point built from
the bid and the ask would be a figure no trade ever happened at."""


def parse_ticker(body: str | bytes, requested: Mapping[str, PricePair]) -> tuple[PriceQuote, ...]:
    """The quotes out of one ticker response, or a refusal.

    Hand-written rather than a pydantic model, for the reason the chain parsers give: the
    interesting part is which shapes are refused, and a model spreads that across a
    validator, a config and an exception translation.

    `requested` maps Kraken's pair code to our pair, so correlation is by key and never by
    position. An entry under a code nobody asked for is refused outright rather than
    dropped: a vendor answering about something we did not request is a correlation bug --
    a cached response for another caller, a mis-parsed query -- and dropping it silently
    would hide that behind prices that still look plausible.

    **A pair that is simply absent is not an error.** Kraken answering about three of the
    four codes leaves the fourth to the next source, which is what failover is for; only an
    answer that cannot be trusted stops the call.

    The refusals, and why each is one:

    | Body | Why it is a refusal |
    |---|---|
    | not a JSON object | a 5xx HTML page never reaches here; a 200 that is not JSON is a lie |
    | `error` absent or not a list | not the envelope measured, so nothing in it is trusted |
    | `error` non-empty | Kraken answers 200 and reports its own failures in this list |
    | `result` absent or not an object | same as the `error` case |
    | an entry that is not an object | a pair we asked about answered with something else |
    | `c` absent, not a list, or empty | a guess at another field would be a different number |
    | a price that is not positive and finite | `require_price` decides, once, for four vendors |
    | an entry under an unrequested code | a correlation bug; see above |

    Raises:
        ProviderResponseError: the body is not JSON, is not the documented envelope, or
            carries an entry that cannot be trusted.
    """
    document = require_json_object(body)

    reported = document.get(ERROR_FIELD)
    if not isinstance(reported, list):
        message = (
            f"The {VENDOR} response has no {ERROR_FIELD!r} list, so it is not the envelope "
            "this endpoint documents."
        )
        raise ProviderResponseError(message)
    if reported:
        # Kraken answers 200 and reports the failure in this list, so a status check alone
        # would read an error document as an empty result and silently report no prices.
        # The count and nothing else: the entries are vendor prose and go in no message.
        message = f"The {VENDOR} response carries {len(reported)} error(s) in its envelope."
        raise ProviderResponseError(message)

    result = document.get(RESULT_FIELD)
    if not isinstance(result, dict):
        message = (
            f"The {VENDOR} response has no {RESULT_FIELD!r} object, so it is not the "
            "envelope this endpoint documents."
        )
        raise ProviderResponseError(message)

    unexpected = len(set(result) - set(requested))
    if unexpected:
        message = (
            f"The {VENDOR} response carries {unexpected} pair(s) that were not requested, "
            "so it cannot be matched to the request."
        )
        raise ProviderResponseError(message)

    quotes: list[PriceQuote] = []
    for code, pair in requested.items():
        entry = result.get(code)
        if entry is None:
            # Absent is a pair for the next source, not a refusal. See the docstring.
            continue
        quotes.append(
            PriceQuote(
                asset_symbol=pair[0],
                quote_currency=pair[1],
                amount=_last_trade_price(entry, code),
                source=KRAKEN,
            )
        )
    return tuple(quotes)


def _last_trade_price(entry: object, code: str) -> Decimal:
    """`c[0]` out of one ticker entry, refusing every shape that is not that.

    The code is named in a refusal and the value never is. A pair code is public vendor
    vocabulary; keeping the value out is the habit that keeps a response body out of a log
    at the boundaries where the body is the owner's holdings.

    Raises:
        ProviderResponseError: the entry is not an object, has no usable `c`, or the price
            in it is not one.
    """
    if not isinstance(entry, dict):
        message = (
            f"The {VENDOR} entry for {code} is a {type(entry).__name__} rather than the "
            "object this endpoint documents."
        )
        raise ProviderResponseError(message)
    last = entry.get(LAST_TRADE_FIELD)
    if not isinstance(last, list) or not last:
        message = (
            f"The {VENDOR} entry for {code} has no non-empty {LAST_TRADE_FIELD!r} list, "
            "which is where the last traded price is."
        )
        raise ProviderResponseError(message)
    return require_price(last[0], source=VENDOR)


class KrakenPriceSource:
    """Reads every configured pair from Kraken's public ticker in one request.

    Satisfies `PriceSource` structurally, checked by `mypy --strict` rather than by
    `isinstance`.

    No fallback instance and no second operator: `EndpointSet` is used with the single
    configured endpoint anyway, because it already classifies a non-200 into this package's
    three error types with a message that names the vendor and never the URL. Writing that
    again here would be a second copy of a rule review has already corrected once.
    """

    def __init__(self, client: httpx.AsyncClient, *, settings: Settings | None = None) -> None:
        """Bind to the shared client.

        `settings` is accepted and unused so that every source in this package is built the
        same way -- `price_sources` calls all four with the same two arguments, and a source
        whose constructor is special is one a future edit gets wrong. Kraken needs no
        configuration: no key, and a base URL with exactly one correct value.
        """
        del settings  # No configuration: no key, and one correct base URL.
        self._endpoint = EndpointSet.configured(client, ((PRIMARY, KRAKEN_API_URL),), vendor=VENDOR)

    @property
    def name(self) -> str:
        """`kraken`, the string written to `prices.source`."""
        return KRAKEN

    @property
    def pairs(self) -> frozenset[PricePair]:
        """Every pair in `PAIR_CODES` -- which is all four this product prices.

        Read off the table by name at call time rather than copied onto the instance, so
        that adding a pair to `PAIR_CODES` is the whole of adding it here.
        """
        return frozenset(PAIR_CODES)

    async def fetch(self, pairs: Sequence[PricePair]) -> Sequence[PriceQuote]:
        """Every requested pair in **one** request, which is the whole budget argument.

        The pair codes go in the query string as a comma-separated list, which is what the
        measurement on 2026-09-23 exercised. A query string is normally the thing this
        package is careful about -- `http.py` warns that `strip_query` meets the letter of
        the logging rule and leaks anyway -- and it is harmless here for a reason worth
        stating rather than assuming: this query carries four public asset codes, no
        credential and nothing about the owner. The transport logs `request_target`, which
        renders a scheme, a host and an endpoint label and never the query at all.

        An empty request is not made: with nothing to ask, there is nothing to ask for, and
        a request for zero pairs would be a call against a vendor for no answer.

        Raises:
            ProviderRateLimitedError: a 429 that survived the transport's retries.
            ProviderUnavailableError: the vendor did not answer, or failed with a 5xx.
            ProviderResponseError: it refused the request, or answered with something that
                cannot be trusted.
        """
        requested = {PAIR_CODES[pair]: pair for pair in pairs if pair in PAIR_CODES}
        if not requested:
            return ()
        query = ",".join(requested)
        body, _index = await self._endpoint.read(f"{TICKER_PATH}?pair={query}", ASSET_PRICES)
        return parse_ticker(body, requested)
