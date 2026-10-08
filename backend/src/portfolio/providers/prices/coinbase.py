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

## Daily candles, from a different host (spec 038)

`CoinbaseDailyCloses`, at the bottom, reads BTC/USD daily candles from **Coinbase Exchange**,
`api.exchange.coinbase.com` -- a different API on a different host from the spot price above.
Kraken keeps 720 daily candles; Coinbase Exchange has BTC-USD from 2015-07-20 with no missing
day, measured on 2026-10-08, so the backfill asks it for the days before the earliest close
it has stored and nothing else (R8). There is no KAS product on Coinbase Exchange either.
"""

from __future__ import annotations

from datetime import UTC, date, timedelta
from typing import TYPE_CHECKING, Final

from portfolio.providers.base import decode_json, require_json_object
from portfolio.providers.endpoints import PRIMARY, EndpointSet
from portfolio.providers.errors import (
    ProviderError,
    ProviderRateLimitedError,
    ProviderResponseError,
)
from portfolio.providers.http import ASSET_DAILY_CLOSES, ASSET_PRICE, utc_now
from portfolio.providers.prices.base import (
    BTC,
    EUR,
    USD,
    DailyClose,
    PriceQuote,
    require_price,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from decimal import Decimal

    import httpx

    from portfolio.config import Settings
    from portfolio.providers.prices.base import PricePair

__all__ = [
    "CANDLES_PATH",
    "CANDLE_PRODUCTS",
    "COINBASE",
    "COINBASE_API_URL",
    "COINBASE_EXCHANGE_API_URL",
    "DAILY_GRANULARITY_SECONDS",
    "EARLIEST_CANDLE_DAY",
    "MAX_DAYS_PER_REQUEST",
    "SPOT_PAIRS",
    "SPOT_PATH",
    "CoinbaseDailyCloses",
    "CoinbasePriceSource",
    "candle_windows",
    "parse_candles",
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

        **A rate limit stops the loop rather than continuing to the next pair**, and that
        is the one failure treated differently. `providers/endpoints.py` states the rule
        this follows: re-asking a host that has just told us to stop is how a soft throttle
        becomes the ban one vendor warns about. A 429 has already survived the shared
        transport's retries, so the next request would be the third thing this host has
        refused in a row. The outstanding pairs go to the next source, which is what
        failover is for -- and `ProviderRateLimitedError` is caught first because it is a
        *subclass* of `ProviderUnavailableError`, so ordering the arms the other way round
        would make this branch unreachable.

        A pair outside `SPOT_PAIRS` is skipped without a request: `fetch_prices` filters on
        `pairs` already, and this is the second guard for a caller that does not.
        """
        quotes: list[PriceQuote] = []
        for pair in pairs:
            if pair not in SPOT_PAIRS:
                continue
            try:
                quotes.append(await self._fetch_one(pair))
            except ProviderRateLimitedError:
                # Stop asking this host entirely; see the docstring. Whatever is still
                # outstanding is the next source's.
                break
            except ProviderError:
                # One pair's failure leaves the others. Nothing is logged here, as nowhere
                # in this package logs.
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


# --------------------------------------------------------------------------------------
# Daily candles from Coinbase Exchange, for the days before Kraken's window (spec 038)
# --------------------------------------------------------------------------------------

COINBASE_EXCHANGE_API_URL: Final = "https://api.exchange.coinbase.com"
"""Coinbase Exchange's public REST root, a constant for the reason `COINBASE_API_URL` is one.

**Not the same host as `COINBASE_API_URL`.** The spot price is Coinbase's retail API; candles
are the Exchange API's, documented at docs.cdp.coinbase.com with this server and
`security: []` -- no key. Measured on 2026-10-08.
"""

CANDLES_PATH: Final = "/products/{product}/candles"
"""Confirmed against Coinbase's documentation and the live service on 2026-10-08.

`{product}` comes only from `CANDLE_PRODUCTS`, never from a caller's string."""

EXCHANGE_VENDOR: Final = "Coinbase Exchange"
"""What the upstream is called in a refusal or an exhaustion message: the brand, never a host."""

CANDLE_PRODUCTS: Final[Mapping[PricePair, str]] = {(BTC, USD): "BTC-USD"}
"""Our pair to Coinbase Exchange's product id. BTC/USD only.

USD only for the reason `kraken.BACKFILL_PAIRS` gives, and no KAS because there is none to
ask for: `GET /products/KAS-USD` is a 404 and `GET /products` lists no KAS product, measured
on 2026-10-08. KAS before Kraken's first KAS candle stays a gap (spec 038).
"""

