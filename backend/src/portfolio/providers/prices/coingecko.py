"""CoinGecko's simple price, batched, keyed -- and the one source nothing here has measured.

The last source for every pair, and the only one that needs a credential. **It is also the
only parser in this package written from documentation rather than from a response**, and
the distinction is recorded here because the next person cannot tell the two apart and would
trust them equally.

## What was read from the vendor's documentation on 2026-09-23, and what that does not mean

* Demo API root: `https://api.coingecko.com/api/v3/`, distinct from the Pro root
  `https://pro-api.coingecko.com/api/v3/`.
* Demo key header: `x-cg-demo-api-key`. The documented query-parameter alternative is
  `x_cg_demo_api_key` and **this module does not use it**; see below.
* `GET /api/v3/simple/price` takes `vs_currencies` (required, comma-separated) and `ids`
  (comma-separated CoinGecko coin ids), plus optional flags this module does not send.
  `precision` accepts `0`-`18` or `full`; `full` is what is sent, because the default
  rounds and a rounded price is a price we were not given.
* The response is an object keyed by coin id, each value an object keyed by the lower-case
  currency: `{"bitcoin": {"usd": 86123.45, "eur": 79211.02}}`.
* "Each successful request (HTTP 200) deducts 1 credit from your monthly quota."

**None of that was exercised against the live service**, because doing so needs a key and
rule 3 forbids this repository from containing one. The shape above is what the parser was
written against; a response that differs from it is refused as untrustworthy rather than
mis-parsed, which is the direction to be wrong in, but it is a refusal that would arrive in
production rather than in a test. `docs/providers.md` records this as the one parser that
meets a real server for the first time on the day it is needed.

**The numbers are JSON numbers, not strings** -- the documented example shows
`"usd": 76975` -- so this is a second float boundary, and it is closed by the same
`parse_float=Decimal` in `providers.base.decode_json` that closes the Kaspa one. It is a
different vendor and a different shape, and the fact that one line covers both is the whole
argument for putting it in the shared decoder.

## The key travels in a header, and never in the query string

Both spellings are documented and they are not equivalent. A key in a query string is
recorded by the vendor's own access logs, by every intermediary, and by anything that
renders a URL -- a traceback, an error message, a metrics label. `providers/http.py` warns
by name that `strip_query` meets the letter of this application's logging rule and leaks
anyway. The header is passed per request through `EndpointSet.read`, so it is attached to
the one call that needs it and is never set on the shared client, where it would be sent to
every vendor every other provider talks to.

**The key is never logged, never persisted and never returned by an endpoint.** It lives in
a `SecretStr` in `Settings` and is read out of it once per request, at the moment the header
is built.

## Without a key this class is never constructed

`price_sources` omits it from the source tuple, and the constructor refuses to build one
without a key. Criterion 5 asks that the source be **absent rather than skipped**: a source
that exists and checks for a key at call time is one line away from a caller that reaches
past the check, and "the object does not exist" is the only version of that guarantee which
cannot be defeated later.

**Not confirmed:** the Demo plan's monthly credit cap and per-minute rate. The
documentation states that credits and rate limits depend on the plan and points at a pricing
page; the figures in `docs/providers.md` -- 10,000 a month, 100 a minute -- come from the
issue rather than from a page read here, and are recorded there as such. They are far above
an hourly refresh's needs either way, and this source is a fallback that is only asked when
the primary has already failed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from portfolio.config import get_settings
from portfolio.providers.base import require_json_object
from portfolio.providers.endpoints import PRIMARY, EndpointSet
from portfolio.providers.errors import ProviderResponseError
from portfolio.providers.http import ASSET_PRICES
from portfolio.providers.prices.base import (
    BTC,
    EUR,
    KAS,
    SUPPORTED_PAIRS,
    USD,
    PriceQuote,
    require_price,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import httpx

    from portfolio.config import Settings
    from portfolio.providers.prices.base import PricePair

__all__ = [
    "API_KEY_HEADER",
    "COINGECKO",
    "COINGECKO_DEMO_API_URL",
    "COIN_IDS",
    "SIMPLE_PRICE_PATH",
    "CoinGeckoPriceSource",
    "parse_simple_price",
]

COINGECKO: Final = "coingecko"
"""What this source is called in `prices.source` and in a log. The brand, never a host."""

COINGECKO_DEMO_API_URL: Final = "https://api.coingecko.com"
"""The **Demo** API root. The Pro plan has a different host and a different header name.

