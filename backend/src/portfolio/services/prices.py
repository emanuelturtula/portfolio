"""Reading prices back, and valuing holdings with them. **This module reaches no vendor.**

It imports `repositories` and nothing from `providers`, and that absence is the point
rather than an accident of what it happened to need. `backend/.importlinter`'s
`prices-are-never-fetched-in-a-request` contract has no `allow_indirect_imports`, so the
chain `api.routers -> service -> providers.prices` is a violation whether the middle step is
direct or not. A valuation module that imported a price source would make every future
valuation endpoint a contract failure, and a contract that fails on the legitimate change is
a contract somebody weakens. Fetching lives in `services/price_refresh.py`, which is the one
module in `services/` allowed to know a vendor exists.

## A missing price is a reason, and never a zero

This is the most important line in the issue and the design follows from it: *a portfolio
silently showing 0 is worse than one showing an error, because it is believed.* A zero is a
number, it renders, it sums, and nothing downstream can tell it from a holding that really
is worthless.

So `lookup_price` returns either a `Price` or a `PriceUnavailable`, never a `Price` with a
zero in it, and `value_portfolio` returns a total **plus** the holdings it could not price
**plus** a `complete` flag. A portfolio with one unpriced asset does not come back with a
smaller number; it comes back saying the number is short and naming what is missing.
Anything that renders a total has to decide what to do with that, which is the intent.

## Staleness is computed here, from an injected clock, and never stored

`stale` is `now - as_of > STALE_AFTER`, evaluated at the moment the price is read. A stored
`is_stale` column would be wrong one second after it was written and would need a background
job whose only purpose was to keep a derived field true.

The clock is injected, which is what makes "the same row is fresh, then stale" a test that
can be written at all -- and is the same reason `WalletService` takes one.

## Which reasons this module can produce, and which it cannot

Only `NEVER_FETCHED`. The table is all this module reads, and "there is no row for that
pair" is the only thing a table knows. `UNSUPPORTED_PAIR`, `NO_SOURCE_CONFIGURED` and
`EVERY_SOURCE_FAILED` are facts about a fetch, and they are produced by
`services/price_refresh.py` and reported per pair there. Teaching `lookup_price` to
distinguish them would mean importing the source registry into the read path, which is
precisely the import the contract exists to forbid -- so the enum is shared and the
producers are not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from portfolio.repositories.assets import AssetRepository
from portfolio.repositories.prices import PriceRepository

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "STALE_AFTER",
    "Holding",
    "PortfolioValue",
    "Price",
    "PriceLookup",
    "PriceService",
    "PriceUnavailable",
    "UnpricedHolding",
    "ValuedHolding",
    "build_price_service",
    "utc_now",
]

STALE_AFTER: Final = timedelta(hours=1)
"""How old an observation may be before a price is flagged stale.

One hour, matching `PORTFOLIO_PRICE_REFRESH_INTERVAL_MINUTES`, whose default is sixty: a
price that has missed exactly one refresh is the first one worth flagging, and anything
shorter would mark every price stale in the minutes before the next run.

**The two numbers are a pair**, and `config.py` says so beside the setting. Lengthening the
interval without lengthening this marks every price stale most of the time; shortening this
without shortening the interval does the same. Neither is wrong on its own, which is exactly
why the relationship is written down in both places.

**A known, accepted property of the pair: every price reads stale for a few seconds each
hour.** The interval equals this threshold, and `as_of` is stamped at the *start* of a
refresh, so by the time the next refresh has fetched and committed, the previous `as_of` is
an hour and a few seconds old. The window is bounded by how long one refresh takes -- seconds,
paced by the per-host limiter -- and it errs towards "stale", which is the direction #9's
`as_of` argument already chose: a price may be flagged a little early, never a little late.
Closing it would mean a threshold longer than the interval, which would let a price that
missed a refresh go unflagged for however long the difference is. Left as it is, deliberately.

