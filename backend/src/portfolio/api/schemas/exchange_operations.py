"""Request and response models for `/api/exchange-operations` and `/api/investment` (spec 042).

Every amount and quantity is a `MoneyStr`: a JSON string in both directions, for the reason
`api/schemas/money.py` gives. The client sums nothing: every figure the dashboard shows is here.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from portfolio.api.schemas.money import MoneyStr
from portfolio.domain.exchange_exports import ExportFormat, OperationKind
from portfolio.domain.investment import InvestmentUnavailable

if TYPE_CHECKING:
    from portfolio.domain.investment import AssetInvestment, Investment
    from portfolio.services.exchange_operations import (
        FileReport,
        ImportReport,
        OperationPage,
        OperationView,
    )

__all__ = [
    "AssetInvestmentResponse",
    "ImportFileResponse",
    "ImportRequest",
    "ImportResponse",
    "InvestedOnDayResponse",
    "InvestmentResponse",
    "ManualOperationRequest",
    "OperationListResponse",
    "OperationResponse",
    "TotalInvestmentResponse",
]

MAX_FILENAME_LENGTH: Final = 255
MAX_TEXT_LENGTH: Final = 200
MAX_SYMBOL_LENGTH: Final = 20


class ImportRequest(BaseModel):
    """One file as the exchange served it, a CSV or a zip of them, in base64 (R12)."""

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=MAX_FILENAME_LENGTH)
    content_base64: str = Field(description="The file's bytes, base64-encoded; at most 5 MiB.")


class ImportFileResponse(BaseModel):
    """One file of an upload: its format, its data rows, and what of it was new.

    `format` is `null` exactly when the file was skipped, and `skipped_reason` says why.
    """

    name: str
    format: ExportFormat | None
    rows: int
    stored: int
    already_stored: int
    skipped_reason: str | None

    @classmethod
    def of(cls, report: FileReport) -> ImportFileResponse:
        """Render one file's report."""
        return cls(
            name=report.name,
            format=report.format,
            rows=report.rows,
            stored=report.stored,
            already_stored=report.already_stored,
            skipped_reason=report.skipped_reason,
        )


class ImportResponse(BaseModel):
    """What an upload stored, file by file, and in all."""

    filename: str
    files: list[ImportFileResponse]
    stored: int
    already_stored: int

    @classmethod
    def of(cls, report: ImportReport) -> ImportResponse:
        """Render an upload's report."""
        return cls(
            filename=report.filename,
            files=[ImportFileResponse.of(file) for file in report.files],
            stored=report.stored,
            already_stored=report.already_stored,
        )


class OperationResponse(BaseModel):
    """One stored operation. `manual` says whether it can be deleted."""

    id: int
    source: str
    venue: str
    external_id: str
    executed_at: datetime
    kind: OperationKind
    asset: str
    quantity: MoneyStr
    quote_currency: str | None
    quote_amount: MoneyStr | None
    fee_asset: str | None
    fee_amount: MoneyStr | None
    description: str
    manual: bool

    @classmethod
    def of(cls, view: OperationView) -> OperationResponse:
        """Render one operation."""
        return cls(
            id=view.id,
            source=view.source,
            venue=view.venue,
            external_id=view.external_id,
            executed_at=view.executed_at,
            kind=OperationKind(view.kind),
            asset=view.asset,
            quantity=view.quantity,
            quote_currency=view.quote_currency,
            quote_amount=view.quote_amount,
            fee_asset=view.fee_asset,
            fee_amount=view.fee_amount,
            description=view.description,
            manual=view.manual,
        )


class OperationListResponse(BaseModel):
    """One page of operations, newest first, and how many the filter keeps.

    `count` rather than `total`, which is a money property everywhere else in the schema.
    """

    count: int
    operations: list[OperationResponse]
    assets: list[str] = Field(
        description="Every asset with a stored operation, alphabetically, whatever the filter."
    )
    venues: list[str] = Field(
        description="Every venue with a stored operation, alphabetically, whatever the filter."
    )

    @classmethod
    def of(cls, page: OperationPage) -> OperationListResponse:
        """Render a page."""
        return cls(
            count=page.total,
            operations=[OperationResponse.of(view) for view in page.operations],
            assets=list(page.assets),
            venues=list(page.venues),
        )


