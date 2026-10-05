"""The dashboard's summary: what is held, what it is worth, what went into it (#154).

`PortfolioService.summary` reads what the syncs and the price refresh stored -- no chain, no
venue and no price source is asked -- and hands the arithmetic to `domain.portfolio`.

## What is held

Every **tracked** asset -- one with a row in `assets`, which is what the price refresh prices
-- summed over two sources, exactly:

* each active wallet's latest balance snapshot, whatever its age;
* each exchange account's stored spot balances, whatever their age.

A reading that is no longer current still counts: it is the last thing known, and dropping
it would show the coins as gone. It is named in `missing` instead, by the rules the holdings
check already applies (`services.reconciliation`), so the two pages cannot disagree about
which reading is out of date. A wallet or a venue that has never been read adds nothing, and
is named too.

Two kinds of balance are not holdings. **Cash** -- `DEFAULT_CASH_ASSETS` -- because it was
never invested, so counting it in the value and not in the invested figure would put it in
the P/L. **Untracked** assets -- the dust a venue pays rebates in, say -- because nothing
prices them; they are listed by name in `untracked`, so leaving them out is visible, but they
do not mark the total partial, which they would forever.

## What it is worth

The cached USD price of each holding, read as USDT one for one: the dashboard shows USDT,
and adding a second price source to measure a spread of a fraction of a percent is not worth
a second set of failures. An unpriced holding is named in `missing` and adds nothing; a stale
price still values its holding and is named.

## What went into it

`domain.portfolio.net_invested` over every stored fill of the owner's accounts. Decoding the
rows and summing them runs in a worker thread, for the reason `services/exchanges.py` gives:
at tens of thousands of fills it is most of the request, and on the event loop every other
request would wait for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from anyio import to_thread

from portfolio.domain.accounting import DEFAULT_CASH_ASSETS
from portfolio.domain.chains import ChainKey
from portfolio.domain.currencies import QuoteCurrency
from portfolio.domain.money import add, from_base_units
from portfolio.domain.portfolio import (
    NetInvested,
    PortfolioSummary,
    PricedQuantity,
    net_invested,
    summarize,
)
from portfolio.repositories.assets import AssetRepository
from portfolio.repositories.balances import BalanceRepository
from portfolio.repositories.exchange_balances import ExchangeBalanceRepository
from portfolio.repositories.exchanges import ExchangeFillRepository, decode_fill_view_rows
from portfolio.repositories.sync_runs import SyncRunRepository
from portfolio.repositories.wallets import WalletRepository
from portfolio.services.exchanges import fill_line_of
from portfolio.services.prices import Holding, PriceService, build_price_service
from portfolio.services.reconciliation import not_compared_reason, wallet_not_compared_reason

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

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
scale, as in `services/reconciliation.py`."""


def utc_now() -> datetime:
    """The production clock, aware and in UTC."""
    return datetime.now(UTC)


class MissingKind(StrEnum):
    """What a summary figure could not include, or could only include as it last stood.

    The member is its own wire form. `Missing.subject` says which one:

    * `wallet_unread` -- a wallet on chain `subject` has never been read: it adds nothing.
    * `wallet_stale` -- a wallet on chain `subject` is counted at a reading that is no longer
      current: its chain failed in the latest balance run, or the reading is over a day old.
    * `exchange_unread` -- venue `subject`'s balances have never been read: it adds nothing.
    * `exchange_stale` -- venue `subject` is counted at a reading that is no longer current:
      the last read failed, its fill sync is not `ok`, or the reading is over a day old.
    * `unpriced` -- asset `subject` is held and has no price: it adds nothing to the value.
    * `stale_price` -- asset `subject` is valued at a price over an hour old.
    * `fill_not_in_cash` -- fills quoted in `subject`, which is not cash, are not in the
      invested figure.
    """

    WALLET_UNREAD = "wallet_unread"
    WALLET_STALE = "wallet_stale"
    EXCHANGE_UNREAD = "exchange_unread"
    EXCHANGE_STALE = "exchange_stale"
    UNPRICED = "unpriced"
    STALE_PRICE = "stale_price"
    FILL_NOT_IN_CASH = "fill_not_in_cash"


