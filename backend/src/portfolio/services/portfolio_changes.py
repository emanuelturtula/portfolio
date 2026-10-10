"""The change widget's data: how much the portfolio moved over 24 hours and 7 days (spec 041).

`PortfolioChangeService` takes the value now from the summary, so the widget and the hero
figure cannot disagree (R5), and the value at each past instant from each active wallet's
snapshot or rebuilt balance at that instant and the hourly closes in `price_hourly` (R2, R3).
The arithmetic is `domain.portfolio_change`'s. Like the summary it reads only what is stored:
no chain and no price source is asked, and this module imports nothing under
`portfolio.providers`.

The prices are USD, read as USDT one for one, for the reason `services/portfolio.py` gives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from portfolio.domain.chains import ChainKey
from portfolio.domain.currencies import QuoteCurrency
from portfolio.domain.money import from_base_units
from portfolio.domain.portfolio_change import (
    PERIODS,
    Change,
    WalletThen,
    change_over,
    quantity_at,
    value_at,
)
from portfolio.domain.portfolio_history import DailyReading
from portfolio.repositories.assets import AssetRepository
from portfolio.repositories.balances import BalanceRepository
from portfolio.repositories.price_hourly import PriceHourlyRepository
from portfolio.repositories.reconstructed_balances import ReconstructedBalanceRepository
from portfolio.repositories.wallets import WalletRepository
from portfolio.services.portfolio import (
    MissingKind,
    PortfolioService,
    build_portfolio_service,
)
from portfolio.services.prices import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.db.models import Wallet

__all__ = [
    "PRICE_MAX_AGE",
    "PortfolioChangeService",
    "PortfolioChanges",
    "build_portfolio_change_service",
]

QUOTE: Final = QuoteCurrency.USD
"""The currency the changes are worked out in: USD, read as USDT."""

PRICE_MAX_AGE: Final = timedelta(hours=2)
"""How long before an instant an hour may have ended and still price it (R2). The hourly
timer stores each hour as it ends, so a healthy table is never more than one hour behind."""

_UNKNOWN_NOW: Final = frozenset({MissingKind.WALLET_UNREAD, MissingKind.UNPRICED})
"""What makes the summary's total not the value now (R5): a wallet it could not count, or a
holding it could not value. A stale reading or price still values, as in the hero."""


@dataclass(frozen=True, slots=True)
class PortfolioChanges:
    """The value now, and the change over each period of `PERIODS`, in that order.

    `as_of` is the instant every period is measured back from. `value` is `None` when the
    value now is unknown.
    """

    as_of: datetime
    value: Decimal | None
    changes: tuple[Change, ...]


class PortfolioChangeService:
    """Reads the summary, past snapshots, rebuilt days and hourly prices. Writes nothing.

    The clock decides "now"; it is injected so a test can name it, and the summary is built
    over the same clock.
    """

    def __init__(
        self,
        *,
        summary: PortfolioService,
        wallets: WalletRepository,
        balances: BalanceRepository,
        rebuilt: ReconstructedBalanceRepository,
        hourly: PriceHourlyRepository,
        assets: AssetRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._summary = summary
        self._wallets = wallets
        self._balances = balances
        self._rebuilt = rebuilt
        self._hourly = hourly
        self._assets = assets
        self._clock = clock

    async def changes(self, user_id: int) -> PortfolioChanges:
        """The owner's change over 24 hours and over 7 days, each by R6."""
        now = self._clock()
        view = await self._summary.summary(user_id)
        unknown_now = any(missing.kind in _UNKNOWN_NOW for missing in view.missing)
        value_now = None if unknown_now else view.summary.total_value

        wallets = await self._wallets.list_for_user(user_id)
        rebuilt = await self._rebuilt.for_wallets([wallet.id for wallet in wallets])
        readings = {
            wallet_id: [
                DailyReading(day=row.day, quantity=from_base_units(row.confirmed, row.decimals))
                for row in rows
            ]
            for wallet_id, rows in rebuilt.items()
        }

        changes = []
        for period, length in PERIODS.items():
            since = now - length
            then = await self._wallets_at(wallets, readings, since)
            prices = await self._prices_at({wallet.asset for wallet in then}, since)
            changes.append(
                change_over(period, since=since, now=value_now, then=value_at(then, prices))
            )
        return PortfolioChanges(as_of=now, value=value_now, changes=tuple(changes))

    async def _wallets_at(
        self,
        wallets: Sequence[Wallet],
        readings: dict[int, list[DailyReading]],
        at: datetime,
    ) -> list[WalletThen]:
        """Each wallet's asset and quantity at the instant `at` (R3)."""
        snapshots = await self._balances.latest_at_or_before([wallet.id for wallet in wallets], at)
        then = []
        for wallet in wallets:
            snapshot = snapshots.get(wallet.id)
            quantity = (
                None if snapshot is None else from_base_units(snapshot.confirmed, snapshot.decimals)
            )
            then.append(
                WalletThen(
                    asset=ChainKey(wallet.chain_key).asset_symbol,
                    quantity=quantity_at(at, quantity, readings.get(wallet.id, [])),
                )
            )
        return then

    async def _prices_at(self, symbols: set[str], at: datetime) -> dict[str, Decimal]:
        """Each symbol's price at the instant `at` (R2), keyed by symbol."""
        assets = await self._assets.by_symbol()
        ids = {assets[symbol].id: symbol for symbol in symbols if symbol in assets}
        found = await self._hourly.prices_at(
            asset_ids=sorted(ids), quote_currency=QUOTE, at=at, max_age=PRICE_MAX_AGE
        )
        return {ids[asset_id]: price for asset_id, price in found.items()}


def build_portfolio_change_service(
    session: AsyncSession, *, clock: Callable[[], datetime] = utc_now
) -> PortfolioChangeService:
    """Assemble the read-side service over one database session, the summary included."""
    return PortfolioChangeService(
        summary=build_portfolio_service(session, clock=clock),
        wallets=WalletRepository(session),
        balances=BalanceRepository(session),
        rebuilt=ReconstructedBalanceRepository(session),
        hourly=PriceHourlyRepository(session),
        assets=AssetRepository(session),
        clock=clock,
    )
