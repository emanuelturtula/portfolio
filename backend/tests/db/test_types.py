"""`UtcDateTime`: criterion 5, from both ends.

The bind and result processors are exercised directly, because that is where the
rejections live and a rejection wrapped in a `StatementError` says less than the
exception it wrapped. They are then exercised through a real column on a real file, so
that a type which is correct in isolation but not actually attached to `created_at`
cannot pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.exc import StatementError

from portfolio.db import models
from portfolio.db.engine import create_session_factory
from portfolio.db.types import UtcDateTime

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

DIALECT: Final = sqlite_dialect()
COLUMN_TYPE: Final = UtcDateTime()

# A placeholder, not a credential: nothing in this change hashes a password. The column
# is `NOT NULL`, so a row needs something in it.
PLACEHOLDER_USER_DIGEST = "not-a-real-argon2-encoded-hash"

BUENOS_AIRES = timezone(timedelta(hours=-3))
# The same instant, written two ways.
AWARE_UTC = datetime(2026, 7, 4, 15, 30, 45, tzinfo=UTC)
AWARE_OFFSET = datetime(2026, 7, 4, 12, 30, 45, tzinfo=BUENOS_AIRES)
NAIVE = datetime(2026, 7, 4, 15, 30, 45)  # noqa: DTZ001 -- the value under test


async def test_utcdatetime_round_trips_as_utc(migrated_engine: AsyncEngine) -> None:
    """An aware UTC value comes back as the same instant, still aware."""
    factory = create_session_factory(migrated_engine)

    async with factory() as session:
        session.add(
            models.User(
                username="owner", password_hash=PLACEHOLDER_USER_DIGEST, created_at=AWARE_UTC
            )
        )
        await session.commit()

    async with factory() as session:
        stored = await session.scalar(select(models.User.created_at))

    assert stored == AWARE_UTC
    assert stored is not None
    assert stored.tzinfo is not None
    assert stored.utcoffset() == timedelta(0)


async def test_utcdatetime_normalizes_a_non_utc_offset(migrated_engine: AsyncEngine) -> None:
    """An offset datetime is converted, not stored verbatim and reinterpreted."""
    factory = create_session_factory(migrated_engine)

    async with factory() as session:
        session.add(
            models.User(
                username="owner", password_hash=PLACEHOLDER_USER_DIGEST, created_at=AWARE_OFFSET
            )
        )
        await session.commit()

    async with factory() as session:
        stored = await session.scalar(select(models.User.created_at))

    assert stored is not None
    assert stored.utcoffset() == timedelta(0)
    assert stored == AWARE_OFFSET
    assert stored == AWARE_UTC
    # The wall clock was rewritten, not just relabelled.
    assert stored.hour == AWARE_UTC.hour


def test_utcdatetime_rejects_a_naive_datetime() -> None:
    """A naive value is a bug, not something to guess a timezone for."""
    with pytest.raises(ValueError, match="requires a timezone-aware datetime"):
        COLUMN_TYPE.process_bind_param(NAIVE, DIALECT)


def test_utcdatetime_rejects_a_non_datetime() -> None:
    """Accepting the value long enough to look at it is the point of the type."""
    with pytest.raises(TypeError, match="requires a datetime, got str"):
        COLUMN_TYPE.process_bind_param("2026-07-04T15:30:45Z", DIALECT)


async def test_a_naive_datetime_is_rejected_on_insert(migrated_engine: AsyncEngine) -> None:
    """The type is actually attached to the column, not merely defined."""
    factory = create_session_factory(migrated_engine)

    async with factory() as session:
        session.add(
            models.User(username="owner", password_hash=PLACEHOLDER_USER_DIGEST, created_at=NAIVE)
        )
        with pytest.raises(StatementError, match="requires a timezone-aware datetime"):
            await session.commit()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (AWARE_UTC, AWARE_UTC),
        (AWARE_OFFSET, AWARE_UTC),
        (None, None),
    ],
)
def test_bind_processing(value: datetime | None, expected: datetime | None) -> None:
    assert COLUMN_TYPE.process_bind_param(value, DIALECT) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # SQLite stores no offset, so the driver always hands back a naive value.
        (NAIVE, AWARE_UTC),
        (AWARE_UTC, AWARE_UTC),
        (AWARE_OFFSET, AWARE_UTC),
        (None, None),
    ],
)
def test_result_processing(value: datetime | None, expected: datetime | None) -> None:
    result = COLUMN_TYPE.process_result_value(value, DIALECT)

    assert result == expected
    if expected is not None:
        assert result is not None
        assert result.utcoffset() == timedelta(0)


def test_the_type_is_cacheable() -> None:
    """Without `cache_ok` SQLAlchemy warns and refuses to cache every statement."""
    assert UtcDateTime.cache_ok is True