@dataclass(frozen=True, slots=True, order=True)
class Missing:
    """One thing a figure could not include as current, and which one: a chain, a venue or an
    asset. Ordered, so a sorted tuple of them is stable."""

    kind: MissingKind
    subject: str


@dataclass(frozen=True, slots=True)
class PortfolioSummaryView:
    """The summary, what it is missing, and the assets held that nothing tracks.

    `missing` is sorted and holds each pair once: two unread wallets on one chain are one
    entry. Empty means every figure is whole and current.
    """

    summary: PortfolioSummary
    missing: tuple[Missing, ...]
    untracked: tuple[str, ...]


class PortfolioService:
    """Reads the stored balances, prices and fills, and summarizes them. Writes nothing.

    It takes no session, for the reason `PriceService` does not: every method here reads.
    The clock is what a reading's age is measured against, injected so a test can name it.
    """

    def __init__(
        self,
        *,
        wallets: WalletRepository,
        balances: BalanceRepository,
        sync_runs: SyncRunRepository,
        exchange_balances: ExchangeBalanceRepository,
        fills: ExchangeFillRepository,
        assets: AssetRepository,
        prices: PriceService,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._wallets = wallets
        self._balances = balances
        self._sync_runs = sync_runs
        self._exchange_balances = exchange_balances
        self._fills = fills
        self._assets = assets
        self._prices = prices
        self._clock = clock

    async def summary(self, user_id: int) -> PortfolioSummaryView:
        """The owner's total value, net invested and P/L, and every holding's share.

        The clock is read once, so every reading is held to the same instant. The latest
        finished balance run is read before the snapshots, in the order the holdings check
        reads them and for its reason (spec 028, R3).
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
            if wallet_not_compared_reason(wallet.chain_key, reading, latest_run, now) is not None:
                missing.add(Missing(MissingKind.WALLET_STALE, wallet.chain_key))
            symbol = ChainKey(wallet.chain_key).asset_symbol
            quantity = from_base_units(reading.confirmed, reading.decimals)
            quantities[symbol] = add(quantities.get(symbol, _NOTHING), quantity)

        for account in await self._exchange_balances.list_for_user(user_id):
            if account.balances_read_at is None:
                missing.add(Missing(MissingKind.EXCHANGE_UNREAD, account.exchange_key))
                continue
            if not_compared_reason(account, now) is not None:
                missing.add(Missing(MissingKind.EXCHANGE_STALE, account.exchange_key))
            for balance in account.balances:
                previous = quantities.get(balance.asset, _NOTHING)
                quantities[balance.asset] = add(previous, balance.quantity)

        tracked = await self._assets.by_symbol()
        # Cash first: USDT has a row in `assets` too, and it is still not a holding.
        non_cash = {
            asset: quantity
            for asset, quantity in quantities.items()
            if asset not in DEFAULT_CASH_ASSETS and quantity != 0
        }
        held = {asset: quantity for asset, quantity in non_cash.items() if asset in tracked}
        untracked = tuple(sorted(asset for asset in non_cash if asset not in tracked))

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

        rows = await self._fills.fetch_fill_view_rows(user_id, None)
        invested = await to_thread.run_sync(_invested, rows, abandon_on_cancel=True)
        missing.update(
            Missing(MissingKind.FILL_NOT_IN_CASH, quote) for quote in invested.unvalued_quotes
        )

        return PortfolioSummaryView(
            summary=summarize(priced, invested.amount),
            missing=tuple(sorted(missing)),
            untracked=untracked,
        )


def _invested(rows: Sequence[Sequence[Any]]) -> NetInvested:
    """Decode the stored fills and sum the cash they moved. Pure, so it runs in a thread."""
    lines = (fill_line_of(record) for record in decode_fill_view_rows(rows))
    return net_invested(lines, DEFAULT_CASH_ASSETS)


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
        exchange_balances=ExchangeBalanceRepository(session),
        fills=ExchangeFillRepository(session),
        assets=AssetRepository(session),
        prices=build_price_service(session, clock=clock),
        clock=clock,
    )
