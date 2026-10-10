"""What went in, what it is worth, and whether the exchanges explain the wallets (spec 042).

`InvestmentService.investment` reads the stored operations and the dashboard's summary, and
hands the arithmetic to `domain.investment`. The value and the quantity held are the
summary's own, so the dashboard and this figure cannot disagree (R9).

## What is tracked

The assets of the owner's active wallets (R6). A wallet never read leaves its asset's held
quantity unknown rather than zero, and with it the value and the difference.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from portfolio.domain.chains import ChainKey
from portfolio.domain.exchange_exports import OperationKind
from portfolio.domain.investment import Holding, Investment, Movement, summarize_investment
from portfolio.repositories.exchange_operations import ExchangeOperationRepository
from portfolio.repositories.wallets import WalletRepository
from portfolio.services.portfolio import MissingKind, PortfolioService, build_portfolio_service

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["InvestmentService", "build_investment_service"]

_NOTHING: Final = Decimal(0)


def utc_now() -> datetime:
    """The production clock, aware and in UTC."""
    return datetime.now(UTC)


class InvestmentService:
    """Reads the operations and the summary, and figures them. Writes nothing."""

    def __init__(
        self,
        *,
        operations: ExchangeOperationRepository,
        wallets: WalletRepository,
        portfolio: PortfolioService,
    ) -> None:
        self._operations = operations
        self._wallets = wallets
        self._portfolio = portfolio

    async def investment(self, user_id: int) -> Investment:
        """Every tracked asset's invested, value, profit, held and explained quantities."""
        wallets = await self._wallets.list_for_user(user_id)
        tracked = sorted({ChainKey(wallet.chain_key).asset_symbol for wallet in wallets})
        view = await self._portfolio.summary(user_id)
        unread = {
            ChainKey(missing.subject).asset_symbol
            for missing in view.missing
            if missing.kind is MissingKind.WALLET_UNREAD
        }
        by_asset = {holding.asset: holding for holding in view.summary.holdings}

        holdings = []
        for asset in tracked:
            if asset in unread:
                holdings.append(Holding(asset, None, None))
                continue
            holding = by_asset.get(asset)
            if holding is None:
                holdings.append(Holding(asset, _NOTHING, _NOTHING))
            else:
                holdings.append(Holding(asset, holding.quantity, holding.value))

        rows = await self._operations.all_for(user_id)
        movements = [
            Movement(
                executed_at=row.executed_at,
                kind=OperationKind(row.kind),
                asset=row.asset,
                quantity=row.quantity,
                quote_currency=row.quote_currency,
                quote_amount=row.quote_amount,
                fee_asset=row.fee_asset,
                fee_amount=row.fee_amount,
            )
            for row in rows
        ]
        return summarize_investment(movements, holdings)


def build_investment_service(
    session: AsyncSession, *, clock: Callable[[], datetime] = utc_now
) -> InvestmentService:
    """Assemble the read-side service over one session. The clock is the summary's."""
    return InvestmentService(
        operations=ExchangeOperationRepository(session),
        wallets=WalletRepository(session),
        portfolio=build_portfolio_service(session, clock=clock),
    )