A module constant for the reason `kraken.KRAKEN_API_URL` gives, with one extra consequence
worth stating: supporting the Pro plan is a second host *and* a second header, so it is a
deliberate change here rather than an operator pointing a variable somewhere new. A Demo key
sent to the Pro host is rejected, and a variable that lets an operator make that mistake
silently is a variable worth not having.
"""

SIMPLE_PRICE_PATH: Final = "/api/v3/simple/price"
"""Read from the vendor's documentation on 2026-09-23. Not exercised; see the module docstring."""

API_KEY_HEADER: Final = "x-cg-demo-api-key"
"""The Demo plan's documented header. The Pro plan's is `x-cg-pro-api-key`.

The documented query-parameter alternative is deliberately unused; see the module docstring.
"""

VENDOR: Final = "CoinGecko"
"""What the upstream is called in an exhaustion message: the brand, never a host."""

FULL_PRECISION: Final = "full"
"""What `precision` is set to, so the vendor returns the digits it has rather than rounding.

The default is documented as a rounded value. A price that has been rounded before it
reaches us is a price we cannot un-round, and rule 2's whole subject is a monetary value
losing digits somewhere nobody looks.
"""

COIN_IDS: Final[Mapping[str, str]] = {
    BTC: "bitcoin",
    KAS: "kaspa",
}
"""Our asset symbol to CoinGecko's coin id, which is not the symbol and not derivable from it.

The vendor keys its responses by id, and several unrelated assets share a ticker symbol on
it -- which is the reason the id exists. A table rather than `symbol.lower()`, so that the
day an id is not the lower-cased name the mapping is a line to edit rather than a rule to
discover.
"""

CURRENCY_CODES: Final[Mapping[str, str]] = {
    USD: "usd",
    EUR: "eur",
}
"""Our currency code to the lower-case form the vendor keys its response by.

Written down rather than `currency.lower()` for the same reason as `COIN_IDS`: the mapping
is between two vocabularies, and a transformation that happens to work today is not a
statement that they are the same vocabulary.
"""


def parse_simple_price(
    body: str | bytes,
    requested: Sequence[PricePair],
) -> tuple[PriceQuote, ...]:
    """The quotes out of one simple-price response, or a refusal.

    Correlation is by the two keys the vendor uses -- coin id, then lower-case currency --
    translated back into our vocabulary through `COIN_IDS` and `CURRENCY_CODES`, never by
    position. A pair that is absent from the document is left for nobody: this is the last
    source, so an absent pair simply comes back unanswered and becomes a reason.

    **An entry for a coin id nobody asked about is refused**, for the reason `align_balances`
    refuses an address nobody asked about: a vendor answering a question we did not put is a
    correlation failure, and dropping it silently would hide that behind prices which still
    look plausible. A *currency* nobody asked about within a requested coin is not refused,
    because the documented request carries `vs_currencies` for the whole call rather than per
    coin -- asking for BTC/USD and KAS/EUR necessarily asks both currencies of both coins,
    so the extra entries are ours and not the vendor's.

    Raises:
        ProviderResponseError: the body is not a JSON object, carries a coin nobody asked
            about, or holds a value that is not a price.
    """
    document = require_json_object(body)

    asked_ids = {COIN_IDS[symbol] for symbol, _currency in requested if symbol in COIN_IDS}
    unexpected = len(set(document) - asked_ids)
    if unexpected:
        message = (
            f"The {VENDOR} response carries {unexpected} coin(s) that were not requested, "
            "so it cannot be matched to the request."
        )
        raise ProviderResponseError(message)

    quotes: list[PriceQuote] = []
    for asset_symbol, quote_currency in requested:
        entry = document.get(COIN_IDS.get(asset_symbol, ""))
        if not isinstance(entry, dict):
            # Absent, or present as something that is not an object. Either way this pair
            # is unanswered rather than wrong: there is no number here to misread.
            continue
        reported = entry.get(CURRENCY_CODES.get(quote_currency, ""))
        if reported is None:
            continue
        quotes.append(
            PriceQuote(
                asset_symbol=asset_symbol,
                quote_currency=quote_currency,
                amount=require_price(reported, source=VENDOR),
                source=COINGECKO,
            )
        )
    return tuple(quotes)


