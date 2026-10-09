"""The monthly reminder to export each exchange's transactions by hand (spec 040).

`ExportReminderService` answers which closed months the owner has not marked done, and marks
one. The rule for which months are owed is `domain.export_reminders`; this module supplies
the clock and the stored marks, and owns the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from portfolio.domain.export_reminders import (
    EXPORT_EXCHANGES,
    pending_months,
    require_closed,
)
from portfolio.repositories.export_months import ExportMonthRepository
from portfolio.services.prices import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date, datetime

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["ExportReminder", "ExportReminderService", "build_export_reminder_service"]


@dataclass(frozen=True, slots=True)
class ExportReminder:
    """What the dashboard shows: the months still owed, oldest first, and whose exports."""

    pending: tuple[date, ...]
    exchanges: tuple[str, ...]


class ExportReminderService:
    """The unit of work for the export reminder.

    The repository flushes and this class commits. The caller owns the session and closes it.
    The clock is injected so a test can name "now".
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        months: ExportMonthRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._months = months
        self._clock = clock

    async def reminder(self, user_id: int) -> ExportReminder:
        """The closed months this owner has not marked done."""
        done = await self._months.done_months(user_id)
        return ExportReminder(
            pending=tuple(pending_months(self._clock(), done)),
            exchanges=EXPORT_EXCHANGES,
        )

    async def mark_done(self, user_id: int, month: date) -> ExportReminder:
        """Mark `month` done and answer what is still owed. Marking it again changes nothing.

        Raises:
            MonthNotRemindedError: `month` is before the first reminded month.
            MonthNotClosedError: `month` has not ended yet in Argentina time.
        """
        now = self._clock()
        require_closed(month, now)
        await self._months.mark_done(user_id, month, at=now)
        await self._session.commit()
        return await self.reminder(user_id)


def build_export_reminder_service(
    session: AsyncSession,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> ExportReminderService:
    """Assemble the service over one database session."""
    return ExportReminderService(
        session=session, months=ExportMonthRepository(session), clock=clock
    )