EARLIEST_CANDLE_DAY: Final = date(2015, 7, 20)
"""The first day BTC-USD has a daily candle, measured: 2015-07-01..08-01 starts on the 20th,
and 2014 answers `[]`. Nothing before it is ever asked for."""

DAILY_GRANULARITY_SECONDS: Final = 86_400
"""One candle per day: `86400` is one of the six documented granularities."""

MAX_DAYS_PER_REQUEST: Final = 300
"""The most days one request asks for: a window of `start=D`, `end=D+299 days`.

Documented as "the maximum number of data points for a single request is 300 candles", and
measured as a limit on *intervals* with both ends inclusive: 300 intervals answered 301
candles and 301 intervals were a `400`. A window of 300 days is 299 intervals, so it is
inside both readings and never asks for more than 300 candles.
"""

CANDLE_CLOSE_INDEX: Final = 4
"""Where the close is: `[time, low, high, open, close, volume]`. Not Kraken's order, whose
index 4 is also the close but whose index 1 is the open rather than the low."""

_EPOCH_DAY: Final = date(1970, 1, 1)


def candle_windows(first_day: date, last_day: date) -> tuple[tuple[date, date], ...]:
    """The `(start, end)` days of each request covering `first_day..last_day`, oldest first.

    Each window holds at most `MAX_DAYS_PER_REQUEST` days, both ends inclusive, and the next
    starts the day after the previous one ends, so no day is asked for twice. An empty range
    is no window at all.
    """
    windows: list[tuple[date, date]] = []
    start = first_day
    while start <= last_day:
        end = min(start + timedelta(days=MAX_DAYS_PER_REQUEST - 1), last_day)
        windows.append((start, end))
        start = end + timedelta(days=1)
    return tuple(windows)