class CoinGeckoPriceSource:
    """Reads every requested pair from CoinGecko's simple price, in one keyed request.

    Satisfies `PriceSource` structurally, checked by `mypy --strict` rather than by
    `isinstance`.

    **Cannot be constructed without a key**, which is what makes criterion 5's "absent, not
    skipped" a property of the object graph rather than of a branch somebody remembered to
    write.
    """

    def __init__(self, client: httpx.AsyncClient, *, settings: Settings | None = None) -> None:
        """Bind to the shared client and take the key out of the settings once.

        Raises:
            ValueError: no CoinGecko key is configured. `price_sources` never reaches this,
                because it omits the source entirely; the raise is for a caller that
                constructs one directly, and it is a refusal rather than a source that
                silently sends a blank credential to a vendor on every refresh.
        """
        resolved = settings if settings is not None else get_settings()
        if resolved.coingecko_api_key is None:
            message = (
                "PORTFOLIO_COINGECKO_API_KEY is not set, so the CoinGecko price source "
                "cannot be built. It is absent from the source list rather than skipped."
            )
            raise ValueError(message)
        # Held as the `SecretStr` rather than as the string it wraps: the secret is read out
        # of it at the one moment a header is built, so an instance of this class that
        # reaches a repr or a traceback carries a masked value rather than the key.
        self._api_key = resolved.coingecko_api_key
        self._endpoint = EndpointSet.configured(
            client, ((PRIMARY, COINGECKO_DEMO_API_URL),), vendor=VENDOR
        )

    @property
    def name(self) -> str:
        """`coingecko`, the string written to `prices.source`."""
        return COINGECKO

    @property
    def pairs(self) -> frozenset[PricePair]:
        """Every pair this product prices. This is the only source that lists all four."""
        return SUPPORTED_PAIRS

    async def fetch(self, pairs: Sequence[PricePair]) -> Sequence[PriceQuote]:
        """Every requested pair in one keyed request.

        The ids and the currencies are each sent once, de-duplicated and sorted, so that two
        pairs of one coin cost one id rather than two and the query is stable between runs --
        which matters for a vendor sitting behind a cache and for a human reading two logs
        side by side.

        The query string carries public coin ids, currency codes and a precision flag. **The
        key is not in it**; it is a header, for the reasons the module docstring gives.

        Raises:
            ProviderRateLimitedError: a 429 that survived the transport's retries.
            ProviderUnavailableError: the vendor did not answer, or failed with a 5xx.
            ProviderResponseError: it refused -- a wrong or exhausted key is this -- or
                answered with something that cannot be trusted.
        """
        wanted = [pair for pair in pairs if pair in SUPPORTED_PAIRS]
        if not wanted:
            return ()
        ids = sorted({COIN_IDS[symbol] for symbol, _currency in wanted})
        currencies = sorted({CURRENCY_CODES[currency] for _symbol, currency in wanted})
        query = (
            f"ids={','.join(ids)}&vs_currencies={','.join(currencies)}&precision={FULL_PRECISION}"
        )
        body, _index = await self._endpoint.read(
            f"{SIMPLE_PRICE_PATH}?{query}",
            ASSET_PRICES,
            headers={API_KEY_HEADER: self._api_key.get_secret_value()},
        )
        return parse_simple_price(body, wanted)
