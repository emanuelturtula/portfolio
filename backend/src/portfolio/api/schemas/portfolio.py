"""Response models for `/api/portfolio`: the dashboard's summary (#154).

Every amount, quantity and percentage is a `MoneyStr`, for the reason `api/schemas/money.py`
gives. The client sums nothing: every figure the dashboard shows is here.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

from portfolio.api.schemas.money import MoneyStr
from portfolio.domain.portfolio import HoldingSummary  # noqa: TC001
from portfolio.domain.portfolio_change import Change, ChangePeriod, Unavailable
from portfolio.services.portfolio import (
    Missing,
    MissingKind,
    PortfolioSummaryView,
)

if TYPE_CHECKING:
    from portfolio.services.portfolio_changes import PortfolioChanges

__all__ = [
    "ChangeResponse",
    "HoldingResponse",
    "MissingResponse",
    "PortfolioChangesResponse",
    "PortfolioSummaryResponse",
]


class HoldingResponse(BaseModel):
    """One asset held: how much, its price, its value and its share of the total.

    `price`, `value` and `share_pct` are `null` together when nothing prices the asset, never
    a zero; `share_pct` alone is `null` when the total is zero.
    """

    asset: str
    quantity: MoneyStr
    price: MoneyStr | None
    value: MoneyStr | None
    share_pct: MoneyStr | None

    @classmethod
    def of(cls, holding: HoldingSummary) -> HoldingResponse:
        """Render one holding."""
        return cls(
            asset=holding.asset,
            quantity=holding.quantity,
            price=holding.price,
            value=holding.value,
            share_pct=holding.share_pct,
        )


class MissingResponse(BaseModel):
    """Something a figure could not include as current, and which chain or asset."""

    kind: MissingKind
    subject: str

    @classmethod
    def of(cls, missing: Missing) -> MissingResponse:
        """Render one entry."""
        return cls(kind=missing.kind, subject=missing.subject)


class PortfolioSummaryResponse(BaseModel):
    """The dashboard's figures, in USDT.

    * `total_value` -- every asset the wallets hold, at its cached price.
    * `holdings` -- largest value first, the unpriced ones last.
    * `missing` -- what the figures could not include, or include only as last read. Empty
      means they are whole and current.
    """

    total_value: MoneyStr
    holdings: list[HoldingResponse]
    missing: list[MissingResponse]

    @classmethod
    def of(cls, view: PortfolioSummaryView) -> PortfolioSummaryResponse:
        """Render the service's view."""
        summary = view.summary
        return cls(
            total_value=summary.total_value,
            holdings=[HoldingResponse.of(holding) for holding in summary.holdings],
            missing=[MissingResponse.of(entry) for entry in view.missing],
        )


class ChangeResponse(BaseModel):
    """The change over one period in USDT, or why there is none (spec 041).

    `change` and `change_pct` are `null` exactly when `unavailable` names a reason, and never
    `"0"` in its place; `change_pct` is also `null` when nothing was held at `since`.
    """

    period: ChangePeriod
    since: datetime
    value_then: MoneyStr | None
    change: MoneyStr | None
    change_pct: MoneyStr | None
    unavailable: Unavailable | None

    @classmethod
    def of(cls, change: Change) -> ChangeResponse:
        """Render one period."""
        return cls(
            period=change.period,
            since=change.since,
            value_then=change.value_then,
            change=change.change,
            change_pct=change.change_pct,
            unavailable=change.unavailable,
        )


class PortfolioChangesResponse(BaseModel):
    """The value now in USDT, `null` when unknown, and the change over 24 hours and 7 days."""

    as_of: datetime
    value: MoneyStr | None
    changes: list[ChangeResponse]

    @classmethod
    def of(cls, changes: PortfolioChanges) -> PortfolioChangesResponse:
        """Render the service's changes."""
        return cls(
            as_of=changes.as_of,
            value=changes.value,
            changes=[ChangeResponse.of(change) for change in changes.changes],
        )
