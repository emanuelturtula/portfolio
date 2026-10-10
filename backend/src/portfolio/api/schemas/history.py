"""Response models for the value-history chart (spec 037).

Every amount and quantity is a `MoneyStr`, for the reason `api/schemas/money.py` gives, and
a day nothing can value is `null`, never `"0"`.
"""

from __future__ import annotations

from collections.abc import Mapping  # noqa: TC003
from datetime import date

from pydantic import BaseModel

from portfolio.api.schemas.money import MoneyStr
from portfolio.domain.portfolio_history import DayValue, WalletDay  # noqa: TC001
from portfolio.services.portfolio_history import (
    HistoryRange,
    PortfolioHistory,
    WalletValueHistory,
)

__all__ = [
    "PortfolioHistoryResponse",
    "PortfolioPointResponse",
    "WalletPointResponse",
    "WalletValueHistoryResponse",
]


class PortfolioPointResponse(BaseModel):
    """The wallets' value at the end of `day`, in USDT. `null` when it cannot be known.

    `assets` is each asset's own value that day, keyed by the response's `assets`, and `null`
    for an asset by the same rule (spec 041, R7).
    """

    day: date
    value: MoneyStr | None
    assets: dict[str, MoneyStr | None]

    @classmethod
    def of(cls, point: DayValue, assets: Mapping[str, DayValue]) -> PortfolioPointResponse:
        """Render one day, with each asset's point of the same day."""
        return cls(
            day=point.day,
            value=point.value,
            assets={asset: day.value for asset, day in assets.items()},
        )


class PortfolioHistoryResponse(BaseModel):
    """Every active wallet together, one point per day of `range`, oldest first.

    `assets` names the asset of every active wallet, sorted: the series a chart can draw
    beside the total.
    """

    range: HistoryRange
    assets: list[str]
    points: list[PortfolioPointResponse]

    @classmethod
    def of(cls, history: PortfolioHistory) -> PortfolioHistoryResponse:
        """Render the service's history."""
        assets = sorted(history.by_asset)
        return cls(
            range=history.history_range,
            assets=assets,
            points=[
                PortfolioPointResponse.of(
                    point, {asset: history.by_asset[asset][index] for asset in assets}
                )
                for index, point in enumerate(history.points)
            ],
        )


class WalletPointResponse(BaseModel):
    """One wallet at the end of `day`.

    `quantity` is `null` before the wallet's first reading; `value` is `null` then too, and
    when what it held has no price that day.
    """

    day: date
    quantity: MoneyStr | None
    value: MoneyStr | None

    @classmethod
    def of(cls, point: WalletDay) -> WalletPointResponse:
        """Render one day."""
        return cls(day=point.day, quantity=point.quantity, value=point.value)


class WalletValueHistoryResponse(BaseModel):
    """One wallet, one point per day of `range`, oldest first."""

    wallet_id: int
    asset: str
    range: HistoryRange
    points: list[WalletPointResponse]

    @classmethod
    def of(cls, history: WalletValueHistory) -> WalletValueHistoryResponse:
        """Render the service's history."""
        return cls(
            wallet_id=history.wallet_id,
            asset=history.asset,
            range=history.history_range,
            points=[WalletPointResponse.of(point) for point in history.points],
        )
