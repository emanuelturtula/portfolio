"""Reads and writes of the `prices` table.

Queries and nothing else: no clock, no staleness, no policy about what a missing row means.
The repository is handed an `AsyncSession` and it does not commit -- the service that opened
the unit of work decides when it ends.

## Nothing here aggregates, orders or compares money

`prices.amount` is a `TEXT` column holding a canonical fixed-point string, and `SUM()`,
`ORDER BY` and `<` on it all apply SQLite's numeric affinity -- which is the C double that
`NumericText` exists to keep money away from, applied to every row at once. So no method
here returns a total, a maximum or a sorted-by-price list, and none ever should. The
valuation service loads the rows and sums them in Python, over a table with four rows in it.

Ordering is by `asset_id`, an `INTEGER` primary key on the referenced table, which is the
seed order of `0002_seed_assets` and is stable between runs.

## Why an upsert rather than an insert

`UNIQUE (asset_id, quote_currency)` makes one row per pair the schema's own rule, so a
refresh either replaces the row or adds the first one. Expressed as read-then-write rather
than as SQLite's `INSERT ... ON CONFLICT`: the product is single user with one writer, the
table has four rows, and a dialect-specific statement would be the one query in this
application that a different database could not run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import StatementError

from portfolio.db.models import AssetPrice
from portfolio.domain.money import require_amount

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession


class PriceRepository:
    """Every query this application makes against `prices`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, *, asset_id: int, quote_currency: str) -> AssetPrice | None:
        """The row holding this pair's slot, or `None` if the pair has never been fetched.

        The two columns are exactly the ones `uq_prices_asset_currency` covers, so what this
        finds is what an insert would collide with.
        """
        found: AssetPrice | None = await self._session.scalar(
            select(AssetPrice).where(
                AssetPrice.asset_id == asset_id,
                AssetPrice.quote_currency == quote_currency,
            )
        )
        return found

    async def list_for_currency(self, quote_currency: str) -> list[AssetPrice]:
        """Every price quoted in one currency, ordered by `asset_id`.

        One query for a whole valuation, rather than one per holding. **Filtering by
        currency in SQL is safe and filtering by amount would not be**: `quote_currency` is
        a `TEXT` column holding a three-letter code, and an equality comparison on it is a
        string comparison. The rule is about money columns, not about `TEXT`.
        """
        result = await self._session.scalars(
            select(AssetPrice)
            .where(AssetPrice.quote_currency == quote_currency)
            .order_by(AssetPrice.asset_id)
        )
        return list(result)

    async def list_all(self) -> list[AssetPrice]:
        """Every price row, ordered by `asset_id` then `quote_currency`.

        For an operator command that wants to show the whole cache. Both sort keys are
        ordinary non-money columns; see the module docstring.
        """
        result = await self._session.scalars(
            select(AssetPrice).order_by(AssetPrice.asset_id, AssetPrice.quote_currency)
        )
        return list(result)

    async def upsert(
        self,
        *,
        asset_id: int,
        quote_currency: str,
        amount: Decimal,
        source: str,
        as_of: datetime,
        fetched_at: datetime,
    ) -> AssetPrice:
        """Write this pair's price, replacing the row that holds its slot if there is one.

        **Every field is overwritten, `source` included.** A refresh that failed over to a
        second vendor must leave the column naming the vendor that actually answered; a row
        that kept its old `source` while taking a new `amount` would be a record that cannot
        be audited and looks exactly like one that can.

        `amount` is a `Decimal` and stays one all the way into `NumericText`, which renders
        it as fixed-point text at exactly `PRICE_SCALE` places. Nothing converts it here.

        **The amount is checked before anything is staged, and that is not redundant with
        `NumericText`'s own refusal.** The column does refuse a `float`, but it refuses it
        at *bind* time: by then the ORM has either added a new object to the session or
        assigned the bad value onto a live one, so the failure arrives as a
        `sqlalchemy.exc.StatementError` from inside `flush()` with the session left needing
        a rollback, and the update branch has already mutated a row in memory. Checking
        here turns that into an ordinary `TypeError` raised before the session is touched,
        which is the same argument `WalletRepository.add` makes about not letting a driver
        exception out of a repository -- there it carried the bound row, here it carries
        the statement and a broken unit of work.

        The check is `domain.money.require_amount`, not a second rule written here. One
        definition of what an amount is, and `NumericText` calls the same function.

        Flushes rather than commits, so the caller's unit of work decides when the write
        becomes durable.

        Args:
            asset_id: the `assets.id` this price is for.
            quote_currency: `USD` or `EUR`; the `CHECK` on the column refuses anything else.
            amount: the price, exact.
            source: the name of the source that actually answered.
            as_of: our clock, and specifically the instant the refresh began. Not a vendor
                quote time -- none supplies one. See `db.models.AssetPrice`.
            fetched_at: when this row was written.

        Raises:
            TypeError: `amount` is not a `Decimal` -- a `float` out of a vendor's JSON is
                the case this exists for.
            ValueError: `amount` is a NaN or an infinity, or is an amount the column
                refuses -- too large for the digits before the point, or so fine that
                rounding it to `PRICE_SCALE` places leaves nothing of it. The last two are
                raised by `NumericText` during the flush and unwrapped by `_flush`, so
                they arrive as themselves rather than inside a `StatementError`.

        Returns:
            The row, whether it was created or updated.
        """
        require_amount(amount, subject="prices.amount")
        existing = await self.get(asset_id=asset_id, quote_currency=quote_currency)
        if existing is None:
            row = AssetPrice(
                asset_id=asset_id,
                quote_currency=quote_currency,
                amount=amount,
                source=source,
                as_of=as_of,
                fetched_at=fetched_at,
            )
            self._session.add(row)
            await self._flush()
            return row

        existing.amount = amount
        existing.source = source
        existing.as_of = as_of
        existing.fetched_at = fetched_at
        await self._flush()
        return existing

    async def _flush(self) -> None:
        """Flush, and never let a `sqlalchemy` exception out of this repository.

        **A column type that refuses a value raises at *bind* time, inside `flush()`, and
        SQLAlchemy wraps whatever it raised in a `StatementError`.** So `NumericText`'s two
        careful refusals -- an amount too large for the digits in front of the point, and a
        non-zero amount that rounds away to nothing -- reach a caller as
        `sqlalchemy.exc.StatementError`, carrying the `INSERT` statement with them.

        Three things are wrong with letting that travel. It is a `sqlalchemy` exception in
        a caller that `import-linter` forbids from importing `sqlalchemy`, which is the
        same layering breach as a raw `httpx` error reaching a service, in the other
        direction. It is a traceback where a message belongs, since `cli.py` catches the
        application's own exception types and not the driver's. And the sentence
        `NumericText` wrote to be read by a person is buried inside one the driver wrote
        to be read by a developer.

        The original is re-raised rather than translated into a repository-specific type.
        `WalletConstraintError` exists because an `IntegrityError` means only "a constraint
        refused this" and deciding *which* is the service's job; here there is nothing to
        decide -- the column has already said exactly what is wrong with the value, in a
        `ValueError` or a `TypeError` that the rest of this application already handles.
        Wrapping it again would bury a good message a second time.

        Anything whose cause is not one of those two is a real database failure and is left
        alone: it is re-raised as itself, because a connection that dropped is not a value
        this method can explain.

        Raises:
            TypeError: a column type refused the value's type.
            ValueError: a column type refused the value.
            sqlalchemy.exc.StatementError: anything else the flush hit -- a genuine
                database failure, which this method has no better account of.
        """
        try:
            await self._session.flush()
        except StatementError as exc:
            # `orig` is whatever was raised underneath: the DBAPI's error for a real
            # database failure, and the column type's own exception for a bind refusal.
            if isinstance(exc.orig, TypeError | ValueError):
                raise exc.orig from exc
            raise
