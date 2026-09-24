"""Reads and writes of `balance_snapshots`.

Queries and nothing else: no clock, no valuation, no policy about what a missing snapshot
means. The repository is handed an `AsyncSession` and it does not commit -- the service
that opened the unit of work decides when it ends.

## The table is append-only, and every read here depends on it

A run adds a row and nothing updates one. That is what makes `MAX(id)` a correct answer to
"the latest snapshot per wallet": identity order is insertion order, so the newest row for a
wallet is the one with the largest id, and the question needs no argument about how a `TEXT`
datetime collates.

## What may be compared in SQL here, and what may not

`confirmed` and `pending` are `BaseUnits` -- `BigInteger` columns holding an exact count of
indivisible units -- so they are ordinary integers to SQLite and carry none of the hazard a
`TEXT` money column does. Nothing in this module sums or orders by them anyway; the rule
that matters is enforced one layer up, where a total is built.

`observed_at` **is** compared and ordered in SQL, and that is deliberate rather than an
oversight of rule 2. `UtcDateTime` normalises every value to UTC on the way in and
SQLAlchemy writes a SQLite datetime at a fixed width, so lexicographic order is
chronological order. `NumericText` has a fixed *scale* and a variable number of integer
digits, which is why `"9"` sorts after `"10"` there and why money is aggregated in Python.
The rule is about variable-width digits, not about `TEXT`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError, StatementError

from portfolio.db.models import BalanceSnapshot

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["BalanceRepository", "SnapshotConstraintError"]


class SnapshotConstraintError(Exception):
    """An insert the database refused, re-raised without the driver's exception.

    The same class `WalletConstraintError` is, and for the same reason: SQLAlchemy renders
    the bound parameters into a `StatementError`'s message, so letting an `IntegrityError`
    travel upward carries the row with it -- a wallet id and a balance -- into a traceback
    and from there into a log line that `redact_sensitive` cannot help with, because that
    processor matches key names and the field is called `exception`.

    Re-raising a bare exception here is also what keeps `sqlalchemy` out of the service
    layer: the sync can tell "the database refused this" from "something else went wrong"
    without importing the driver to name its error type.

    The realistic cause is `uq_balance_snapshots_wallet_run` -- one run writing a wallet
    twice -- which would mean the grouping by chain had broken and the same address was
    about to be counted twice in one total.
    """


class BalanceRepository:
    """Every query this application makes against `balance_snapshots`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        wallet_id: int,
        sync_run_id: int,
        confirmed: int,
        pending: int | None,
        decimals: int,
        observed_at: datetime,
    ) -> BalanceSnapshot:
        """Append one reading. Nothing is ever updated in this table.

        `decimals` is written onto the row rather than looked up in `assets` at read time,
        so that editing an asset cannot reinterpret a reading that was already taken.

        `pending` is passed through exactly as the provider reported it, `None` included:
        `None` means the chain does not answer the mempool question and zero means it
        answered zero, and collapsing the two would destroy the distinction #7 added the
        field for.

        Flushes rather than commits, so the caller's unit of work decides when the write
        becomes durable.

        Raises:
            SnapshotConstraintError: the database refused the insert -- a duplicate
                `(wallet_id, sync_run_id)`, or a wallet or run that is not there.
            TypeError: `confirmed` or `pending` is not an `int`. `BaseUnits` refuses a
                `float` out of a vendor's JSON, which is the case this exists for.
            ValueError: the count is outside the signed 64-bit range SQLite can hold, or
                `confirmed` is negative and the `CHECK` refused it.
        """
        snapshot = BalanceSnapshot(
            wallet_id=wallet_id,
            sync_run_id=sync_run_id,
            confirmed=confirmed,
            pending=pending,
            decimals=decimals,
            observed_at=observed_at,
        )
        self._session.add(snapshot)
        await self._flush()
        return snapshot

    async def latest_for_wallets(self, wallet_ids: Sequence[int]) -> dict[int, BalanceSnapshot]:
        """The newest snapshot for each of these wallets, keyed by `wallet_id`.

        **A wallet with no snapshot is simply absent from the result**, rather than mapped
        to a zero. An unread wallet and an empty wallet are different facts and the caller
        is the layer allowed to say which; a zero here would make them the same row.

        Resolved by `MAX(id)` over an append-only table -- see the module docstring. Two
        queries' worth of work in one statement, and one statement regardless of how many
        wallets are asked about.
        """
        if not wallet_ids:
            # `IN ()` is legal but pointless, and an empty argument is the ordinary case
            # for an account that has registered nothing yet.
            return {}
        newest = (
            select(func.max(BalanceSnapshot.id))
            .where(BalanceSnapshot.wallet_id.in_(wallet_ids))
            .group_by(BalanceSnapshot.wallet_id)
            .scalar_subquery()
        )
        rows = await self._session.scalars(
            select(BalanceSnapshot).where(BalanceSnapshot.id.in_(newest))
        )
        return {row.wallet_id: row for row in rows}

    async def history(
        self,
        *,
        wallet_id: int,
        since: datetime | None,
        after: tuple[datetime, int] | None,
        limit: int,
    ) -> list[BalanceSnapshot]:
        """One wallet's readings, always oldest first. **Which `limit` rows depends on the ask.**

        Three windows, and the asymmetry is deliberate rather than an oversight -- a reader
        who assumes one rule for all three will read this as a bug, so it is written down at
        each layer:

        | Asked with | Rows |
        |---|---|
        | neither | the **most recent** `limit`, reversed back to oldest-first |
        | `since` | the **first** `limit` at or after it -- the first page of a walk forward |
        | `after` | the **first** `limit` strictly after `(observed_at, id)` -- every later page |

        **The default has to be the recent end.** The only consumer is a chart, and the
        oldest five hundred readings of a wallet that has been watched for a year are the
        wrong five hundred: they render a picture of last January and stop.

        **A walk forward needs the pair, not the instant.** The first version of this method
        documented "take the last `observed_at` you saw and ask again with it as `since`",
        and that cursor never advances: `since` is inclusive and has no tie-break, so a page
        of one row returned the same row forever, and any page whose last rows shared an
        instant with the next page's first rows repeated them. `after` is a keyset: rows
        strictly after `(observed_at, id)` in `(observed_at, id)` order. Snapshots are
        append-only, so `id` is insertion order and the pair is a total order over one
        wallet's history -- which is what makes "strictly after" mean something.

        The keyset is spelled as `observed_at > t OR (observed_at = t AND id > i)` rather than
        as a row-value comparison. SQLite has supported row values since 3.15, but this form
        says what it means to a reader who has not memorised that, and it is the one every
        other database accepts too.

        **Equality on `observed_at` is exact**, which the keyset depends on. The cursor's
        instant was read out of this column and is bound back through the same `UtcDateTime`,
        so it renders to the same fixed-width text the row holds. A cursor whose instant had
        been through a float, or had lost its microseconds, would compare unequal and skip
        the rows it shares an instant with -- which is why the API encodes it from
        `isoformat()` and nothing coarser.

        `since` and `after` together is a caller's mistake; the service refuses the pair, and
        this method gives `after` precedence rather than inventing a combination.

        The descending arm reverses in Python rather than asking SQL for the rows twice.
        `limit` is bounded by the page size, so the list being reversed is bounded too.

        The comparisons and all three orderings are done in SQL; the module docstring says
        why that is safe here and is not safe for a money column.
        """
        statement = select(BalanceSnapshot).where(BalanceSnapshot.wallet_id == wallet_id)
        if after is not None:
            instant, snapshot_id = after
            statement = statement.where(
                or_(
                    BalanceSnapshot.observed_at > instant,
                    and_(
                        BalanceSnapshot.observed_at == instant,
                        BalanceSnapshot.id > snapshot_id,
                    ),
                )
            )
        elif since is not None:
            statement = statement.where(BalanceSnapshot.observed_at >= since)
        else:
            newest = await self._session.scalars(
                statement.order_by(
                    BalanceSnapshot.observed_at.desc(), BalanceSnapshot.id.desc()
                ).limit(limit)
            )
            return list(reversed(list(newest)))
        rows = await self._session.scalars(
            statement.order_by(BalanceSnapshot.observed_at, BalanceSnapshot.id).limit(limit)
        )
        return list(rows)

    async def _flush(self) -> None:
        """Flush, and never let a `sqlalchemy` exception out of this repository.

        Two failure shapes arrive through the same door. A constraint the database refused
        is an `IntegrityError`, translated into this module's own type so that the bound row
        does not travel with it. A value a column type refused -- `BaseUnits` meeting a
        `float` that came out of a vendor's JSON -- is raised at *bind* time, inside
        `flush()`, and SQLAlchemy wraps it in a `StatementError`; that one is unwrapped so
        the `TypeError` the column wrote for a person to read arrives as itself rather than
        buried in one the driver wrote for a developer.

        The same arrangement `PriceRepository._flush` makes, for the same three reasons:
        a `sqlalchemy` exception in a caller that may not import `sqlalchemy` is a layering
        breach in the other direction, a traceback belongs where a message does, and the
        good message must not be buried.

        Anything else is a genuine database failure and is re-raised as itself: a connection
        that dropped is not a value this method has an account of.
        """
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # `from exc` keeps the chain for a debugger, and it is safe to keep *because*
            # the engine is built with `hide_parameters=True`. The two are one control.
            raise SnapshotConstraintError from exc
        except StatementError as exc:
            if isinstance(exc.orig, TypeError | ValueError):
                raise exc.orig from exc
            raise