**The threshold is not an expiry.** A stale price is still returned, with its age visible,
because the last known price is better information than no price at all -- the same argument
`ProviderUnavailableError` makes about keeping a previous balance. Refusing to return it
would turn a vendor's bad afternoon into a portfolio that shows nothing.
"""


def utc_now() -> datetime:
    """The clock, in one place, so a test can replace it with a value it chose."""
    return datetime.now(UTC)


class PriceUnavailable(StrEnum):
    """Why there is no price, as a value a caller can branch on and render.

    A `StrEnum` so that the member is its own wire form and its own log field with nothing
    to convert, and so that a reason reaching a template renders as the string rather than
    as `PriceUnavailable.NEVER_FETCHED`.

    Four members, and they are not interchangeable -- each points at a different thing to
    go and look at:

    * `NEVER_FETCHED`: the pair is one we price and no refresh has stored it yet. Look at
      whether the refresh has run.
    * `EVERY_SOURCE_FAILED`: every eligible source was asked and none answered. Look at the
      vendors, or at the network.
    * `UNSUPPORTED_PAIR`: this product does not price that pair at all. Nothing was asked,
      because nothing could have answered. Look at the request, not at the system.
    * `NO_SOURCE_CONFIGURED`: the pair is supported but no source was available to ask.
      Look at the configuration.

    Collapsing them into one "unavailable" would be the same mistake `providers/errors.py`
    exists to prevent one layer down: a caller that cannot tell a broken vendor from a
    question nobody could answer has nothing to act on.
    """

    NEVER_FETCHED = "never_fetched"
    EVERY_SOURCE_FAILED = "every_source_failed"
    UNSUPPORTED_PAIR = "unsupported_pair"
    NO_SOURCE_CONFIGURED = "no_source_configured"


@dataclass(frozen=True, slots=True)
class Price:
    """A price as everything above this layer sees it, with its age already decided.

    A frozen snapshot rather than the ORM row, for the reason `WalletView` is one: the row
    is attached to a session the caller closes, and `asset_id` is an implementation detail
    of the schema that nothing above this layer should have to translate.

    `stale` is a field on the snapshot rather than a method, and that is consistent with
    "never stored": it is computed when the snapshot is built, from the clock read that
    built it, and the snapshot is immutable. What is forbidden is a *column*.

    `as_of` is our clock, copied from the row. No vendor supplies a quote time -- measured
    on all three key-free sources -- so it says when we asked rather than how old the answer
    was, and it is specifically the instant the *refresh* that wrote the row began. It
    therefore errs a few seconds early, never late, which is the direction that cannot make
    a stale price read as a fresh one. `db.models.AssetPrice` carries the full account.
    """

    asset_symbol: str
    quote_currency: str
    amount: Decimal
    source: str
    as_of: datetime
    stale: bool


type PriceLookup = Price | PriceUnavailable
"""Either a price or a reason. **There is no third case and no wrapper with two fields.**

