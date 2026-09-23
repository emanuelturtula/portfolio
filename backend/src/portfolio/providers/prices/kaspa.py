"""The Kaspa REST server's own price endpoint: one asset, one assumed currency, a JSON number.

The last key-free source for KAS/USD, and **the only one in this package whose quote
currency is a guess rather than a fact**. Measured against the live service on
**2026-09-23**:

```
GET https://api.kaspa.org/info/price
-> 200, {"price": 0.04228645}
```

**Confirmed by that measurement:** the endpoint answers without a key, and the body is a
single-key object whose value is a JSON **number** -- not a string, which is what Kraken and
Coinbase both send and what a parser written from those two would expect.

**Assumed, and this is the assumption the module exists to make visible: the currency is
USD.** The body names no currency. The documentation names no currency. USD is an inference
from the number's magnitude against the market on the day it was measured, which is not
evidence -- it is the shape of reasoning that would read a EUR price as a USD one and be
wrong by a few percent, invisibly, forever.

Two things follow, and both are deliberate rather than cautious:

* **It is last for the one pair it can answer and absent from every other.** KAS/USD is
  `Kraken, Kaspa, CoinGecko`; KAS/EUR does not list this source at all. So the guess is only
  ever used when Kraken -- which *does* name its currencies -- has already failed.
* **It never answers a EUR pair**, not even by converting. `docs/providers.md` records the
  assumption and the date beside it.

Using a price whose currency is a guess to value somebody's holdings is precisely the
failure criterion 3 describes, which is why the guess is confined to one pair, placed
behind a source that knows, and written down in three places rather than one.

**This is the float boundary that made rule 2 the subject of this issue.** `json.loads`
would turn `0.04228645` into a `float` before any code here ran, and the value would already
not be the one the vendor sent. `providers.base.decode_json` passes `parse_float=Decimal`, so
it arrives as `Decimal("0.04228645")` -- built from the literal text on the wire.

**Not confirmed:** any rate limit, any cache header on this path specifically, and whether
the number is a spot price, a volume-weighted average, or an aggregate of exchanges. The
balance endpoints on this host were measured to sit behind Cloudflare with an eight-second
`Cache-Control`; nothing was measured for this one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from portfolio.config import get_settings
from portfolio.providers.base import require_json_object
from portfolio.providers.endpoints import FALLBACK, PRIMARY, EndpointSet
from portfolio.providers.http import ASSET_PRICE
from portfolio.providers.prices.base import KAS, USD, PriceQuote, require_price

if TYPE_CHECKING:
    from collections.abc import Sequence

    import httpx

    from portfolio.config import Settings
    from portfolio.providers.prices.base import PricePair

__all__ = [
    "ASSUMED_CURRENCY",
    "KASPA",
    "PRICE_FIELD",
    "PRICE_PAIRS",
    "PRICE_PATH",
    "KaspaPriceSource",
    "parse_price",
]

KASPA: Final = "kaspa"
"""What this source is called in `prices.source` and in a log.

Deliberately the same word the chain provider uses for its key, because it is the same
vendor and the same host; what differs is which endpoint on it was asked.
"""

PRICE_PATH: Final = "/info/price"
"""Confirmed against the live service on 2026-09-23."""

VENDOR: Final = "Kaspa REST"
"""What the upstream is called in an exhaustion message: the software's name, never a host.

Spelled out here rather than imported from `providers/chains/kaspa.py`. The two modules
describe the same vendor and share a base URL setting, and importing one from the other to
save a line would make every import of this package register a chain provider as a side
effect -- a coupling with nothing to gain and an import-order surprise to lose.
"""

PRICE_FIELD: Final = "price"
"""The one field in the body. Written down once, for the reason every other parser in this
package writes its field names down once: a typo here refuses every well-formed response."""

ASSUMED_CURRENCY: Final = USD
"""**The currency this endpoint's number is assumed to be in. It is not stated anywhere.**

