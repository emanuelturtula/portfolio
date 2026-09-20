"""Column types that keep the database honest about things Python is loose about.

A portfolio is a time-ordered event log. A naive datetime in it does not raise; it simply
records the wrong instant, and the error only surfaces months later as a trade that sorts
before the deposit that funded it. Ruff's `DTZ` rules stop a naive value being *produced*
by a clock call; `UtcDateTime` stops one being *persisted*, whatever its origin -- parsed
JSON from an exchange, a query string, or a fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import Dialect


class UtcDateTime(TypeDecorator[datetime]):
    """A `DateTime` that only ever holds timezone-aware UTC.

    On the way in, a naive value is rejected rather than guessed at, and an aware value in
    any other offset is converted to UTC. On the way out, UTC is attached: SQLite stores no
    offset, so the driver always hands back a naive value, and without this every read
    would produce exactly the naive datetime the bind side refuses to accept.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    # `object` rather than `datetime | None`: the point of the type is to reject values
    # that are not datetimes at all, which requires accepting them long enough to look.
    def process_bind_param(self, value: object, dialect: Dialect) -> datetime | None:
        """Normalise a value to UTC, rejecting anything that cannot be placed in time."""
        if value is None:
            return None
        if not isinstance(value, datetime):
            message = f"UtcDateTime requires a datetime, got {type(value).__name__}"
            raise TypeError(message)
        if value.tzinfo is None or value.utcoffset() is None:
            message = f"UtcDateTime requires a timezone-aware datetime, got {value!r}"
            raise ValueError(message)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Attach UTC to the naive value SQLite hands back."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
