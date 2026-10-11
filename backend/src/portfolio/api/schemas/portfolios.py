"""Request and response models for `/api/portfolios` (spec 044).

`wallets` and `operations` are counts, not money.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from portfolio.services.portfolios import MAX_NAME_LENGTH

if TYPE_CHECKING:
    from portfolio.services.portfolios import PortfolioView

__all__ = ["PortfolioListResponse", "PortfolioRequest", "PortfolioResponse"]


class PortfolioRequest(BaseModel):
    """A portfolio's name, to create one or to rename one. Trimmed, and unique per owner."""

    model_config = ConfigDict(extra="forbid")

    # The bound is checked again once trimmed, by the service.
    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH * 2)


class PortfolioResponse(BaseModel):
    """One portfolio, with how many wallets (archived ones included) and operations it holds:
    it can be deleted only when both are zero and it is not the last one."""

    id: int
    name: str
    wallets: int
    operations: int
    created_at: datetime

    @classmethod
    def of(cls, view: PortfolioView) -> PortfolioResponse:
        """Render a service view."""
        return cls(
            id=view.id,
            name=view.name,
            wallets=view.wallets,
            operations=view.operations,
            created_at=view.created_at,
        )


class PortfolioListResponse(BaseModel):
    """Every portfolio, oldest first: the first is where a write that names none goes."""

    portfolios: list[PortfolioResponse]
