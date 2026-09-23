"""The seam every price source is on the other side of, and the failover loop over them.

A price source answers one question -- what does this asset cost in this currency -- for
the pairs it says it can answer, and says nothing else. There is no `health()` here and no
address validation: neither has a caller, and `providers/` already carries one unexercised
parser that looks tested.

## Why the pair, and not a global failover chain

`providers/endpoints.py` fails over between interchangeable instances of **one** vendor's
API, where every instance can answer every question. Sources are not interchangeable:
Coinbase does not list KAS at all -- measured, a 404 on both `KAS-USD` and `KAS-EUR` -- and
the Kaspa price endpoint knows one asset and (assumed) one currency. A single ordered list
tried for every pair would spend a request asking a vendor a question it can never answer,
and would report "every source failed" when the truth is that only one was ever eligible.

So each source declares `pairs`, and the order for a pair is the global order filtered by
that declaration. The table in the spec falls out of the code rather than sitting beside it
as a second copy:

| Pair | Order today |
|---|---|
| BTC/USD, BTC/EUR | Kraken, Coinbase, CoinGecko (only when keyed) |
| KAS/USD | Kraken, Kaspa, CoinGecko (only when keyed) |
| KAS/EUR | Kraken, CoinGecko (only when keyed) |
| anything else | nothing, refused without a request |

## `as_of` is not here, and that is the same decision `ProviderHealth` made

A `PriceQuote` carries no timestamp. Measured on 2026-09-23: **none of Kraken's ticker,
Coinbase's spot endpoint or the Kaspa price endpoint returns a quote time.** We know when
we asked and we do not know how old the answer was, so the only honest timestamp is the
caller's own -- and a second one produced in here could disagree with it, which is how a
stale reading gets presented as a fresh one. The refresh service reads its clock once and
stamps every row from that read.

## Money arrives as a `Decimal` because `decode_json` makes it one

Two of the four vendors send a price as a JSON **string** and two send it as a JSON
**number**. `providers/base.decode_json` passes `parse_float=Decimal`, so a number arrives
built from the literal text the vendor sent rather than from the nearest double. Nothing in
this package converts, rounds or compares a price; it is a `Decimal` from the parser to the
column.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Final, Protocol

from portfolio.db.models import PRICE_SCALE
from portfolio.domain.money import MONEY_PRECISION
from portfolio.providers.errors import ProviderError, ProviderResponseError

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "BTC",
    "EUR",
    "KAS",
    "SUPPORTED_PAIRS",
    "USD",
    "PriceFetch",
    "PricePair",
    "PriceQuote",
    "PriceSource",
    "fetch_prices",
    "require_price",
    "sources_for",
]

BTC: Final = "BTC"
KAS: Final = "KAS"
"""The asset symbols this product prices, spelled exactly as `assets.symbol` holds them.

Written down here rather than read from the database because a source has to build a
vendor-specific pair code out of them -- Kraken's `XXBTZUSD` is not derivable from a row --
and a symbol that exists in the table with no source behind it is simply a pair nothing
answers.
"""

USD: Final = "USD"
EUR: Final = "EUR"
"""The two quote currencies, matching the `CHECK` on `prices.quote_currency`.

**Neither is ever derived from the other.** Valuing a EUR portfolio from a USD price and a
cross rate would put a second vendor's error into every number with nothing saying so, so
each currency is fetched on its own and a pair nobody answered stays unanswered.

The constant is duplicated as SQL text in `db/models.py` and in `v0004_prices.py`, which is
the same duplication `_WALLET_CHAIN_KEY_CHECK` carries and is covered the same way: a test
reflects the constraint off a migrated database and compares it. `db` may not import
`providers`, so the direction that would remove the duplication is the one the layering
forbids.
"""

MAX_PRICE_INTEGER_DIGITS: Final = MONEY_PRECISION - PRICE_SCALE
"""How many digits a price may carry before the decimal point: 38 - 12 = 26.

