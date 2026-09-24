"""Filling the price cache from the vendors. **The only module in `services/` that may.**

That isolation is the guarantee, and it is written here because a guarantee nobody wrote
down is one the next refactor merges away. `backend/.importlinter`'s
`prices-are-never-fetched-in-a-request` contract forbids any chain from
`portfolio.api.routers` to `portfolio.providers.prices`, with no `allow_indirect_imports` --
so **this module is the single place a reviewer has to look to answer "can a request reach a
vendor".** As long as nothing under `api/routers` imports it, directly or through anything
else, the answer is no, mechanically.

The corollary is the rule that has to survive: **do not import this module from
`services/prices.py`.** The valuation service is deliberately provider-free so that a
valuation endpoint is possible at all, and the tempting change -- "just refresh it if it is
stale" inside `value_portfolio` -- is precisely the one that turns every dashboard render
into four vendor calls. It would also turn the contract red, which is the point of writing
it without `allow_indirect_imports`.

## Still no scheduler, and that is now a gap rather than a deferral

#9 delivered `refresh_prices()` as a service with **no caller in the running application**
and a CLI entry point beside it, so the call budget could be measured by hand before
anything automated it. #10 was to own the scheduling; what #10 actually scheduled was
**balances**, because that is the scope its issue set.

So this is still called only by `portfolio refresh-prices`, and the consequence is visible
to a user rather than only to a maintainer: on a fresh deployment `GET /api/balances/current`
reports every holding under `unpriced` with `reason: never_fetched` until an operator runs
the command. `services/scheduler.py` is generic over what it ticks and the shared HTTP client
is now open for the life of the process, so the missing piece is a caller rather than a
design. `docs/providers.md` records it as outstanding with no owner yet.

## The sources are handed in, never imported by the caller's caller

`PriceRefreshService` takes its sources as a constructor argument, and `cli.py` builds them
with `providers.prices.registry.price_sources`. So the service depends on the `PriceSource`
protocol and on the failover loop, and never on which vendors exist -- which is what lets a
test drive the whole refresh with two fakes and no network.

## One clock read per refresh, taken *before* the first request

`as_of` and `fetched_at` on every row written by one call come from the same read. Two reads
would let two rows in one refresh disagree about when it happened, and staleness is computed
from `as_of`.

**So `as_of` is the instant the refresh started, not the instant each price was observed**,
and the difference is real rather than theoretical: the shared limiter paces requests at one
per second per host, so a refresh that fails over across three hosts stamps its rows several
seconds before the answers actually arrived. It is bounded by how long a refresh can take,
which is seconds, against a staleness threshold of an hour.

Kept, because the error is in the safe direction and it buys something worth more than the
seconds: **one instant for the whole refresh.** A per-quote timestamp would make two rows
written by one call disagree about when that call happened, and the alternative that fixed
both -- reading the clock again after the fetch -- would date a price *later* than it was
observed, which makes a stale price look fresher than it is. Erring early is the only one of
the three that cannot do that.

None of this makes `as_of` a vendor's quote time. No vendor supplies one; see
`providers/prices/base.py`. It is our clock, read at the start of our refresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from portfolio.providers.prices.base import SUPPORTED_PAIRS, fetch_prices, sources_for
from portfolio.repositories.assets import AssetRepository
from portfolio.repositories.prices import PriceRepository
from portfolio.services.prices import PriceUnavailable, utc_now

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.providers.prices.base import PricePair, PriceQuote, PriceSource

__all__ = [
    "PriceRefreshService",
    "RefreshReport",
    "RefreshedPair",
    "UnavailablePair",
    "UnknownAssetError",
    "build_price_refresh_service",
]


class UnknownAssetError(Exception):
    """A source answered about an asset that has no row in `assets`.

    Loud rather than a dropped quote, because the failure it prevents is silent: a pair
    that reports "never fetched" forever while every refresh appears to succeed.

    **Two ways to arrive here, and neither is a misbehaving vendor.** A quote can only
    exist for a pair in `SUPPORTED_PAIRS` -- `sources_for` refuses anything else without a
    request, and `fetch_prices` discards a response that volunteers a pair nobody asked
    for -- so a vendor cannot cause this.

    * **A pair was added to `SUPPORTED_PAIRS` without the migration that seeds its asset.**
      A code change, caught the first time the refresh runs.
    * **The `assets` row is missing from a database that should have it.** A file restored
      from before `0002_seed_assets`, or a row deleted by hand on the host. This is the one
      that happens in production, and it is why the guard is a raise rather than an
      assertion about our own constants: the process cannot fix it and must not paper over
      it.

    Nothing is committed when it fires, so a refresh that hits it leaves the previous
    prices standing rather than writing a partial set.

    Names the symbol, which is a public ticker rather than anything about the owner.
    """

    def __init__(self, asset_symbol: str) -> None:
        self.asset_symbol = asset_symbol
        super().__init__(
            f"No asset row exists for symbol {asset_symbol!r}, so its price cannot be "
            f"stored. A supported pair needs a seeded asset."
        )


@dataclass(frozen=True, slots=True)
class RefreshedPair:
    """One pair that was fetched and stored, and which source actually answered.

    `source` is the failover's answer rather than the first source asked, which is the whole
    reason the field exists on the report as well as in the column: an operator reading a
    refresh that took four seconds wants to see that Kraken was skipped, without querying
    the table.

    **`amount` is the value the table holds, read back after the commit, not the value the
    vendor sent.** `prices.amount` rounds to `PRICE_SCALE` places, so the two differ for a
    price finer than that -- and a report that showed the vendor's number would disagree
    with the row every later valuation reads, at the one moment somebody is watching.
    """

    asset_symbol: str
    quote_currency: str
    source: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class UnavailablePair:
    """One pair that was not stored, and which of the four reasons applies.

    Three of the four `PriceUnavailable` members are produced here and nowhere else;
    `NEVER_FETCHED` is the read path's. See `services/prices.py` for why the producers are
    split and the vocabulary is not.
    """

    asset_symbol: str
    quote_currency: str
    reason: PriceUnavailable


@dataclass(frozen=True, slots=True)
class RefreshReport:
    """What one refresh did: what it stored, what it could not, and when it began.

    **`as_of` is the instant the refresh started**, not the instant any individual price
    was observed, and it is the value written to both timestamp columns of every row the
    call wrote. The module docstring has the argument for that.

    **A refresh that stored nothing is not an exception.** Every vendor being down is a
    real state the caller has to be able to report, and raising would throw away the pairs
    that did succeed alongside the ones that did not -- the same argument `fetch_prices`
    makes for returning its `unanswered` rather than raising on it.

    Both tuples are sorted by pair, so a report is stable between runs and a CLI rendering
    it produces a diffable transcript.
    """

    as_of: datetime
    refreshed: tuple[RefreshedPair, ...]
    unavailable: tuple[UnavailablePair, ...]


class PriceRefreshService:
    """Fetches every configured pair and writes the result into the price cache.

    Owns the transaction: the repository flushes and this class commits. The caller -- the
    CLI today, #10's scheduler later -- owns the session and closes it, so an exception
    leaves uncommitted work rolled back rather than half applied.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        prices: PriceRepository,
        assets: AssetRepository,
        sources: Sequence[PriceSource],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._prices = prices
        self._assets = assets
        self._sources = tuple(sources)
        self._clock = clock

    async def refresh_prices(self, pairs: Sequence[PricePair] | None = None) -> RefreshReport:
        """Fetch these pairs and store what came back, reporting the rest as reasons.

        `None` means every pair in `SUPPORTED_PAIRS`, sorted -- which is what the scheduler
        and the CLI both want, and sorting makes the request Kraken receives identical
        between runs.

        The order of the work, and each step is a decision:

        1. **Unsupported pairs are refused before anything is asked.** A pair outside
           `SUPPORTED_PAIRS` costs no request and is reported `UNSUPPORTED_PAIR`, because
           nothing was asked and "every source failed" would send an operator to look at
           vendors that were never called.
        2. **A supported pair with no eligible source is `NO_SOURCE_CONFIGURED`**, also
           without a request. In the shipped configuration this cannot happen -- Kraken is
           key-free, always constructed, and lists all four pairs -- so it is reachable
           only by constructing this service with a source list that is empty or does not
           cover a pair. That is not a hypothetical: it is exactly what a future
           configuration switch turning a source off would produce, and it is how a test
           reaches this branch. Reporting it as a failed fetch would be a lie about a call
           nobody made.
        3. **Everything else goes to `fetch_prices`**, which tries each eligible source in
           turn and returns the quotes it got plus the pairs nothing answered. Those become
           `EVERY_SOURCE_FAILED` -- the only one of the three that means "go and look at a
           vendor".
        4. **The quotes are written, then the transaction commits once.** One commit for
           the whole refresh, so a crash halfway leaves the previous prices intact rather
           than a cache that is half new and half old with nothing recording which is which.
        5. **The rows are read back and the report is built from them**, so what it says was
           stored is what was stored. See `RefreshedPair.amount`.

        Raises:
            UnknownAssetError: a quote arrived for a symbol with no row in `assets`. A
                wiring mistake, and loud on purpose; nothing is committed.

        Returns:
            The report. Never raises for a vendor failure -- that is a line in the report.
        """
        requested = list(pairs) if pairs is not None else sorted(SUPPORTED_PAIRS)
        as_of = self._clock()

        unavailable: list[UnavailablePair] = []
        to_fetch: list[PricePair] = []
        for pair in requested:
            asset_symbol, quote_currency = pair
            eligible = sources_for(asset_symbol, quote_currency, self._sources)
            if eligible:
                to_fetch.append(pair)
                continue
            unavailable.append(
                UnavailablePair(
                    asset_symbol=asset_symbol,
                    quote_currency=quote_currency,
                    reason=(
                        PriceUnavailable.UNSUPPORTED_PAIR
                        if pair not in SUPPORTED_PAIRS
                        else PriceUnavailable.NO_SOURCE_CONFIGURED
                    ),
                )
            )

        fetched = await fetch_prices(to_fetch, self._sources)
        unavailable.extend(
            UnavailablePair(
                asset_symbol=asset_symbol,
                quote_currency=quote_currency,
                reason=PriceUnavailable.EVERY_SOURCE_FAILED,
            )
            for asset_symbol, quote_currency in fetched.unanswered
        )

        assets = await self._assets.by_symbol()
        written: list[tuple[int, PriceQuote]] = []
        for quote in fetched.quotes:
            asset = assets.get(quote.asset_symbol)
            if asset is None:
                raise UnknownAssetError(quote.asset_symbol)
            await self._prices.upsert(
                asset_id=asset.id,
                quote_currency=quote.quote_currency,
                amount=quote.amount,
                source=quote.source,
                # The same instant on both columns, from the one clock read taken before
                # the first request -- so this is when the refresh *started*, a few seconds
                # earlier than the answer arrived. The module docstring says why that is
                # the right way to be wrong. They diverge the day a vendor supplies a quote
                # time; none does today.
                as_of=as_of,
                fetched_at=as_of,
            )
            written.append((asset.id, quote))
        await self._session.commit()

        # **The report carries what the table holds, not what the vendor said**, and the
        # two are not always the same number: `prices.amount` is `NumericText(12)`, so a
        # price finer than twelve places is rounded on the way in. Reporting the quote
        # would hand an operator a transcript that disagrees with the row a later valuation
        # reads -- and it would make that rounding invisible at the one moment somebody is
        # looking. `NumericText` now refuses a value that rounds away to nothing, so what
        # remains is ordinary rounding, which is exactly the kind of thing worth showing
        # rather than hiding.
        #
        # `expire_all` is what makes the read a read. The session's identity map already
        # holds these rows, and the factory is built with `expire_on_commit=False`, so
        # without it the query would hand back the same objects still carrying the
        # unrounded `Decimal` that was assigned to them -- the in-memory value dressed up
        # as a round trip, which is the verifier sharing state with its subject.
        self._session.expire_all()
        stored = {(row.asset_id, row.quote_currency): row for row in await self._prices.list_all()}
        refreshed = [
            RefreshedPair(
                asset_symbol=quote.asset_symbol,
                quote_currency=quote.quote_currency,
                source=quote.source,
                amount=stored[(asset_id, quote.quote_currency)].amount,
            )
            for asset_id, quote in written
        ]

        return RefreshReport(
            as_of=as_of,
            refreshed=tuple(sorted(refreshed, key=_pair_key)),
            unavailable=tuple(sorted(unavailable, key=_pair_key)),
        )


def _pair_key(entry: RefreshedPair | UnavailablePair) -> tuple[str, str]:
    """Sort a report line by its pair, so two runs produce the same transcript."""
    return (entry.asset_symbol, entry.quote_currency)


def build_price_refresh_service(
    session: AsyncSession,
    *,
    sources: Sequence[PriceSource],
    clock: Callable[[], datetime] = utc_now,
) -> PriceRefreshService:
    """Assemble the refresh service over one database session and a built source list.

    `sources` is required and has no default, deliberately. A default would have to call
    `price_sources`, which would mean building an `httpx.AsyncClient` in here -- a
    process-wide object whose lifetime belongs to whoever opened it -- and would make the
    one thing this service must not decide, which vendors exist, a decision it makes
    silently when a caller forgets.
    """
    return PriceRefreshService(
        session=session,
        prices=PriceRepository(session),
        assets=AssetRepository(session),
        sources=sources,
        clock=clock,
    )
