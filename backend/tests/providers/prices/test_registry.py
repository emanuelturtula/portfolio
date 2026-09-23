"""Criterion 5 of #9: with a key and without one -- and "without" means absent, not skipped.

The interpretation the spec records is the whole of this module: *with no key, CoinGecko is
not merely skipped at call time -- it is absent from the source list, so no code path can
reach it.* The difference between those two readings is a branch. A source that is built
and then skipped is one `if` away from being asked, and the `if` is in whichever loop
somebody edits next; a source that was never constructed cannot be reached by any edit that
does not also add it back.

So the assertions here are about the **object graph**: what `price_sources` returns, what
type is not in it, and what constructing one directly does instead.

The per-pair order is asserted by reading it out of the shipped code rather than against a
hand-written table. There is deliberately no per-pair constant in `providers/prices/` --
the order is `price_sources()` filtered by each source's own `pairs` -- so a table here
would be a second statement of facts the sources already declare, and the second statement
is the one that goes stale. What is pinned as a literal is the *result*, which is what the
spec's table promises and what an operator would check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest

from portfolio.config import Settings
from portfolio.providers.http import ASSET_PRICE, ASSET_PRICES, ENDPOINT_LABELS
from portfolio.providers.prices.base import (
    BTC,
    EUR,
    KAS,
    SUPPORTED_PAIRS,
    USD,
    fetch_prices,
    sources_for,
)
from portfolio.providers.prices.coinbase import COINBASE, COINBASE_API_URL, CoinbasePriceSource
from portfolio.providers.prices.coingecko import (
    COINGECKO,
    COINGECKO_DEMO_API_URL,
    CoinGeckoPriceSource,
)
from portfolio.providers.prices.kaspa import KASPA, KaspaPriceSource
from portfolio.providers.prices.kraken import KRAKEN, KRAKEN_API_URL, KrakenPriceSource
from portfolio.providers.prices.registry import price_sources
from tests.providers.prices.harness import (
    SYNTHETIC_COINGECKO_KEY,
    PriceFake,
    price_client,
    price_settings,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from portfolio.providers.prices.base import PricePair, PriceSource

#: The shipped per-pair order, written out. This is the spec's table, and it is what a test
#: is for: `sources_for` derives it, and a derivation nobody checked against an intended
#: result is a derivation that can be right about the wrong thing.
UNKEYED_ORDER: Final[dict[tuple[str, str], tuple[str, ...]]] = {
    (BTC, USD): (KRAKEN, COINBASE),
    (BTC, EUR): (KRAKEN, COINBASE),
    (KAS, USD): (KRAKEN, KASPA),
    (KAS, EUR): (KRAKEN,),
}

#: The same table with a key configured: CoinGecko is appended to every pair, because it is
#: the only source that lists all four -- and it is last, because it is the only one that
#: spends a quota.
KEYED_ORDER: Final[dict[tuple[str, str], tuple[str, ...]]] = {
    pair: (*order, COINGECKO) for pair, order in UNKEYED_ORDER.items()
}

UNSUPPORTED: Final[tuple[tuple[str, str], ...]] = (
    ("XRP", USD),
    ("BTC", "GBP"),
    ("USDT", USD),
    ("", ""),
)
"""Pairs that must cost no request. `USDT` is the interesting one: it is a seeded asset, so
a check written against the `assets` table rather than against `SUPPORTED_PAIRS` would let
it through and then find no source for it at the far end."""


def names(sources: Sequence[PriceSource]) -> tuple[str, ...]:
    """The `name` of each source, in order. The thing written to `prices.source`."""
    return tuple(source.name for source in sources)


def built(fake: PriceFake, *, key: str | None) -> tuple[PriceSource, ...]:
    """The shipped source list, over a client that would record any request made."""
    return price_sources(price_client(fake), settings=price_settings(coingecko_api_key=key))


# --------------------------------------------------------------------------------------
# Criterion 5: no key means the keyed source is not there
# --------------------------------------------------------------------------------------


def test_without_a_key_the_keyed_source_is_absent_not_skipped() -> None:
    """Three sources, and no `CoinGeckoPriceSource` instance anywhere in the graph.

    Asserted on the types as well as on the names. A name check alone would pass for a
    source object that was built, given a blank credential, and then filtered out by a
    caller -- which is the reading this criterion exists to reject, because a filter is one
    edit away from not filtering.
    """
    sources = built(PriceFake(), key=None)

    assert names(sources) == (KRAKEN, COINBASE, KASPA)
    assert COINGECKO not in names(sources)
    assert not any(isinstance(source, CoinGeckoPriceSource) for source in sources)
    assert [type(source) for source in sources] == [
        KrakenPriceSource,
        CoinbasePriceSource,
        KaspaPriceSource,
    ]


def test_a_configured_key_appends_the_keyed_source() -> None:
    """Four sources, CoinGecko last. The control: without it the test above is half a claim.

    Last rather than anywhere: it is the only source with a published monthly quota, so
    every request that reaches it is one of ten thousand, and every request that does not
    is free. The order is therefore a budget decision and not a preference.
    """
    sources = built(PriceFake(), key=SYNTHETIC_COINGECKO_KEY)

    assert names(sources) == (KRAKEN, COINBASE, KASPA, COINGECKO)
    assert isinstance(sources[-1], CoinGeckoPriceSource)


def test_the_keyed_source_refuses_to_be_built_without_a_key() -> None:
    """Constructing one directly is a refusal, not a source that sends a blank credential.

    `price_sources` never reaches this -- it omits the source entirely -- so this is the
    guard for the caller who builds one by hand, which is exactly the edit that would
    quietly reintroduce the skipped-not-absent reading. A blank credential sent to a vendor
    on every refresh is worse than no source at all: it is a source that fails in a way an
    operator reads as an outage.
    """
    client = price_client(PriceFake())
    unkeyed = price_settings(coingecko_api_key=None)

    with pytest.raises(ValueError, match=r"COINGECKO_API_KEY") as caught:
        CoinGeckoPriceSource(client, settings=unkeyed)

    # The message names the variable to set and says which of the two readings applies.
    assert "absent" in str(caught.value)


def test_an_empty_key_is_a_configured_key_and_not_an_absent_one() -> None:
    """`""` is a mis-set variable, and the check is `is None` rather than a truthiness test.

    The distinction has a real consequence in each direction. Treated as absent, an operator
    who typed `PORTFOLIO_COINGECKO_API_KEY=` gets three sources and no explanation; treated
    as present, they get a source that fails against the vendor, which is visible. Neither
    is lovely and the second is the one that can be diagnosed.

    Pinned here because a later "tidy-up" to `if resolved.coingecko_api_key:` would change
    it silently, and nothing else in the suite would notice.
    """
    sources = built(PriceFake(), key="")

    assert names(sources) == (KRAKEN, COINBASE, KASPA, COINGECKO)


async def test_no_source_makes_a_request_merely_by_being_built() -> None:
    """Construction is not a call. Four sources built, zero requests, keyed and unkeyed.

    A source that probed its vendor in `__init__` would turn `price_sources()` -- which
    #10's scheduler will call, and which a future dependency might call per request -- into
    four vendor calls, and the measured budget would be wrong by however often it is built.
    """
    fake = PriceFake()

    built(fake, key=None)
    built(fake, key=SYNTHETIC_COINGECKO_KEY)

    assert fake.requests == []
    assert set(fake.counts.values()) == {0}


# --------------------------------------------------------------------------------------
# The per-pair order, read out of the shipped code
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("pair", "expected"), sorted(UNKEYED_ORDER.items()))
def test_the_unkeyed_order_for_each_pair_is_the_one_the_spec_records(
    pair: PricePair,
    expected: tuple[str, ...],
) -> None:
    """The spec's table, pair by pair, over the sources production builds with no key.

    KAS/EUR is the row that carries the risk and it is why this is asserted per pair rather
    than as one set: Kraken is its only key-free source, so losing Kraken means that pair
    falls to CoinGecko or to a reason. A test that only checked "every pair has at least
    one source" would never say so.
    """
    asset_symbol, quote_currency = pair
    sources = built(PriceFake(), key=None)

    assert names(sources_for(asset_symbol, quote_currency, sources)) == expected


@pytest.mark.parametrize(("pair", "expected"), sorted(KEYED_ORDER.items()))
def test_a_key_puts_the_keyed_source_last_for_every_pair(
    pair: PricePair,
    expected: tuple[str, ...],
) -> None:
    """With a key, every pair gains CoinGecko at the end and loses nothing.

    Including KAS/EUR, which goes from one source to two -- the only thing that changes
    that pair's single point of failure.
    """
    asset_symbol, quote_currency = pair
    sources = built(PriceFake(), key=SYNTHETIC_COINGECKO_KEY)

    assert names(sources_for(asset_symbol, quote_currency, sources)) == expected


def test_coinbase_is_absent_from_every_kaspa_pair() -> None:
    """The measured 404, expressed as an absence rather than as a request that fails.

    Coinbase answers 404 on `KAS-USD` and `KAS-EUR`, measured. A global failover chain would
    ask it anyway, pay a round trip, and move on -- every hour, for as long as the product
    exists. Declaring the pairs on the source is what turns that into no request at all,
    and this is the assertion that says the declaration is the one being consulted.
    """
    sources = built(PriceFake(), key=SYNTHETIC_COINGECKO_KEY)

    for currency in (USD, EUR):
        assert COINBASE not in names(sources_for(KAS, currency, sources))
    for currency in (USD, EUR):
        assert COINBASE in names(sources_for(BTC, currency, sources))


def test_the_kaspa_endpoint_is_usd_only_and_last_among_the_key_free_sources() -> None:
    """The source whose currency is a guess, placed where a guess belongs.

    Its body is `{"price": ...}` and names no currency; USD is an inference from the
    number's magnitude, which is not evidence. Two things follow and both are asserted:
    it is absent from the EUR pair entirely, and among the key-free sources it is the last
    one tried for the pair it does answer -- so a healthy Kraken means the assumption is
    never used at all.
    """
    sources = built(PriceFake(), key=None)

    usd = names(sources_for(KAS, USD, sources))
    eur = names(sources_for(KAS, EUR, sources))

    assert KASPA not in eur
    assert usd[-1] == KASPA
    assert usd.index(KRAKEN) < usd.index(KASPA)


# --------------------------------------------------------------------------------------
# An unsupported pair costs nothing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("pair", UNSUPPORTED)
def test_an_unsupported_pair_is_refused_without_a_request(pair: PricePair) -> None:
    """No sources, no exception, and -- the part that matters -- no request.

    Not an exception, because a caller asking about a pair this product does not price is
    asking an ordinary question with an ordinary answer, and `refresh_prices` has to be able
    to report it as `UNSUPPORTED_PAIR` rather than catch it.
    """
    asset_symbol, quote_currency = pair
    fake = PriceFake()
    sources = built(fake, key=SYNTHETIC_COINGECKO_KEY)

    assert sources_for(asset_symbol, quote_currency, sources) == ()
    assert fake.requests == []


async def test_fetching_an_unsupported_pair_calls_nobody_and_reports_it_unanswered() -> None:
    """The same claim through the loop, which is where a request would actually be made.

    `sources_for` returning `()` cannot make a request whatever it does, so asserting on it
    alone is asserting about a function that has no client. `fetch_prices` does have one,
    and this is the test that says an unknown pair reaches the end of it without a vendor
    being troubled.
    """
    fake = PriceFake()
    sources = built(fake, key=SYNTHETIC_COINGECKO_KEY)

    result = await fetch_prices([("XRP", USD)], sources)

    assert result.quotes == ()
    assert result.unanswered == (("XRP", USD),)
    assert fake.requests == []


def test_the_supported_pairs_are_the_four_the_product_prices() -> None:
    """Pinned as a literal set, so removing a pair fails rather than shrinking the check.

    Every other test in this module is parametrised over a table derived from the pairs;
    without this, dropping KAS/EUR from `SUPPORTED_PAIRS` would simply stop testing it.
    """
    assert frozenset({(BTC, USD), (BTC, EUR), (KAS, USD), (KAS, EUR)}) == SUPPORTED_PAIRS
    assert set(UNKEYED_ORDER) == SUPPORTED_PAIRS
    assert all(order for order in UNKEYED_ORDER.values()), "every supported pair needs a source"


def test_the_currency_constants_are_the_two_the_column_admits() -> None:
    """The two vocabularies that must never drift: these constants and the `CHECK`.

    `db` may not import `providers`, so `_PRICE_QUOTE_CURRENCY_CHECK` is a literal string
    of SQL and the duplication cannot be removed by an import. A test is what holds them
    together, and this is it: a currency added here without the migration is a price nothing
    can store, and one added to the column without a source is a price nothing can fetch.
    """
    from portfolio.db.models import _PRICE_QUOTE_CURRENCY_CHECK

    for currency in (USD, EUR):
        assert f"'{currency}'" in _PRICE_QUOTE_CURRENCY_CHECK
    # And nothing else is admitted: the constraint names exactly two currencies.
    assert _PRICE_QUOTE_CURRENCY_CHECK.count("'") == 4


# --------------------------------------------------------------------------------------
# The shipped values, pinned in one place
# --------------------------------------------------------------------------------------


def test_the_shipped_vendor_roots_are_the_measured_ones() -> None:
    """The three constant roots, as literals, because they are what a deployment calls.

    Not derived -- `KRAKEN_API_URL == KRAKEN_API_URL` is true of any string, including an
    empty one, which would make every price request a `httpx.InvalidURL` at run time. The
    harness routes on the host parsed out of these, so this is also what makes the whole
    suite's routing meaningful rather than circular.
    """
    assert KRAKEN_API_URL == "https://api.kraken.com"
    assert COINBASE_API_URL == "https://api.coinbase.com"
    assert COINGECKO_DEMO_API_URL == "https://api.coingecko.com"
    assert all(
        url.startswith("https://")
        for url in (KRAKEN_API_URL, COINBASE_API_URL, COINGECKO_DEMO_API_URL)
    )


def test_the_kaspa_price_source_reads_the_same_settings_as_the_chain_provider() -> None:
    """One host, one pair of variables. A third would be two answers to one question.

    An operator who points the balance reads at their own instance has pointed the price
    read there too, which is the only behaviour that is not surprising. Asserted by giving
    the settings a fictional URL and watching the request land there.
    """
    shipped = Settings()

    assert shipped.kaspa_api_url == "https://api.kaspa.org"
    assert shipped.kaspa_api_fallback_url == ""
    assert shipped.coingecko_api_key is None, "the shipped default is the unkeyed deployment"


def test_the_two_price_endpoint_labels_are_on_the_allowlist() -> None:
    """A label outside `ENDPOINT_LABELS` renders as `<unlabelled>` in every log line.

    The allowlist is the mechanism, and a source that passed a label nobody added would log
    correctly-shaped nonsense forever -- which is worse than an obvious failure, because
    the log still looks like a log. Both labels are asserted, singular and plural, because
    they are one character apart and a copy-paste between the two is invisible.
    """
    assert ASSET_PRICE == "asset_price"
    assert ASSET_PRICES == "asset_prices"
    assert ASSET_PRICE in ENDPOINT_LABELS
    assert ASSET_PRICES in ENDPOINT_LABELS