**Derived from the column, not invented here**, and that is what makes it exact. A price
this application cannot store is not a price it should accept, and the two numbers it
depends on already exist -- `MONEY_PRECISION` is the domain's significant-digit budget and
`PRICE_SCALE` is what `prices.amount` rounds to. Writing a plausible-looking bound here
instead would be a third number that drifts from both.

The import that makes this possible is `providers -> db`, which the layering contract
allows: `db` sits below `providers`, for the same reason `NumericText` rounds by
`domain.money`'s rule rather than carrying a second copy of it.

Twenty-six digits in front of the point is roughly 10**26 units of fiat for one coin. No
asset will approach it, which is the point: the bound exists to catch a vendor sending
something that is not a price at all, not to express a view about the market.
"""

type PricePair = tuple[str, str]
"""`(asset_symbol, quote_currency)`. A tuple rather than a dataclass, so it is a dict key
and a set member without anything being written to make it one."""

SUPPORTED_PAIRS: Final[frozenset[PricePair]] = frozenset(
    {(BTC, USD), (BTC, EUR), (KAS, USD), (KAS, EUR)}
)
"""Every pair this product knows how to price. Anything else is refused without a request.

Four today. A pair outside this set is not a failure to reach a vendor and must not be
reported as one: nothing was asked, because nothing could have answered.
"""


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """One vendor's answer: what an asset costs in a currency, and who said so.

    `source` is the name of the source that actually answered, not the one that was asked
    first. It is carried on the quote rather than inferred by the caller from its position
    in a list, because the whole point of failover is that those two differ, and a stored
    price whose `source` column says "kraken" when Coinbase answered is a record that
    cannot be audited.

    No timestamp: see the module docstring. No `stale` either -- staleness is a question
    about a stored row and a clock, and neither exists here.
    """

    asset_symbol: str
    quote_currency: str
    amount: Decimal
    source: str

    @property
    def pair(self) -> PricePair:
        """The pair this quote answers, derived so it cannot disagree with the fields."""
        return (self.asset_symbol, self.quote_currency)


class PriceSource(Protocol):
    """What a price source must offer. Structural, and checked by `mypy` rather than at run time.

    Not `@runtime_checkable`, for the reason `ChainProvider` is not: `isinstance` against a
    runtime-checkable protocol compares attribute *names* and nothing else, so a class whose
    `fetch` takes the wrong arguments or is not a coroutine function passes it happily. The
    real check is `mypy --strict` deciding assignability.
    """

    @property
    def name(self) -> str:
        """What this source is called in a `prices.source` column and in a log.

        A vendor's software or brand name, lower case, never a host: it is written to a
        column an operations view renders, and a hostname there names a deployment.
        """

    @property
    def pairs(self) -> frozenset[PricePair]:
        """Every pair this source can answer. Constant for the life of the instance.

        A declaration rather than a discovery, so that a pair a vendor does not list costs
        no request at all. Coinbase's is the one that matters: it omits KAS, measured.
        """

    async def fetch(self, pairs: Sequence[PricePair]) -> Sequence[PriceQuote]:
        """Read the pairs this source was asked for, in as few calls as it can manage.

        The caller has already filtered `pairs` down to this source's own `pairs`, so an
        implementation never has to decide what to do with one it cannot answer.

        **A partial answer is allowed and is not an error.** Returning quotes for three of
        four pairs leaves the fourth to the next source, which is the failure mode failover
        exists for. What is *not* allowed is answering about a pair nobody asked for: that
        is a correlation bug -- a cached response for another request, a mis-keyed lookup --
        and a parser that dropped it silently would hide it behind a price that still looks
        plausible.

        **That last rule is enforced by `fetch_prices`, not merely stated here.** A
        response carrying a pair nobody asked for is discarded whole and the source is
        passed over, because the quotes we did ask for came out of the same document. Each
        parser checks its own correlation as well, and the duplication is deliberate: four
        copies of a rule is four chances to drop it, and the source nobody has written yet
        is the one that would.

        Raises:
            ProviderUnavailableError: the vendor could not be reached, or did not answer.
            ProviderRateLimitedError: it refused because we asked too often.
            ProviderResponseError: it answered with something that cannot be trusted, or it
                refused the request.
        """


@dataclass(frozen=True, slots=True)
class PriceFetch:
    """What one pass over the sources produced: the quotes, and what nothing answered.

    Two tuples rather than a mapping plus a derived list, because the pairs nothing
    answered are the half a caller is most likely to forget -- and a caller that has to
    compute them from a mapping is a caller that can compute them wrongly and report a
    total that silently omits a holding.

    `unanswered` is sorted, so a report built from it is stable between runs and a test can
    assert on it without sorting first.
    """

    quotes: tuple[PriceQuote, ...]
    unanswered: tuple[PricePair, ...]


def sources_for(
    asset_symbol: str,
    quote_currency: str,
    sources: Sequence[PriceSource],
) -> tuple[PriceSource, ...]:
    """The sources that can answer this pair, in the order they should be tried.

    Derived: the order `sources` arrives in, filtered by each source's own `pairs`. There
    is deliberately no hand-written per-pair table. One would be a second statement of
    facts the sources already declare -- "Coinbase does not list KAS" lives on the Coinbase
    source, where the measurement that established it can be cited -- and two statements of
    one fact is how they come to disagree.

    **Returns an empty tuple for an unsupported pair, and makes no request doing it.** Not
    an exception: a caller asking about a pair this product does not price is asking an
    ordinary question with an ordinary answer, and `refresh_prices` has to distinguish that
    from "every source failed" in its report. An empty tuple is also what a supported pair
    returns when the caller passed no sources at all, which is a different report line for
    the same reason.

    Args:
        asset_symbol: the symbol as `assets.symbol` spells it.
        quote_currency: `USD` or `EUR`; anything else is unsupported by construction.
        sources: the built sources, in the order they should be tried.

    Returns:
        The eligible sources, in order. Empty when the pair is unsupported, or when no
        source among those given declares it.
    """
    pair: PricePair = (asset_symbol, quote_currency)
    if pair not in SUPPORTED_PAIRS:
        return ()
    return tuple(source for source in sources if pair in source.pairs)


async def fetch_prices(
    pairs: Sequence[PricePair],
    sources: Sequence[PriceSource],
) -> PriceFetch:
    """Ask each source in turn for whatever is still outstanding, and report what is not.

    The failover loop, and the rule it implements is one sentence: **every failure to
    answer moves to the next source, and a source that answers some of what it was asked
    leaves the rest to the one behind it.** That is deliberately more forgiving than
    `EndpointSet`'s loop, and for a reason: there, every endpoint runs the same software
    against the same chain, so a partial answer is a correlation bug. Here the sources are
    four unrelated vendors and a partial answer is the normal case -- Kraken lists all four
    pairs, Coinbase lists two, the Kaspa endpoint lists one.

    **Every `ProviderError` is caught and moved past, including a refusal.** A 401 from a
    keyed source means that one key is wrong, not that the price is unknowable; a 404 from
    Coinbase for a pair it turns out not to list is exactly what the next source is for. The
    call that is *not* caught is a `ValueError` or anything else out of a source's own
    logic, because that is a bug here rather than a vendor being a vendor, and swallowing it
    would turn a broken parser into a permanently missing price with nothing in any log.

    **The last failure is not raised.** Unlike `EndpointSet.read`, which has one answer to
    give and must raise when it cannot give it, this returns what it has: a portfolio with
    three prices and one missing is a real, reportable state, and an exception here would
    throw away the three. The missing one travels in `unanswered`, and `refresh_prices`
    turns it into a reason rather than a zero.

    A source is not asked at all when nothing outstanding is in its `pairs`, which is what
    keeps an unsupported pair from costing a request, and the loop stops as soon as
    everything is answered -- so a healthy Kraken means Coinbase is never called and the
    measured budget is one request per refresh.

    Args:
        pairs: the pairs to fetch. Duplicates collapse; unsupported pairs are not filtered
            here, because `sources_for` already returns nothing for them and a pair no
            source declares simply comes back unanswered.
        sources: the built sources, in the order they should be tried.

    Returns:
        The quotes obtained, in the order the sources produced them, and the pairs nothing
        answered, sorted.
    """
    outstanding = list(dict.fromkeys(pairs))
    answered: dict[PricePair, PriceQuote] = {}

    for source in sources:
        wanted = [pair for pair in outstanding if pair in source.pairs]
        if not wanted:
            continue
        try:
            quotes = await source.fetch(wanted)
        except ProviderError:
            # Deliberately not logged and not re-raised. Nothing here has a logger -- the
            # shared transport is the only thing in this package that logs, and its
            # contract is enforced rather than remembered -- and the fact that a source
            # failed reaches an operator as a `source` column naming whoever did answer,
            # or as a reason when nobody did.
            continue
        if not _answers_only_what_was_asked(quotes, wanted):
            # **The whole response is discarded, not just the extra quote**, and the
            # outstanding pairs go to the next source as though this one had not answered.
            # A response that does not correspond to its request has proved that its
            # correlation is broken; the quotes for pairs we *did* ask for come out of the
            # same document and are no more trustworthy than the one that gave it away.
            # Keeping them and dropping the extra would hide a paging mistake, a cached
            # answer for somebody else's request or a mis-keyed lookup behind prices that
            # still look plausible -- which is the argument `align_balances` makes about an
            # address nobody requested, applied to the other end of the same idea.
            continue
        for quote in quotes:
            answered[quote.pair] = quote
        outstanding = [pair for pair in outstanding if pair not in answered]
        if not outstanding:
            break

    return PriceFetch(
        quotes=tuple(answered.values()),
        unanswered=tuple(sorted(outstanding)),
    )


def _answers_only_what_was_asked(
    quotes: Sequence[PriceQuote],
    wanted: Sequence[PricePair],
) -> bool:
    """Whether every quote is about a pair this source was actually asked for.

    **The rule `PriceSource.fetch` states, enforced in the loop rather than trusted to four
    parsers.** All four do check their own documents today -- `parse_ticker` against the
    codes it requested, `parse_spot` against the echoed `base` and `currency`,
    `parse_simple_price` against the coin ids it asked for, and `parse_price` returns a
    constant pair -- so nothing reaches this check in the shipped configuration. It is here
    for the reason `align_balances` refuses an unrequested address in the shared alignment
    step instead of leaving it to each provider: a rule enforced in four places is a rule
    one of them can drop, and the fifth source nobody has written yet is the one that
    would.

    Answering about *fewer* pairs than were asked is fine and is the normal case: a partial
    answer leaves the rest to the next source, which is what failover is for. Only an
    answer about something nobody asked for is a correlation failure.

    Returns `False` rather than raising. The caller is a loop over sources whose one rule is
    that a source which does not answer moves us to the next one, and an exception would
    have to be caught two lines later to mean the same thing. It also must not abort the
    whole fetch: three pairs already obtained from an earlier source are real and a fourth
    vendor misbehaving is no reason to throw them away.
    """
    asked = set(wanted)
    return all(quote.pair in asked for quote in quotes)


def require_price(value: object, *, source: str) -> Decimal:
    """Turn whatever a vendor put in its price field into a `Decimal`, or refuse it.

    **The single boundary every price parser goes through**, so that four vendors cannot
    have four opinions about what counts as a price. It accepts exactly two shapes, which
    are the two the four vendors actually send, measured on 2026-09-23:

    | Shape | Who sends it | What arrives here |
    |---|---|---|
    | JSON string | Kraken (`c[0]`), Coinbase (`data.amount`) | `str` |
    | JSON number | the Kaspa price endpoint, CoinGecko | `Decimal`, via `decode_json` |

    A JSON number is already a `Decimal` by the time it reaches this function, because
    `providers.base.decode_json` passes `parse_float=Decimal` -- built from the literal text
    the vendor sent, not from the nearest double. **That is the whole of rule 2 at this
    boundary, and it happens before any of this code runs**, which is why the hook is in the
    shared decoder rather than here: a parser cannot repair a value that a parser already
    damaged.

    An `int` is accepted with them, exactly as `NumericText` accepts one: a whole number has
    nothing after the point to lose. A `bool` is refused although it is an `int`, for the
    reason `domain/money.py` gives -- `True` would become a price of 1.

    Refused: a zero or negative price, which is not a price and would value a holding at
    nothing; a price with more integer digits than `MAX_PRICE_INTEGER_DIGITS`, which this
    application cannot store and which must fail as a vendor error rather than inside a
    database flush; a non-finite `Decimal`; and anything else at all, including a `float`, which
    cannot arrive through `decode_json` but could from a parser that built one some other
    way.

    **The non-finite arm is reached through a JSON *string*, not through a JSON number**,
    and that is worth knowing before somebody deletes it as unreachable. `decode_json`
    refuses the bare tokens `NaN` and `Infinity` at the decoder, so `{"price": NaN}` never
    gets this far. But `{"price": "NaN"}` is an ordinary JSON string, which is the shape
    Kraken and Coinbase use for every price they send, and `Decimal("NaN")` constructs
    perfectly happily. A NaN in a money column compares false against itself forever.

    The message names the source and the type and **never the value**. A price is public
    market data rather than the owner's holdings, so the value is not a disclosure -- but a
    message that quotes a body is a habit, and the habit is what leaks an address at the
    next boundary.

    Raises:
        ProviderResponseError: the value is not a price this application can store.
    """
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        message = (
            f"The {source} price is a {type(value).__name__} rather than a number or a "
            "string carrying one."
        )
        raise ProviderResponseError(message)
    try:
        amount = Decimal(value)
    except InvalidOperation:
        # A string the vendor rendered in a shape `Decimal` cannot read -- `"1,234.5"`, an
        # empty string, a localised separator. `Decimal(str)` raises `InvalidOperation`
        # rather than `ValueError`, which is the arm a reader would not have written down.
        message = f"The {source} price is a string that is not a number."
        raise ProviderResponseError(message) from None
    if not amount.is_finite():
        # `json.loads` accepts `NaN`, `Infinity` and `-Infinity` out of the box, and
        # `Decimal("NaN")` constructs happily. Neither is a price, and a NaN stored in a
        # money column compares false against itself forever.
        message = f"The {source} price is not a finite number."
        raise ProviderResponseError(message)
    if amount <= 0:
        message = f"The {source} price is not greater than zero, so it is not a price."
        raise ProviderResponseError(message)
    if amount.adjusted() >= MAX_PRICE_INTEGER_DIGITS:
        # **Refused here so that it is a vendor failure rather than a database failure.**
        # Kraken and Coinbase send prices as strings, so `"1e300"` is a well-formed
        # response body as far as every layer above this one is concerned. Without this
        # check the quote is built, survives the service, and dies inside `flush()` --
        # where `NumericText` raises a `ValueError` that SQLAlchemy wraps in a
        # `StatementError`. Three things go wrong at that point and none of them is the
        # bad price: a `sqlalchemy` exception escapes a service into a caller that may not
        # import it, the operator gets a traceback from a CLI command, and **every pair
        # that had already been fetched in the same refresh is discarded**, which is the
        # outcome `fetch_prices` is written to make impossible.
        #
        # As a `ProviderResponseError` it is instead one more thing a vendor can get
        # wrong: the failover loop passes the source over, the other pairs are kept, and
        # this one is answered by the next source or becomes a reason. No new vocabulary
        # and no new report line -- the path already exists.
        #
        # `adjusted()` is the exponent of the leading digit, so it is `d - 1` for a value
        # with `d` integer digits; `>=` is therefore the bound on `d > MAX`. Nothing is
        # rejected for being too *small* here: a price finer than the scale is destroyed
        # rather than merely imprecise, and `NumericText` refuses it for every money
        # column rather than this function refusing it for one.
        message = (
            f"The {source} price has more than {MAX_PRICE_INTEGER_DIGITS} digits before "
            "the decimal point, which is more than this application can represent."
        )
        raise ProviderResponseError(message)
    return amount
