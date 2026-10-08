"""The value-history chart's data: what the wallets were worth on each day (spec 037).

`PortfolioHistoryService` reads each active wallet's closing snapshot per day and the daily
prices in `price_history`, and hands the arithmetic to `domain.portfolio_history`. Like the
summary it reads only what is stored: no chain and no price source is asked, and this module
imports nothing under `portfolio.providers`.

The prices are USD, read as USDT one for one, for the reason `services/portfolio.py` gives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Final

from portfolio.domain.chains import ChainKey
from portfolio.domain.currencies import QuoteCurrency
from portfolio.domain.money import from_base_units
from portfolio.domain.portfolio_history import (
    DailyReading,
    DayValue,
    HistoryRange,
    WalletDay,
    WalletReadings,
    days_of,
    portfolio_days,
    wallet_days,
)
from portfolio.repositories.assets import AssetRepository
from portfolio.repositories.balances import BalanceRepository
from portfolio.repositories.price_history import PriceHistoryRepository
from portfolio.repositories.wallets import WalletRepository
from portfolio.services.prices import utc_now
from portfolio.services.wallets import WalletNotFoundError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date, datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.db.models import BalanceSnapshot, Wallet

__all__ = [
    "HistoryRange",
    "PortfolioHistory",
    "PortfolioHistoryService",
    "WalletValueHistory",
    "build_portfolio_history_service",
]
"""`HistoryRange` is re-exported, so the API layer takes its query type from the service."""

QUOTE: Final = QuoteCurrency.USD
"""The currency the history is valued in: USD, read as USDT."""


@dataclass(frozen=True, slots=True)
class PortfolioHistory:
    """Every active wallet together, one point per day of the range."""

    history_range: HistoryRange
    points: tuple[DayValue, ...]


@dataclass(frozen=True, slots=True)
class WalletValueHistory:
    """One wallet, one point per day of the range."""

    wallet_id: int
    asset: str
    history_range: HistoryRange
    points: tuple[WalletDay, ...]


class PortfolioHistoryService:
    """Reads snapshots and daily prices, and builds the history. Writes nothing.

    The clock decides "today", the last day of every range; it is injected so a test can
    name it.
    """

    def __init__(
        self,
        *,
        wallets: WalletRepository,
        balances: BalanceRepository,
        history: PriceHistoryRepository,
        assets: AssetRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._wallets = wallets
        self._balances = balances
        self._history = history
        self._assets = assets
        self._clock = clock

    async def portfolio(self, user_id: int, history_range: HistoryRange) -> PortfolioHistory:
        """The owner's active wallets together, over `history_range` (R6, R7)."""
        today = self._clock().astimezone(UTC).date()
        wallets = await self._wallets.list_for_user(user_id)
        series = await self._readings(wallets)
        days = days_of(history_range, today=today, first_reading=_first_day(series))
        prices = await self._prices({reading.asset for reading in series}, days)
        return PortfolioHistory(
            history_range=history_range, points=portfolio_days(days, series, prices)
        )

    async def wallet(
        self, user_id: int, wallet_id: int, history_range: HistoryRange
    ) -> WalletValueHistory:
        """One of the owner's wallets, archived ones included, over `history_range`.

        Raises:
            WalletNotFoundError: no wallet with that id belongs to the owner.
        """
        wallet = await self._wallets.get_for_user(user_id, wallet_id)
        if wallet is None:
            raise WalletNotFoundError
        today = self._clock().astimezone(UTC).date()
        (series,) = await self._readings([wallet])
        days = days_of(history_range, today=today, first_reading=_first_day([series]))
        prices = await self._prices({series.asset}, days)
        return WalletValueHistory(
            wallet_id=wallet.id,
            asset=series.asset,
            history_range=history_range,
            points=wallet_days(days, series, prices.get(series.asset, {})),
        )

    async def _readings(self, wallets: Sequence[Wallet]) -> list[WalletReadings]:
        """Each wallet's asset and its closing reading per day, in the order given."""
        closing = await self._balances.daily_closing([wallet.id for wallet in wallets])
        return [
            WalletReadings(
                asset=ChainKey(wallet.chain_key).asset_symbol,
                readings=tuple(_reading(row) for row in closing.get(wallet.id, [])),
            )
            for wallet in wallets
        ]

    async def _prices(
        self, symbols: set[str], days: Sequence[date]
    ) -> dict[str, dict[date, Decimal]]:
        """The daily price of each symbol over `days`, keyed by symbol."""
        assets = await self._assets.by_symbol()
        ids = {assets[symbol].id: symbol for symbol in symbols if symbol in assets}
        by_id = await self._history.series(
            asset_ids=sorted(ids), quote_currency=QUOTE, first_day=days[0], last_day=days[-1]
        )
        return {ids[asset_id]: prices for asset_id, prices in by_id.items()}


def _reading(row: BalanceSnapshot) -> DailyReading:
    """A closing snapshot as the domain reads it: its UTC day and its confirmed quantity."""
    return DailyReading(
        day=row.observed_at.astimezone(UTC).date(),
        quantity=from_base_units(row.confirmed, row.decimals),
    )


def _first_day(series: Sequence[WalletReadings]) -> date | None:
    """The first day any of these wallets was read, or `None`."""
    return min(
        (wallet.readings[0].day for wallet in series if wallet.readings),
        default=None,
    )


def build_portfolio_history_service(
    session: AsyncSession, *, clock: Callable[[], datetime] = utc_now
) -> PortfolioHistoryService:
    """Assemble the read-side service over one database session."""
    return PortfolioHistoryService(
        wallets=WalletRepository(session),
        balances=BalanceRepository(session),
        history=PriceHistoryRepository(session),
        assets=AssetRepository(session),
        clock=clock,
    )
