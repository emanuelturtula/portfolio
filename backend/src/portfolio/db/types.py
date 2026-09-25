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
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Final

from sqlalchemy import BigInteger, DateTime, Text
from sqlalchemy.types import TypeDecorator

from portfolio.domain.money import MONEY_PRECISION, quantize, require_amount

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

    `scale` is required, has no default, and is validated at construction. A money column
    without a declared scale has no defined rounding, and a default would let one be
    omitted by accident rather than by decision.

    A column holds `MONEY_PRECISION - scale` digits before the decimal point: at
    `scale=20` the largest storable amount is just under 10**18. Beyond that, binding
    raises rather than storing a rounded amount, because the digits that would be lost
    are the ones in front.

    **The three over-precision rules here are deliberately different, and a reader meets
    them side by side.** Too many digits *after* the point is rounded away, because that
    is what declaring a scale means and what `DECIMAL(p, s)` does everywhere else. Too
    many digits *before* it is refused, because there is no rounding that preserves the
    amount. And a non-zero amount that rounds away **to zero** is refused as well, because
    that is not rounding at all -- it is the amount being replaced by the one value nobody
    downstream can recognise as wrong. `domain.money.to_base_units` refuses in both of the
    first two directions instead -- a chain balance is exact and rounding one would report
    a holding the chain disagrees with -- so the same value can be legal in a column and
    rejected by a conversion.

    **Neither refusal quotes the amount.** Both name the scale and the rule, and the
    too-large one adds the digit ceiling, which is what a reader needs to act on. This type
    held only prices until #12, which is public market data, and the too-large message
    quoted the value; `exchange_fills` puts a *quantity* in it -- a fill is the owner's
    holdings -- so that message now follows the rule the rest of this application already
    did, and the one the rounds-to-nothing refusal was written to from the start. The
    change waited for the first column that could hold a quantity rather than being made
    under an unrelated issue.

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
        """Declare the number of decimal places this column rounds to and stores.

        Validated here so a bad scale is an error when the module is imported, rather
        than a column that quietly stores the wrong number. `NumericText(-2)`, a
        plausible typo for `NumericText(2)`, otherwise binds `Decimal("12345.67")` to
        `"12300"` -- a legal-looking amount, silently missing 45.67 -- and
        `NumericText(38)`, conflating the precision with the scale, builds a column that
        accepts `Decimal("0.5")` and then raises on `Decimal("1.5")`.

        Raises:
            TypeError: `scale` is not an `int`, or is a `bool`.
            ValueError: `scale` is negative or exceeds `MONEY_PRECISION`.
        """
        super().__init__()
        if isinstance(scale, bool) or not isinstance(scale, int):
            # A float scale, in the type whose reason for existing is banning floats,
            # otherwise constructs fine and dies at bind time with `exponent must be an
            # integer` and no mention of the column.
            message = f"NumericText requires an int scale, got {type(scale).__name__}"
            raise TypeError(message)
        if not 0 <= scale <= MONEY_PRECISION:
            message = f"NumericText requires a scale between 0 and {MONEY_PRECISION}, got {scale}"
            raise ValueError(message)
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
        candidate = require_amount(value, subject="NumericText")
        try:
            amount = quantize(candidate, self.scale)
        except InvalidOperation:
            # The bare `decimal.InvalidOperation: [<class 'decimal.InvalidOperation'>]`
            # names no value, no column and no reason, and SQLAlchemy wraps it in a
            # StatementError at INSERT time. This is the failure a money column actually
            # hits -- an amount too large for the digits the scale leaves in front of the
            # point -- so it says which scale and what the ceiling is. **Never which
            # value**: `exchange_fills` stores quantities in this type, and a quantity in a
            # traceback is the owner's holdings in a log.
            integer_digits = MONEY_PRECISION - self.scale
            message = (
                f"NumericText cannot store an amount this large: a scale of {self.scale} leaves "
                f"{integer_digits} digits before the decimal point, out of the "
                f"{MONEY_PRECISION} this application represents"
            )
            raise ValueError(message) from None
        if amount.is_zero():
            if not candidate.is_zero():
                # **A non-zero amount that rounds to nothing is destroyed, not rounded**,
                # and this is the one over-precision case that must not be absorbed
                # quietly. The two rules above it are survivable: digits lost *after* the
                # point are digits the scale said were not worth keeping, and digits lost
                # *before* it are refused outright. This case looks like the first and
                # behaves like the second -- every significant digit is gone and what
                # remains is a legal-looking zero.
                #
                # Zero is the worst possible replacement value, because it is believed. A
                # price that rounds away values every holding of that asset at nothing,
                # and the total computed from it is complete, confident and wrong -- there
                # is no missing row for a valuation to notice and no reason for it to
                # report. That is the exact failure `services/prices.py` is shaped around,
                # arriving through the column instead of through an absent row.
                #
                # Refused here rather than in the prices repository because it is a
                # property of the column: a fee, a fill or a cost basis added later meets
                # the same boundary, and a guard living beside one caller protects one
                # caller.
                message = (
                    f"NumericText cannot store an amount finer than its scale of "
                    f"{self.scale}: the value is not zero and rounding it to "
                    f"{self.scale} decimal places leaves nothing of it"
                )
                raise ValueError(message)
            # A genuine zero, normalised. Otherwise a column holds two spellings of the
            # same amount: `-0.00` and `0.00` are equal as Decimals and different as the
            # text SQLite compares. `Decimal("0")` still binds, which a future money
            # column that legitimately holds one depends on.
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

    def process_result_value(self, value: object, dialect: Dialect) -> int | None:
        """Refuse a stored value that is not an integer, rather than returning a float.

        Guarding the bind side only assumes every row got here through the ORM. SQLite
        has no column type enforcement: a row written by an Alembic `op.execute` backfill
        or by `sqlite3` on the Pi as `1.5` is stored with `typeof` = `real` and comes back
        as a Python `float` from an attribute annotated `Mapped[int]`. Nothing downstream
        would notice, and the float ban would have been defeated by the one path that
        never passes through the code the AST test reads.

        `NumericText` needs no equivalent guard only because TEXT affinity coerces the
        value on the way in -- that protection is SQLite's, not this module's, so it is
        not one to rely on here.
        """
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            message = (
                f"BaseUnits read {value!r} ({type(value).__name__}) from the database, "
                f"which is not an integer quantity: the row was not written through this type"
            )
            raise TypeError(message)
        return value