class ManualOperationRequest(BaseModel):
    """An operation the exports do not cover (R11): a swap in a wallet app, a miner's payout,
    or a network fee a withdrawal paid that the export did not list (spec 043).

    A `buy` or `sell` says what it cost or brought in: `quantity` of `asset` for `quote_amount`
    of `quote_currency`, before any fee. A `reward` or a `fee` is only `quantity` of `asset`,
    and is refused with a counterpart or a fee of its own. A symbol is stored upper-cased.
    """

    model_config = ConfigDict(extra="forbid")

    venue: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    executed_at: AwareDatetime
    kind: Literal[OperationKind.BUY, OperationKind.SELL, OperationKind.REWARD, OperationKind.FEE]
    asset: str = Field(min_length=1, max_length=MAX_SYMBOL_LENGTH)
    quantity: MoneyStr = Field(gt=0)
    quote_currency: str | None = Field(default=None, min_length=1, max_length=MAX_SYMBOL_LENGTH)
    quote_amount: MoneyStr | None = Field(default=None, ge=0)
    fee_asset: str | None = Field(default=None, min_length=1, max_length=MAX_SYMBOL_LENGTH)
    fee_amount: MoneyStr | None = Field(default=None, gt=0)
    description: str = Field(default="", max_length=MAX_TEXT_LENGTH)

    @model_validator(mode="after")
    def _counterpart_matches_kind(self) -> ManualOperationRequest:
        trade = self.kind in {OperationKind.BUY, OperationKind.SELL}
        quoted = (self.quote_currency, self.quote_amount)
        if trade and None in quoted:
            message = "a buy or a sell needs quote_currency and quote_amount"
            raise ValueError(message)
        if not trade and quoted != (None, None):
            message = "a reward or a fee has no quote_currency or quote_amount"
            raise ValueError(message)
        if not trade and (self.fee_asset, self.fee_amount) != (None, None):
            message = "a reward or a fee has no fee of its own"
            raise ValueError(message)
        return self


class AssetInvestmentResponse(BaseModel):
    """One tracked asset's figures. A figure that cannot be known is `null`, never `"0"`, and
    `unavailable` says why the profit or its percentage is missing."""

    asset: str
    invested: MoneyStr | None
    value: MoneyStr | None
    pnl: MoneyStr | None
    pnl_pct: MoneyStr | None
    held: MoneyStr | None
    explained: MoneyStr
    difference: MoneyStr | None
    trades: int
    unvalued_trades: int
    unavailable: InvestmentUnavailable | None

    @classmethod
    def of(cls, asset: AssetInvestment) -> AssetInvestmentResponse:
        """Render one asset."""
        return cls(
            asset=asset.asset,
            invested=asset.invested,
            value=asset.value,
            pnl=asset.pnl,
            pnl_pct=asset.pnl_pct,
            held=asset.held,
            explained=asset.explained,
            difference=asset.difference,
            trades=asset.trades,
            unvalued_trades=asset.unvalued_trades,
            unavailable=asset.unavailable,
        )


class TotalInvestmentResponse(BaseModel):
    """The tracked assets together, unknown where any of them is."""

    invested: MoneyStr | None
    value: MoneyStr | None
    pnl: MoneyStr | None
    pnl_pct: MoneyStr | None
    unavailable: InvestmentUnavailable | None


class InvestedOnDayResponse(BaseModel):
    """The total invested at the end of a UTC day a trade changed it; `null` once unknown."""

    day: date
    invested: MoneyStr | None


class InvestmentResponse(BaseModel):
    """What went in, what it is worth now, and whether the exchanges explain the wallets.

    `overall` rather than `total`, which is a money property everywhere else in the schema.
    """

    assets: list[AssetInvestmentResponse]
    overall: TotalInvestmentResponse
    invested_by_day: list[InvestedOnDayResponse]

    @classmethod
    def of(cls, investment: Investment) -> InvestmentResponse:
        """Render the figures."""
        total = investment.total
        return cls(
            assets=[AssetInvestmentResponse.of(asset) for asset in investment.assets],
            overall=TotalInvestmentResponse(
                invested=total.invested,
                value=total.value,
                pnl=total.pnl,
                pnl_pct=total.pnl_pct,
                unavailable=total.unavailable,
            ),
            invested_by_day=[
                InvestedOnDayResponse(day=entry.day, invested=entry.invested)
                for entry in investment.invested_by_day
            ],
        )
