"""Database copies: what one is called, which ones to keep, and how an attempt can fail.

Pure, like the rest of `domain`: nothing here reads a directory, a file or the clock. The
code that does (`db/backup.py`, `services/backup.py`) hands in names and instants and acts on
what comes back, which is what lets the rotation rule be tested with a list of literals rather
than a directory of files (spec 029).

## A copy's name is its only record

`portfolio-YYYYMMDDTHHMMSSffffffZ.sqlite3`: the UTC instant the copy was **started**, to the
microsecond, so two copies never share a name. No table records when a copy was taken -- a
history of attempts in the database was ruled out of scope -- so the name is the one place
the instant lives, and `instant_of` is the one parser of it.

**Only a name `instant_of` accepts is ever listed, rotated or restored.** Anything else in the
backup directory -- an operator's own file, a copy renamed by hand, a name with a month 13 --
is not a copy as far as this application is concerned, and is never deleted.

## Rotation keeps the most recent days *that have a copy*

`backups_to_keep` keeps **every** copy on the `keep_daily` most recent UTC dates that have
one, and the newest of each of the `keep_weekly` most recent ISO weeks that have one.

"Most recent dates that have a copy" rather than "the last N calendar days": after a pause in
backups -- a host switched off for a fortnight, a full disk -- a rule counted in calendar days
would find nothing inside its window and empty the daily set on the first copy after the
pause. Counted in days that have a copy, a pause costs nothing that was kept.

**Every copy on those days, not the newest of each** (ruling R5 of spec 029). Keeping one per
day lost the copy that mattered: an owner who notices a bad import at noon and takes a copy
"to be safe" would have had rotation delete that day's earlier copy -- the one from before
the import -- and a restore's safety copy would have gone the same way one rotation later.
The copies beyond the scheduled one are taken by hand or by a restore, so they stay few.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "BACKUP_NAME_PATTERN",
    "BackupErrorKind",
    "backup_name",
    "backups_to_keep",
    "instant_of",
    "utc_stamp",
]

BACKUP_NAME_PATTERN: Final = re.compile(
    r"\Aportfolio-(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(\d{6})Z\.sqlite3\Z"
)
"""The whole name of a copy, and nothing else: `\\A` and `\\Z` rather than `^` and `$`, which
would let a trailing newline through."""

_STAMP_FORMAT: Final = "%Y%m%dT%H%M%S%fZ"


# Here rather than beside the code that raises it, because three layers name it: `db` raises
# it, the service records it, and the API serves it as `last_error_kind`. `db` sits above
# `domain`, so this is the one module all three may import. The docstring below is served in
# the OpenAPI document, so it describes the values and nothing of the layering.
class BackupErrorKind(StrEnum):
    """Why an attempt to take a copy of the database failed.

    * `database_error` -- the live database could not be opened or read.
    * `integrity_failed` -- the copy did not pass `PRAGMA integrity_check`, or does not hold
      exactly one schema revision. It was not kept.
    * `storage_error` -- writing, syncing, renaming or removing a file in the backup directory
      failed: a full disk, or a directory the application may not write to.
    """

    DATABASE_ERROR = "database_error"
    INTEGRITY_FAILED = "integrity_failed"
    STORAGE_ERROR = "storage_error"


def utc_stamp(instant: datetime) -> str:
    """`instant` in UTC as `YYYYMMDDTHHMMSSffffffZ`: the part of a copy's name that is a time.

    The one spelling of an instant in a file name, shared by a copy, its temporary file and a
    damaged database moved aside by a restore, so that all three sort and read the same way.

    Raises:
        ValueError: `instant` is naive. Converting a naive value would read the host's time
            zone, and a name in local time sorts wrongly against one in UTC.
    """
    return _require_aware(instant).astimezone(UTC).strftime(_STAMP_FORMAT)


def backup_name(started_at: datetime) -> str:
    """The name of a copy started at `started_at`, which must be timezone-aware.

    Converted to UTC first, so an instant given in any offset names the same file.

    Raises:
        ValueError: `started_at` is naive, as `utc_stamp` explains.
    """
    return f"portfolio-{utc_stamp(started_at)}.sqlite3"


def instant_of(name: str) -> datetime | None:
    """The UTC instant a copy's name records, or `None` if `name` is not a copy's name.

    `None` both for a name that does not match the pattern and for one that does but names no
    real instant, such as a 13th month: either way it is not a file this application wrote,
    and so not one it may rotate or restore.
    """
    match = BACKUP_NAME_PATTERN.match(name)
    if match is None:
        return None
    year, month, day, hour, minute, second, microsecond = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=UTC)
    except ValueError:
        return None


def backups_to_keep(
    instants: Iterable[datetime],
    *,
    keep_daily: int,
    keep_weekly: int,
) -> frozenset[datetime]:
    """The copies rotation keeps, as their instants. Every other copy is deleted.

    The union of two sets:

    * **daily**: the instants grouped by UTC calendar date; the `keep_daily` most recent dates,
      and **every** copy on them;
    * **weekly**: grouped by ISO year and week (`isocalendar`, so the week of 31 December can
      belong to the next year, as ISO 8601 says); the `keep_weekly` most recent, and the newest
      copy of each.

    The newest copy overall is always in the daily set, because `keep_daily` is at least one,
    so the copy just taken is never the one rotation deletes.

    Raises:
        ValueError: `keep_daily` is below one, `keep_weekly` below zero, or an instant is
            naive. The settings refuse the first two at startup; this repeats the rule where
            it matters, because a `keep_daily` of zero would delete the newest copy.
    """
    if keep_daily < 1:
        message = f"keep_daily must be at least 1, got {keep_daily}"
        raise ValueError(message)
    if keep_weekly < 0:
        message = f"keep_weekly must be at least 0, got {keep_weekly}"
        raise ValueError(message)
    in_utc = [_require_aware(instant).astimezone(UTC) for instant in instants]
    newest_by_week: dict[tuple[int, int], datetime] = {}
    for instant in in_utc:
        iso = instant.isocalendar()
        week = (iso.year, iso.week)
        newest_by_week[week] = max(instant, newest_by_week.get(week, instant))
    dates: list[date] = sorted({instant.date() for instant in in_utc}, reverse=True)
    kept_dates = frozenset(dates[:keep_daily])
    daily = [instant for instant in in_utc if instant.date() in kept_dates]
    weekly = [newest_by_week[week] for week in sorted(newest_by_week, reverse=True)[:keep_weekly]]
    return frozenset(daily) | frozenset(weekly)


def _require_aware(instant: datetime) -> datetime:
    """`instant` itself, if it is timezone-aware. A naive value has no instant to name."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        message = "a copy's instant must be timezone-aware"
        raise ValueError(message)
    return instant