def parse_candles(
    body: str | bytes,
    product: str,
    first_day: date,
    last_day: date,
) -> tuple[DailyClose, ...]:
    """Every daily close from `first_day` to `last_day` in one candles response, oldest first.

    **The prices are JSON numbers, not strings**, measured on 2026-10-08 against the
    documentation's general rule; `decode_json` builds each one as a `Decimal` from the digits
    the vendor sent, and a bare JSON integer arrives as an `int`, which `require_price` takes
    exactly. Nothing here goes near a float.

    The candles are keyed by their time and never read by position: they were measured newest
    first, which is undocumented. A candle outside the asked window is dropped rather than
    refused, because the documentation warns that some "may precede your declared `start`".

    The refusals, and why each is one:

    | Body | Why it is a refusal |
    |---|---|
    | not a JSON array | not the documented shape |
    | an entry that is not an array of at least five | not a candle with a close |
    | a time that is not an integer, or not a UTC midnight | not a daily candle's open |
    | a close in the window that is not a positive, finite price | `require_price` decides |
    | two candles for one day in the window | the series cannot be trusted as a whole |

    Raises:
        ProviderResponseError: any of the above.
    """
    document = decode_json(body)
    if not isinstance(document, list):
        message = (
            f"The {EXCHANGE_VENDOR} candles response for {product} is a "
            f"{type(document).__name__} rather than the JSON array this endpoint documents."
        )
        raise ProviderResponseError(message)
    earliest = _epoch_second(first_day)
    latest = _epoch_second(last_day)
    closes: dict[date, Decimal] = {}
    for entry in document:
        opened, close = _candle(entry, product)
        if not earliest <= opened <= latest:
            # Outside the window, which includes a candle for a day still trading: the
            # source never asks for today, so today is always outside it.
            continue
        day = _EPOCH_DAY + timedelta(days=opened // DAILY_GRANULARITY_SECONDS)
        if day in closes:
            message = f"The {EXCHANGE_VENDOR} candles for {product} carry two for one day."
            raise ProviderResponseError(message)
        closes[day] = require_price(close, source=EXCHANGE_VENDOR)
    return tuple(DailyClose(day=day, close=closes[day]) for day in sorted(closes))


def _epoch_second(day: date) -> int:
    """The Unix time of `day`'s UTC midnight, in integer arithmetic."""
    return (day - _EPOCH_DAY).days * DAILY_GRANULARITY_SECONDS


def _candle(entry: object, product: str) -> tuple[int, object]:
    """A candle's open time and its raw close, refusing every candle that is not a daily one.

    The time is compared with the window as an integer before it is ever made a date, so a
    time no `date` can hold is dropped as outside the window rather than raising
    `OverflowError` out of a parser whose contract is `ProviderResponseError`.

    Raises:
        ProviderResponseError: not an array of at least five, or its time is not an integer
            at a UTC midnight. `False` is refused although it equals 0, a UTC midnight.
    """
    if not isinstance(entry, list) or len(entry) <= CANDLE_CLOSE_INDEX:
        message = f"A {EXCHANGE_VENDOR} candle for {product} is not the documented array."
        raise ProviderResponseError(message)
    opened = entry[0]
    if (
        not isinstance(opened, int)
        or isinstance(opened, bool)
        or opened % DAILY_GRANULARITY_SECONDS
    ):
        message = f"A {EXCHANGE_VENDOR} candle for {product} does not open at a UTC midnight."
        raise ProviderResponseError(message)
    return opened, entry[CANDLE_CLOSE_INDEX]


class CoinbaseDailyCloses:
    """Reads BTC/USD daily closes for a range of past days from Coinbase Exchange's candles.

    Satisfies `HistoricalCloseSource` structurally. One endpoint and no fallback, through
    `EndpointSet` for the reason `KrakenPriceSource` gives.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind to the shared client. No key and no configuration.

        `clock` is read once per call, only to know which UTC day is still trading: the
        candle for today moves until midnight, so the last day ever asked for is yesterday.
        """
        self._endpoint = EndpointSet.configured(
            client, ((PRIMARY, COINBASE_EXCHANGE_API_URL),), vendor=EXCHANGE_VENDOR
        )
        self._clock = clock

    @property
    def name(self) -> str:
        """`coinbase`, the string written to `price_history.source`."""
        return COINBASE

    @property
    def pairs(self) -> frozenset[PricePair]:
        """Every pair in `CANDLE_PRODUCTS`: BTC/USD."""
        return frozenset(CANDLE_PRODUCTS)

    @property
    def earliest_day(self) -> date:
        """`EARLIEST_CANDLE_DAY`, 2015-07-20."""
        return EARLIEST_CANDLE_DAY

    async def daily_closes_between(
        self,
        pair: PricePair,
        first_day: date,
        last_day: date,
    ) -> tuple[DailyClose, ...]:
        """The committed closes from `first_day` to `last_day`, oldest first.

        The range is narrowed to `EARLIEST_CANDLE_DAY..yesterday` (UTC) before anything is
        asked: no day before the first candle exists, and today's is still trading. What is
        left is asked for in windows of at most `MAX_DAYS_PER_REQUEST` days, **one after the
        other**, never gathered, for the reason `CoinbasePriceSource.fetch` gives -- and the
        shared per-host floor paces them well under the documented 10 requests a second.
        Both `start` and `end` are always sent, as ISO 8601 UTC midnights: either one alone
        is ignored and the vendor answers the last 350 days instead.

        Every window must answer: a failure on any of them raises, and the backfill stores
        nothing for the range rather than a range with a hole in the middle that nothing
        would ask for again.

        The query carries a product id, a granularity and two dates -- nothing about the
        owner -- and the transport logs only the label.

        Raises:
            ProviderRateLimitedError: a 429 that survived the transport's retries.
            ProviderUnavailableError: the vendor did not answer, or failed with a 5xx.
            ProviderResponseError: the pair is not one this source reads, or an answer
                cannot be trusted.
        """
        product = CANDLE_PRODUCTS.get(pair)
        if product is None:
            message = f"{pair[0]}/{pair[1]} is not a pair the {EXCHANGE_VENDOR} backfill reads."
            raise ProviderResponseError(message)
        yesterday = self._clock().astimezone(UTC).date() - timedelta(days=1)
        closes: list[DailyClose] = []
        windows = candle_windows(max(first_day, EARLIEST_CANDLE_DAY), min(last_day, yesterday))
        for start, end in windows:
            path = (
                f"{CANDLES_PATH.format(product=product)}"
                f"?granularity={DAILY_GRANULARITY_SECONDS}"
                f"&start={_iso_midnight(start)}&end={_iso_midnight(end)}"
            )
            body, _index = await self._endpoint.read(path, ASSET_DAILY_CLOSES)
            closes.extend(parse_candles(body, product, start, end))
        return tuple(closes)


def _iso_midnight(day: date) -> str:
    """`day`'s UTC midnight as ISO 8601 with a `Z`, the form the measurement used."""
    return f"{day.isoformat()}T00:00:00Z"
