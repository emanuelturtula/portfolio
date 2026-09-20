"""Column types that keep the database honest about things Python is loose about.

A portfolio is a time-ordered event log. A naive datetime in it does not raise; it simply
records the wrong instant, and the error only surfaces months later as a trade that sorts
before the deposit that funded it. Ruff's `DTZ` rules stop a naive value being *produced*
by a clock call; `UtcDateTime` stops one being *persisted*, whatever its origin -- parsed
JSON from an exchange, a query string, or a fixture.

Money is the same argument with more money at stake. `sqlalchemy.Numeric` on SQLite
round-trips every value through a C `double`, so the one obvious type is the one that
silently destroys precision; `NumericText` stores a canonical fixed-point string instead,
and `BaseUnits` stores an on-chain quantity as the integer of indivisible units it
actually is. Both refuse a `float` at the boundary rather than converting it, because
after the conversion there is nothing left to see.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from sqlalchemy import BigInteger, DateTime, Text
from sqlalchemy.types import TypeDecorator

from portfolio.domain.money import quantize, require_amount

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import Dialect

# What SQLite's INTEGER storage class actually holds.
_INT64_MIN: Final[int] = -(2**63)
_INT64_MAX: Final[int] = 2**63 - 1


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


class NumericText(TypeDecorator[Decimal]):
    """A `Decimal` stored as a canonical fixed-point string in a `TEXT` column.

    Every value in a column shares one shape: exactly `scale` decimal places, never
    scientific notation, one spelling of zero. That is what makes the stored form
    comparable by eye and in a diff, and it is why `format(value, "f")` is used rather
    than `str(value)` -- `str(Decimal("1E+2"))` is `"1E+2"`.

    `scale` is required and has no default. A money column without a declared scale has
    no defined rounding, and a default would let one be omitted by accident rather than
    by decision.

    Text does not sort or sum numerically, and that is a feature, not a limitation to
    work around: `SUM()`, `ORDER BY` and `<` on this column would coerce it to a float in
    SQLite, so money is aggregated in Python instead. See `docs/architecture.md`.
    """

    impl = Text
    # Verified, not assumed. SQLAlchemy builds a parameterized type's cache key from the
    # names of its `__init__` arguments, reading each one off the instance as an
    # attribute -- so `self.scale` is what actually puts the scale in the key, and
    # storing it under any other name would silently yield `(NumericText,)` for every
    # scale, one cache entry shared by columns that round to different places. Observed
    # here: `NumericText(8)._static_cache_key` is `(NumericText, ("scale", 8))` and
    # differs from `NumericText(18)`'s.
    cache_ok = True

    def __init__(self, scale: int) -> None:
        """Declare the number of decimal places this column rounds to and stores."""
        super().__init__()
        self.scale = scale

    # `object` rather than `Decimal | None`: refusing a value that is not a Decimal
    # requires accepting it long enough to look at it.
    def process_bind_param(self, value: object, dialect: Dialect) -> str | None:
        """Render an amount as fixed-point text, refusing anything that is not one."""
        if value is None:
            return None
        if isinstance(value, bool):
            # `bool` is an `int` subclass, so without this branch `True` would sail
            # through the one below and be stored as a perfectly valid `1.00000000`.
            message = f"NumericText requires a Decimal, got {type(value).__name__}"
            raise TypeError(message)
        if isinstance(value, int):
            # Exact by construction: an `int` has nothing after the decimal point to
            # lose. This is the one non-Decimal input worth accepting.
            value = Decimal(value)
        amount = quantize(require_amount(value, subject="NumericText"), self.scale)
        if amount.is_zero():
            # Otherwise a column holds two spellings of the same amount: `-0.00` and
            # `0.00` are equal as Decimals and different as the text SQLite compares.
            amount = abs(amount)
        return format(amount, "f")

    def process_result_value(self, value: str | None, dialect: Dialect) -> Decimal | None:
        """Read the stored text back as the exact `Decimal` that was written."""
        if value is None:
            return None
        return Decimal(value)


class BaseUnits(TypeDecorator[int]):
    """An on-chain quantity, stored as the integer count of indivisible units.

    A satoshi and a sompi cannot be subdivided and every chain API reports them as
    integers, so there is nothing to round and no reason to store text. `assets.decimals`
    is the exponent these integers are read with: 8 means the value counts
    hundred-millionths.

    The ceiling is SQLite's own: a signed 64-bit integer. Confirmed comfortable for V1 --
    the whole 21M BTC supply is 2_100_000_000_000_000 satoshis and Kaspa's is about
    2_870_000_000_000_000_000 sompi. Assumed, not verified, for anything V1 does not
    support: an 18-decimal EVM token would overflow this, so a chain like that needs a
    different representation. Recorded here so the next person meets the limit as a
    rejected value rather than as silent truncation.
    """

    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value: object, dialect: Dialect) -> int | None:
        """Accept an integer quantity within what SQLite can hold, and nothing else."""
        if value is None:
            return None
        # `bool` first, for the same reason as in `NumericText`: `isinstance(True, int)`
        # is `True`, and `True` is not a quantity.
        if isinstance(value, bool) or not isinstance(value, int):
            message = f"BaseUnits requires an int, got {type(value).__name__}"
            raise TypeError(message)
        if not _INT64_MIN <= value <= _INT64_MAX:
            message = f"BaseUnits cannot store {value}: outside the signed 64-bit range"
            raise ValueError(message)
        return value
