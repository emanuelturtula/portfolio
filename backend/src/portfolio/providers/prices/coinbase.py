"""Coinbase's spot price, one request per pair, and bitcoin only.

The first fallback for the two BTC pairs. Measured against the live service on
**2026-09-23**:

```
GET https://api.coinbase.com/v2/prices/BTC-USD/spot
-> 200, {"data": {"amount": "86123.45", "base": "BTC", "currency": "USD"}}
GET https://api.coinbase.com/v2/prices/KAS-USD/spot   -> 404
GET https://api.coinbase.com/v2/prices/KAS-EUR/spot   -> 404
```

**Confirmed by that measurement:** BTC/USD and BTC/EUR both answer, no API key is involved,
and the amount is a JSON **string** under `data.amount`.

**`KAS` is not listed at all, on either currency**, and that single fact is why this package
has a per-pair source order instead of one global failover chain. A chain that tried
Coinbase for KAS would spend a request on a 404 forever, on every refresh, and would report
"every source failed" for a pair only one source was ever eligible to answer. `pairs` below
declares the two it can do, so the pair it cannot is never asked.

**Not confirmed:** any rate limit on this endpoint. Coinbase publishes limits for its
authenticated APIs; nothing was found for this unauthenticated one and nothing was measured.
The shared per-host floor applies.

**One request per pair, and that is why it is not the primary.** The endpoint takes exactly
one pair in its path -- there is no batch form -- so valuing both BTC pairs from here costs
two requests where Kraken costs part of one. It is a fallback that is only reached when the
primary has failed, so the cost is paid on a bad day rather than every hour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from portfolio.providers.base import require_json_object
from portfolio.providers.endpoints import PRIMARY, EndpointSet
from portfolio.providers.errors import ProviderError, ProviderResponseError
from portfolio.providers.http import ASSET_PRICE
from portfolio.providers.prices.base import (
    BTC,
    EUR,
    USD,
    PriceQuote,
    require_price,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx

    from portfolio.config import Settings
    from portfolio.providers.prices.base import PricePair

__all__ = [
    "COINBASE",
    "COINBASE_API_URL",
    "SPOT_PAIRS",
    "SPOT_PATH",
    "CoinbasePriceSource",
    "parse_spot",
]

COINBASE: Final = "coinbase"
"""What this source is called in `prices.source` and in a log. The brand, never a host."""

COINBASE_API_URL: Final = "https://api.coinbase.com"
"""The public API root, a module constant for the reason `kraken.KRAKEN_API_URL` gives."""

SPOT_PATH: Final = "/v2/prices/{pair}/spot"
"""Confirmed on 2026-09-23. `{pair}` is `BASE-QUOTE`, both upper case, joined by a hyphen.

Built only from `SPOT_PAIRS` below, never from a caller's string. The formatting is a path
interpolation and the values are two constants from this package, so nothing a vendor or a
database row chose is ever spliced into a URL here -- the rule `chains/kaspa.py` states at
length about validating an address before a URL is built out of it, applied where the
interpolated value is not user data at all and the habit is kept anyway.
"""

VENDOR: Final = "Coinbase"
"""What the upstream is called in an exhaustion message: the brand, never a host."""

SPOT_PAIRS: Final[frozenset[PricePair]] = frozenset({(BTC, USD), (BTC, EUR)})
"""The two pairs this endpoint answers, measured. KAS is a 404 on both currencies.

A declaration rather than a discovery: a pair that is not in here costs no request, which
is the only way a 404 that will never stop being a 404 can be avoided rather than retried.
"""

DATA_FIELD: Final = "data"
AMOUNT_FIELD: Final = "amount"
BASE_FIELD: Final = "base"
CURRENCY_FIELD: Final = "currency"
"""The response field names, written down once.

