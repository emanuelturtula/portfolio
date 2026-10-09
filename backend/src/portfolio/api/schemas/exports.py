"""Response model for the monthly export reminder (spec 040)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from portfolio.services.export_reminders import ExportReminder  # noqa: TC001

__all__ = ["MONTH_PATTERN", "ExportReminderResponse"]

MONTH_PATTERN = r"^\d{4}-\d{2}$"
"""A month on the wire: `YYYY-MM`, the calendar month in Argentina time."""

Month = Annotated[str, Field(pattern=MONTH_PATTERN, examples=["2026-09"])]


class ExportReminderResponse(BaseModel):
    """The months whose exports are still owed, oldest first, and the exchanges to export.

    An empty `months` means nothing is owed and the dashboard shows no reminder.
    """

    months: list[Month]
    exchanges: list[str]

    @classmethod
    def of(cls, reminder: ExportReminder) -> ExportReminderResponse:
        """Render the service's reminder."""
        return cls(
            months=[f"{month:%Y-%m}" for month in reminder.pending],
            exchanges=list(reminder.exchanges),
        )