A union rather than an object holding `price: Price | None` and `reason: ... | None`, which
is the shape `_Failure` in `providers/endpoints.py` was written to replace: two fields can
disagree, and a caller meeting `price=None, reason=None` has to invent a meaning for it.
Branch with `isinstance(result, Price)`.
"""


@dataclass(frozen=True, slots=True)
class Holding:
    """How much of one asset is held. The input to a valuation.

    A quantity rather than a wallet or a balance snapshot, because neither exists yet: #10
    owns reading balances and storing them. Passing the quantities in keeps this service
    usable, and testable, before that lands -- and keeps it usable afterwards from a CLI or
    an importer that has quantities from somewhere else.
    """

    asset_symbol: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class ValuedHolding:
    """One holding, its price, and what the two multiply to.

    `value` is carried rather than recomputed by every caller, so that one multiplication
    happens in one place and `PortfolioValue.total` cannot disagree with the parts it was
    summed from.
    """

    asset_symbol: str
    quantity: Decimal
    price: Price
    value: Decimal


@dataclass(frozen=True, slots=True)
class UnpricedHolding:
    """One holding that could not be valued, and the reason.

    The reason travels with the symbol rather than in a parallel list, because a list of
    names and a list of reasons are two things that can come to be of different lengths.
    """

    asset_symbol: str
    quantity: Decimal
    reason: PriceUnavailable


@dataclass(frozen=True, slots=True)
class PortfolioValue:
    """What a set of holdings is worth, and what part of that question went unanswered.

    **`total` is the sum of the holdings that could be priced, and it is not the answer on
    its own.** Read without `complete` it is a number that silently omits a holding, which
    is indistinguishable from a number that includes it -- the failure this whole module is
    shaped around. Every renderer has to look at `complete`, and `unpriced` is there so that
    what it says can be specific.
    """

    quote_currency: str
    total: Decimal
    valued: tuple[ValuedHolding, ...]
    unpriced: tuple[UnpricedHolding, ...]

    @property
    def complete(self) -> bool:
        """Whether every holding was priced. Derived, never stored.

        A property rather than a field for the reason `ChainCapabilities.can_batch` is one:
        a stored copy is a second source of truth that can disagree with the tuple it was
        derived from, and the disagreement would be in the direction of claiming a partial
        total is whole.
        """
        return not self.unpriced


class PriceService:
    """Reads the price cache and values holdings against it. Opens no socket, writes nothing.

    **It takes no session, and the absence is a statement.** `WalletService` and
    `PriceRefreshService` both hold one because both commit; every method here reads, so a
    session on this class would be a field nothing uses and a claim no test can check --
    and worse, it would suggest this service owns a transaction. `build_price_service`
    takes the session, because the repositories need one.
    """

    def __init__(
        self,
        *,
        prices: PriceRepository,
        assets: AssetRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._prices = prices
        self._assets = assets
        self._clock = clock

    async def lookup_price(self, asset_symbol: str, quote_currency: str) -> PriceLookup:
        """This pair's price, or the reason there is not one. **Never a zero.**

        One clock read, at the start, so that the `stale` on the returned price and the
        `as_of` it was compared against describe the same instant.

        Raises nothing. A caller asking about an asset that is not in the table, or a
        currency nothing was ever stored in, gets `NEVER_FETCHED` rather than an exception:
        both are "there is no row", and an exception would make the ordinary case of a
        refresh that has not run yet something every caller had to catch.
        """
        now = self._clock()
        asset = await self._assets.get_by_symbol(asset_symbol)
        if asset is None:
            return PriceUnavailable.NEVER_FETCHED
        row = await self._prices.get(asset_id=asset.id, quote_currency=quote_currency)
        if row is None:
            return PriceUnavailable.NEVER_FETCHED
        return _price_of(row.as_of, asset_symbol, quote_currency, row.amount, row.source, now)

    async def value_portfolio(
        self,
        holdings: Sequence[Holding],
        *,
        quote_currency: str,
    ) -> PortfolioValue:
        """What these holdings are worth in one currency, and what could not be valued.

        **Nothing is converted and no cross rate is used.** A EUR valuation reads EUR rows
        and nothing else; a holding with a USD price and no EUR price is unpriced, not
        translated. Deriving one currency from another would put a second vendor's error
        into every number with nothing saying so, and the error would be invisible because
        the result would still look like a price.

        **The total is summed in Python**, over rows loaded whole. `SUM()` on `prices.amount`
        would coerce a `TEXT` money column to a float in SQLite, which is the corruption
        `NumericText` exists to prevent, applied to every row at once. The table has four
        rows and a portfolio has a handful of assets; this is not a performance question.

        One query for the assets and one for the prices, regardless of how many holdings
        were passed, so the cost does not grow with the portfolio.

        A holding of zero is valued rather than skipped: zero of something priced is a
        `ValuedHolding` worth nothing, which is a different statement from a holding nobody
        could price, and collapsing the two is exactly the ambiguity this module refuses.

        Args:
            holdings: what is held, as quantities. Duplicates are not merged -- each is
                valued on its own, because merging would be a policy decision about what a
                repeated symbol means and this layer has not been told one.
            quote_currency: the currency to value in. An unknown one prices nothing and
                every holding comes back unpriced.

        Returns:
            The total of what could be priced, the priced holdings, and the unpriced ones
            with their reasons. `complete` is false whenever anything is unpriced.
        """
        now = self._clock()
        assets = await self._assets.by_symbol()
        rows = await self._prices.list_for_currency(quote_currency)
        by_asset_id = {row.asset_id: row for row in rows}

        valued: list[ValuedHolding] = []
        unpriced: list[UnpricedHolding] = []
        for holding in holdings:
            asset = assets.get(holding.asset_symbol)
            row = by_asset_id.get(asset.id) if asset is not None else None
            if row is None:
                unpriced.append(
                    UnpricedHolding(
                        asset_symbol=holding.asset_symbol,
                        quantity=holding.quantity,
                        reason=PriceUnavailable.NEVER_FETCHED,
                    )
                )
                continue
            price = _price_of(
                row.as_of,
                holding.asset_symbol,
                quote_currency,
                row.amount,
                row.source,
                now,
            )
            valued.append(
                ValuedHolding(
                    asset_symbol=holding.asset_symbol,
                    quantity=holding.quantity,
                    price=price,
                    value=holding.quantity * price.amount,
                )
            )

        # `Decimal(0)` as the start value, not the `0` that `sum` defaults to: an empty
        # portfolio must come back as a Decimal like every other total, and an `int` zero
        # leaking out of here would be a different type in the one case nobody tests.
        total = sum((entry.value for entry in valued), Decimal(0))
        return PortfolioValue(
            quote_currency=quote_currency,
            total=total,
            valued=tuple(valued),
            unpriced=tuple(unpriced),
        )


def _price_of(
    as_of: datetime,
    asset_symbol: str,
    quote_currency: str,
    amount: Decimal,
    source: str,
    now: datetime,
) -> Price:
    """Build the snapshot, deciding staleness against the clock read the caller made.

    One function rather than the comparison written at each call site, because two copies
    of `now - as_of > STALE_AFTER` is how one of them comes to say `>=`.

    Both operands are timezone-aware: `UtcDateTime` attaches UTC on the way out of the
    database, and the clock is a `datetime.now(UTC)`. Subtracting a naive datetime from an
    aware one raises rather than guessing, which is the outcome to want -- Ruff's `DTZ`
    rules keep a naive one from being produced in the first place.
    """
    return Price(
        asset_symbol=asset_symbol,
        quote_currency=quote_currency,
        amount=amount,
        source=source,
        as_of=as_of,
        stale=now - as_of > STALE_AFTER,
    )


def build_price_service(
    session: AsyncSession,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> PriceService:
    """Assemble the read-side service over one database session.

    The repositories are built here rather than injected because there is exactly one
    implementation of each; the clock is injectable so that a test can name the instant and
    watch one row go from fresh to stale without waiting an hour.

    The session goes into the repositories and no further: see `PriceService` for why it
    does not reach the service itself.
    """
    return PriceService(
        prices=PriceRepository(session),
        assets=AssetRepository(session),
        clock=clock,
    )
