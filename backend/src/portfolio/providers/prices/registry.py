"""Which sources exist, in what order, and why the keyed one is sometimes not there at all.

One function. It is a separate module from `base.py` for a dull reason and a good one: the
dull one is that `base` holds `PriceQuote`, which every source imports, so a builder in
`base` would make the package import itself in a circle. The good one is that this is the
same split `providers/registry.py` already has -- the protocol and the vocabulary in one
place, the table of what is actually wired in another -- and a reader looking for "what does
this application call" should find one short file.

Nothing is auto-discovered, for the reason `providers/registry.py` gives at length about
`pkgutil.walk_packages`: a source that was written but never wired should be visible as a
missing line here rather than as a price that quietly never arrives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from portfolio.config import get_settings
from portfolio.providers.prices.coinbase import CoinbasePriceSource
from portfolio.providers.prices.coingecko import CoinGeckoPriceSource
from portfolio.providers.prices.kaspa import KaspaPriceSource
from portfolio.providers.prices.kraken import KrakenPriceSource

if TYPE_CHECKING:
    import httpx

    from portfolio.config import Settings
    from portfolio.providers.prices.base import PriceSource

__all__ = ["price_sources"]


def price_sources(
    client: httpx.AsyncClient,
    *,
    settings: Settings | None = None,
) -> tuple[PriceSource, ...]:
    """Every price source this application can use, in the order they should be tried.

    The order is global and the per-pair order is derived from it by `base.sources_for`,
    filtered by each source's own `pairs`. Reading the two together gives the table in the
    spec, and there is no third place stating it.

    **Kraken first**, because it is the only source that lists all four pairs and the only
    one that answers all four in a single request: an hourly refresh that Kraken answers
    costs one request an hour to one host, which is the whole of the measured budget.

    **Coinbase second**, because it is key-free, answers the two BTC pairs, and costs one
    request per pair -- a price worth paying on the day the primary is down and not worth
    paying every hour.

    **The Kaspa endpoint third**, and it is third rather than second only because Coinbase
    cannot answer its pair at all. Its currency is an assumption; see its module docstring.
    Placing it behind every source that *states* its currency is the mitigation.

    **CoinGecko last, and only when a key is configured.** With no key it is not in this
    tuple, not constructed, and unreachable -- criterion 5's "absent, not skipped".

    The check is `is None` and **deliberately not a truthiness test**, so
    `PORTFOLIO_COINGECKO_API_KEY=` counts as configured. An empty string is a variable
    somebody set and got wrong, and the two readings fail differently: treated as absent,
    the operator gets three sources and no explanation anywhere; treated as present, they
    get a 401 from the vendor, which the shared transport logs at error level naming the
    host and the endpoint label. Neither is lovely and only the second can be diagnosed.
    The cost of the noisy reading is small because this is a last-resort fallback --
    `fetch_prices` stops as soon as every pair is answered, so a healthy refresh never
    reaches it and the 401 appears only on a day somebody is already reading the log.

    A tidy-up to `if resolved.coingecko_api_key:` would reverse that silently, which is why
    `tests/providers/prices/test_registry.py` pins it.

    Args:
        client: the shared `httpx.AsyncClient`, whose transport carries the retry policy and
            the per-host rate limiter. Every source is bound to this one client, for the
            reason `build_http_client` gives -- two clients would each keep their own idea
            of the interval and it would silently become half of what it says.
        settings: the configuration to read. A test passes its own rather than
            monkeypatching the environment.

    Returns:
        The sources, in order: Kraken, Coinbase, Kaspa, and CoinGecko only when keyed.
    """
    resolved = settings if settings is not None else get_settings()
    sources: list[PriceSource] = [
        KrakenPriceSource(client, settings=resolved),
        CoinbasePriceSource(client, settings=resolved),
        KaspaPriceSource(client, settings=resolved),
    ]
    if resolved.coingecko_api_key is not None:
        sources.append(CoinGeckoPriceSource(client, settings=resolved))
    return tuple(sources)