`base` and `currency` are read and checked rather than ignored. The pair is in the **path**,
so a cache or a proxy answering with the wrong document is exactly the failure that would
otherwise attach one asset's price to another -- the same reason `chains/kaspa.py` checks
the echoed address, and there the vendor was measured to sit behind a CDN.
"""


def parse_spot(body: str | bytes, pair: PricePair) -> PriceQuote:
    """The quote out of one spot response, or a refusal.

    **The echoed `base` and `currency` are checked against what was asked**, and that is the
    load-bearing part of this parser rather than a courtesy. The pair travels in the path,
    which means an intermediary cache is one mis-keyed entry away from answering a BTC-EUR
    request with a BTC-USD document -- and a price in the wrong currency is not an error
    anybody downstream can see. It is a plausible number attached to the wrong holding,
    which is the shape of failure criterion 3 exists to refuse.

    The comparison is case-sensitive and against the constants this module sent, because
    those constants are what built the path.

    Raises:
        ProviderResponseError: the body is not the documented envelope, the echoed pair is
            not the requested one, or the amount is not a price.
    """
    document = require_json_object(body)
    data = document.get(DATA_FIELD)
    if not isinstance(data, dict):
        message = (
            f"The {VENDOR} response has no {DATA_FIELD!r} object, so it is not the envelope "
            "this endpoint documents."
        )
        raise ProviderResponseError(message)

    asset_symbol, quote_currency = pair
    echoed = (data.get(BASE_FIELD), data.get(CURRENCY_FIELD))
    if echoed != (asset_symbol, quote_currency):
        # Names the pair that was asked for and never the pair that came back: the first is
        # ours, the second is whatever an intermediary chose, and repeating it in a message
        # is repeating a response body.
        message = (
            f"The {VENDOR} response is not about {asset_symbol}/{quote_currency}, so it "
            "cannot be matched to the request."
        )
        raise ProviderResponseError(message)

    return PriceQuote(
        asset_symbol=asset_symbol,
        quote_currency=quote_currency,
        amount=require_price(data.get(AMOUNT_FIELD), source=VENDOR),
        source=COINBASE,
    )


class CoinbasePriceSource:
    """Reads BTC spot prices from Coinbase, one request per pair.

    Satisfies `PriceSource` structurally, checked by `mypy --strict` rather than by
    `isinstance`.
    """

    def __init__(self, client: httpx.AsyncClient, *, settings: Settings | None = None) -> None:
        """Bind to the shared client. No key and no configurable URL; see `kraken.py`."""
        del settings  # No configuration: no key, and one correct base URL.
        self._endpoint = EndpointSet.configured(
            client, ((PRIMARY, COINBASE_API_URL),), vendor=VENDOR
        )

    @property
    def name(self) -> str:
        """`coinbase`, the string written to `prices.source`."""
        return COINBASE

    @property
    def pairs(self) -> frozenset[PricePair]:
        """The two BTC pairs. KAS is a measured 404 and is therefore never asked for."""
        return SPOT_PAIRS

    async def fetch(self, pairs: Sequence[PricePair]) -> Sequence[PriceQuote]:
        """One request per pair, and **a pair that fails does not take the others with it**.

        Sequential, never `gather`, for the reason `chains/kaspa.py` gives: a `gather` hands
        the shared limiter every acquisition at once and turns a per-host floor into a queue
        whose depth nobody bounded.

        A `ProviderError` on one pair is caught and that pair is simply left out of the
        answer, which is the partial answer `fetch_prices` is built to accept. Letting it
        propagate would throw away a BTC/USD price that had already arrived because BTC/EUR
        failed -- and the caller would move on to the next source for *both*, having already
        paid for one of them. This is the one place in this package where a refusal is
        swallowed rather than raised, and it is swallowed because the loop above treats a
        missing pair correctly.

        A pair outside `SPOT_PAIRS` is skipped without a request: `fetch_prices` filters on
        `pairs` already, and this is the second guard for a caller that does not.
        """
        quotes: list[PriceQuote] = []
        for pair in pairs:
            if pair not in SPOT_PAIRS:
                continue
            try:
                quotes.append(await self._fetch_one(pair))
            except ProviderError:
                # One pair's failure leaves the others; see the docstring. Nothing is logged
                # here, as nowhere in this package logs.
                continue
        return tuple(quotes)

    async def _fetch_one(self, pair: PricePair) -> PriceQuote:
        """One pair, one request.

        Raises:
            ProviderRateLimitedError: a 429 that survived the transport's retries.
            ProviderUnavailableError: the vendor did not answer, or failed with a 5xx.
            ProviderResponseError: it refused -- a 404 for a pair it does not list is this
                -- or answered with something that cannot be trusted.
        """
        asset_symbol, quote_currency = pair
        path = SPOT_PATH.format(pair=f"{asset_symbol}-{quote_currency}")
        body, _index = await self._endpoint.read(path, ASSET_PRICE)
        return parse_spot(body, pair)
