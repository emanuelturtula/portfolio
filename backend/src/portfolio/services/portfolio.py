"""The dashboard's summary: what the wallets hold and what it is worth.

`PortfolioService.summary` reads what the balance sync and the price refresh stored -- no
chain and no price source is asked -- and hands the arithmetic to `domain.portfolio`.

## What is held

Each active wallet's latest balance snapshot, whatever its age, summed per asset exactly. A
reading that is no longer current still counts: it is the last thing known, and dropping it
would show the coins as gone. It is named in `missing` instead, by the rule
`services.wallet_readings` states. A wallet that has never been read adds nothing, and is
named too.

## What it is worth

The cached USD price of each holding, read as USDT one for one: the dashboard shows USDT,
and adding a second price source to measure a spread of a fraction of a percent is not worth
a second set of failures. An unpriced holding is named in `missing` and adds nothing; a stale
price still values its holding and is named.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from portfolio.domain.chains import ChainKey
from portfolio.domain.currencies import QuoteCurrency
from portfolio.domain.money import add, from_base_units
from portfolio.domain.portfolio import PortfolioSummary, PricedQuantity, summarize
from portfolio.repositories.balances import BalanceRepository
from portfolio.repositories.sync_runs import SyncRunRepository
from portfolio.repositories.wallets import WalletRepository
from portfolio.services.prices import Holding, PriceService, build_price_service
from portfolio.services.wallet_readings import wallet_reading_problem

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "Missing",
    "MissingKind",
    "PortfolioService",
    "PortfolioSummaryView",
    "build_portfolio_service",
    "utc_now",
]


_NOTHING: Final = Decimal(0)
"""What a sum starts from. `add` keeps the smaller exponent, so the first quantity sets the
scale."""


def utc_now() -> datetime:
    """The production clock, aware and in UTC."""
    return datetime.now(UTC)


class MissingKind(StrEnum):
    """What a summary figure could not include, or could only include as it last stood.

    The member is its own wire form. `Missing.subject` says which one:

    * `wallet_unread` -- a wallet on chain `subject` has never been read: it adds nothing.
    * `wallet_stale` -- a wallet on chain `subject` is counted at a reading that is no longer
      current: its chain failed in the latest balance run, or the reading is over a day old.
    * `unpriced` -- asset `subject` is held and has no price: it adds nothing to the value.
    * `stale_price` -- asset `subject` is valued at a price over an hour old.
    """

    WALLET_UNREAD = "wallet_unread"
    WALLET_STALE = "wallet_stale"
    UNPRICED = "unpriced"
    STALE_PRICE = "stale_price"


@dataclass(frozen=True, slots=True, order=True)
class Missing:
    """One thing a figure could not include as current, and which one: a chain or an asset.
    Ordered, so a sorted tuple of them is stable."""

    kind: MissingKind
    subject: str


@dataclass(frozen=True, slots=True)
class PortfolioSummaryView:
    """The summary and what it is missing.

    `missing` is sorted and holds each pair once: two unread wallets on one chain are one
    entry. Empty means every figure is whole and current.
    """

    summary: PortfolioSummary
    missing: tuple[Missing, ...]


class PortfolioService:
    """Reads the stored balances and prices, and summarizes them. Writes nothing.

    It takes no session, for the reason `PriceService` does not: every method here reads.
    The clock is what a reading's age is measured against, injected so a test can name it.
    """

    def __init__(
        self,
        *,
        wallets: WalletRepository,
        balances: BalanceRepository,
        sync_runs: SyncRunRepository,
        prices: PriceService,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._wallets = wallets
        self._balances = balances
        self._sync_runs = sync_runs
        self._prices = prices
        self._clock = clock

    async def summary(self, user_id: int) -> PortfolioSummaryView:
        """The owner's total value, and every holding's share of it.

        The clock is read once, so every reading is held to the same instant. The latest
        finished balance run is read before the snapshots: a reading committed between the two
        reads is then newer than the run's verdict, never older (spec 028, R3).
        """
        now = self._clock()
        missing: set[Missing] = set()
        quantities: dict[str, Decimal] = {}

        wallets = await self._wallets.list_for_user(user_id)
        latest_run = await self._sync_runs.latest_finished()
        readings = await self._balances.latest_for_wallets([wallet.id for wallet in wallets])
        for wallet in wallets:
            reading = readings.get(wallet.id)
            if reading is None:
                missing.add(Missing(MissingKind.WALLET_UNREAD, wallet.chain_key))
                continue
            if wallet_reading_problem(wallet.chain_key, reading, latest_run, now) is not None:
                missing.add(Missing(MissingKind.WALLET_STALE, wallet.chain_key))
            symbol = ChainKey(wallet.chain_key).asset_symbol
            quantity = from_base_units(reading.confirmed, reading.decimals)
            quantities[symbol] = add(quantities.get(symbol, _NOTHING), quantity)

        held = {asset: quantity for asset, quantity in quantities.items() if quantity != 0}
        valuation = await self._prices.value_portfolio(
            [Holding(asset_symbol=asset, quantity=quantity) for asset, quantity in held.items()],
            quote_currency=QuoteCurrency.USD,
        )
        priced = [
            PricedQuantity(entry.asset_symbol, entry.quantity, entry.price.amount)
            for entry in valuation.valued
        ]
        priced.extend(
            PricedQuantity(entry.asset_symbol, entry.quantity, None) for entry in valuation.unpriced
        )
        missing.update(
            Missing(MissingKind.STALE_PRICE, entry.asset_symbol)
            for entry in valuation.valued
            if entry.price.stale
        )
        missing.update(
            Missing(MissingKind.UNPRICED, entry.asset_symbol) for entry in valuation.unpriced
        )

        return PortfolioSummaryView(summary=summarize(priced), missing=tuple(sorted(missing)))


def build_portfolio_service(
    session: AsyncSession,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> PortfolioService:
    """Assemble the read-side service over one database session.

    The repositories are built here rather than injected because there is exactly one
    implementation of each. The clock is shared with the price service, so a test that names
    the instant names it for a price's age and a reading's alike.
    """
    return PortfolioService(
        wallets=WalletRepository(session),
        balances=BalanceRepository(session),
        sync_runs=SyncRunRepository(session),
        prices=build_price_service(session, clock=clock),
        clock=clock,
    )