A named constant rather than `USD` written inline at the call site, so that the assumption
has somewhere to be documented and something for a test to assert against. If the vendor
ever names a currency, this constant is deleted rather than edited -- the value would come
from the body instead.
"""

PRICE_PAIRS: Final[frozenset[PricePair]] = frozenset({(KAS, ASSUMED_CURRENCY)})
"""KAS in the assumed currency, and nothing else. No EUR pair, by construction."""


def parse_price(body: str | bytes) -> PriceQuote:
    """The quote out of one price response, or a refusal.

    The parser is four lines and the interesting part is what it does **not** do: it does
    not read a currency, because there is none in the body, and it does not convert. The
    currency comes from `ASSUMED_CURRENCY`, which is where the guess is recorded.

    `require_price` is what makes the JSON number safe, and it is safe because the number
    was already a `Decimal` when it arrived -- `providers.base.decode_json` decided that,
    not this function. A parser cannot repair a value a parser already damaged, which is
    why the hook is in the shared decoder.

    Raises:
        ProviderResponseError: the body is not a JSON object, or `price` is not a positive
            finite number.
    """
    document = require_json_object(body)
    return PriceQuote(
        asset_symbol=KAS,
        # Assumed, not read. The body names no currency; see the module docstring.
        quote_currency=ASSUMED_CURRENCY,
        amount=require_price(document.get(PRICE_FIELD), source=VENDOR),
        source=KASPA,
    )


def _configured_candidates(settings: Settings) -> tuple[tuple[str, str], ...]:
    """The two configured kaspa-rest-server URLs, in order, as `(position, url)`.

    The **same** two settings the chain provider reads, because it is the same host and an
    operator who points the balance reads at their own instance has pointed the price read
    there too. A third pair of variables for the same server would be two answers to one
    question, and the one somebody forgot to set would be the public instance.
    """
    return (
        (PRIMARY, settings.kaspa_api_url),
        (FALLBACK, settings.kaspa_api_fallback_url),
    )


class KaspaPriceSource:
    """Reads KAS in an assumed currency from a kaspa-rest-server instance.

    Satisfies `PriceSource` structurally, checked by `mypy --strict` rather than by
    `isinstance`.
    """

    def __init__(self, client: httpx.AsyncClient, *, settings: Settings | None = None) -> None:
        """Bind to the shared client and read the configuration once.

        Once, here, rather than per request, for the reason the chain provider gives: a
        source whose base URL could change between two calls would produce readings from two
        different instances with nothing saying so.
        """
        resolved = settings if settings is not None else get_settings()
        self._endpoints = EndpointSet.configured(
            client, _configured_candidates(resolved), vendor=VENDOR
        )

    @property
    def name(self) -> str:
        """`kaspa`, the string written to `prices.source`."""
        return KASPA

    @property
    def pairs(self) -> frozenset[PricePair]:
        """KAS in the assumed currency only. Never a EUR pair; see the module docstring."""
        return PRICE_PAIRS

    async def fetch(self, pairs: Sequence[PricePair]) -> Sequence[PriceQuote]:
        """One request, for the one pair this source can answer.

        `pairs` is checked rather than trusted: `fetch_prices` has already filtered it, and
        a caller that has not must not make this source answer a question about a pair it
        was never measured for. The check is the cheap half of the currency assumption --
        the expensive half would be returning a KAS/EUR quote built from a number nobody
        said was in EUR.

        Raises:
            ProviderRateLimitedError: a 429 that survived the transport's retries.
            ProviderUnavailableError: no instance answered, or the last failed with a 5xx.
            ProviderResponseError: an instance refused, or answered with something that
                cannot be trusted.
        """
        if not any(pair in PRICE_PAIRS for pair in pairs):
            return ()
        body, _index = await self._endpoints.read(PRICE_PATH, ASSET_PRICE)
        return (parse_price(body),)
