"""Reads and writes of exchange accounts, the pending-window queue, and the fill log.

Queries and nothing else: no clock, no policy about what a window is or when an account is
done. Every repository here is handed an `AsyncSession` and **none of them commits** -- the
sync commits once per page, so that a page's fills and its checkpoint are one transaction,
and that decision belongs to the caller that owns the unit of work.

## Snapshots rather than ORM rows

`ExchangeAccountState` and `SyncWindowRow` are frozen dataclasses copied out of the rows,
and every write is an `UPDATE` by id. The sync rolls back a page that fails, and a rollback
expires every persistent object on the session; reading an attribute of an expired row is an
implicit lazy load, which under an async session is a `MissingGreenlet` rather than a query.
A snapshot and an integer cannot be expired -- `SyncRunRepository.finish_run` takes an id for
the same reason.

## No datetime is compared or ordered in SQL

`since`, `until` and the account's instants are `TEXT` in SQLite. Ordering or comparing them
in SQL is a string comparison that agrees with time only while every value is written by one
code path. The queue is read by account and ordered by `id`; the sync sorts it in Python.

## The fill log is append-only, and idempotent by constraint

`ExchangeFillRepository.insert_page` inserts with `ON CONFLICT DO NOTHING` on
`uq_exchange_fills_account_trade`: the constraint, not a pre-read, is what makes an
overlapping re-read insert nothing. A skipped id whose stored row differs from the incoming
fill on an accounting field is a collision and raises `FillConflictError`. The database
itself refuses `UPDATE` and `DELETE` of a fill (two triggers, `0007_exchange_sync`), so there
is no method here that could do either.

## The accounting reads the whole log, and never `raw_payload`

`list_fills_for_accounting` (#19) returns every fill of every account an owner has, as
`AccountingFillRecord`s, from a statement that names its columns. `raw_payload` is not one of
them: it is the venue's own object, kept for forensics, and a column never loaded is a column
that cannot reach a log or a snapshot.

## The transactions view reads the owner's fills, and never `raw_payload` or the trade id

`list_fills_for_view` (#93) returns every fill of the owner's accounts on the selected venues,
as `FillViewRecord`s, from `select_fills_for_view`, which names its columns: the ones the view
shows and totals, and **neither `raw_payload` nor `external_trade_id`**. The trade id is never
served, and a column never loaded cannot be served by mistake. The venues are filtered in SQL
-- an integer and an enum column -- and nothing else is: the view's date range and its order are
applied by the service, in Python, for the reason given above.

## This module cannot import `NormalizedFill`

`repositories` and `providers` are siblings in the layers contract and may not import each
other. `FillRecord` is the structural shape `insert_page` needs, and
`providers.exchanges.base.NormalizedFill` satisfies it -- which `mypy --strict` checks at the
one call site, in `services/exchange_sync.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from portfolio.db.models import ExchangeAccount, ExchangeFill, ExchangeSyncWindow
from portfolio.domain.exchanges import AccountSyncStatus, ExchangeKey, FillSide

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "AccountingFillRecord",
    "ExchangeAccountRepository",
    "ExchangeAccountState",
    "ExchangeFillRepository",
    "ExchangeSyncWindowRepository",
    "FillConflictError",
    "FillInsertResult",
    "FillRecord",
    "FillViewRecord",
    "SyncWindowRow",
    "select_fills_for_accounting",
    "select_fills_for_view",
]

_COMPARED_FIELDS: Final = (
    "external_order_id",
    "symbol",
    "base_asset",
    "quote_asset",
    "side",
    "quantity",
    "price",
    "quote_quantity",
    "quote_quantity_derived",
    "fee_amount",
    "fee_asset",
    "executed_at",
)
"""The accounting fields a re-read fill must agree on with the stored one, or it is a collision.

**`raw_payload` is not one of them**, deliberately narrowing spec 014's hand-on: a venue adding
a field to its response would otherwise make every overlap re-read a false collision and stall
the account. `external_trade_id` is the key, and `ingested_at` is our clock.

Amounts are compared as `Decimal`s, so the column's trailing zeros are not a difference, and
`executed_at` as aware datetimes, so the zone a value was written in is not one either.
"""


_INSERT_CHUNK_ROWS: Final = 500
"""The most fills one `INSERT` carries: 500 rows of 16 columns is 8000 bound parameters.

A multi-row `VALUES` binds every column of every row, and SQLite caps the parameters one
statement may bind. The cap has been 32766 since SQLite 3.32, and `RETURNING`, which this
statement needs, arrived in 3.35 -- so any SQLite that can run the statement allows 8000. A
Bitget page is 100 fills, so today every page is one statement; the loop is for a venue with
a larger page.
"""


@dataclass(frozen=True, slots=True)
class ExchangeAccountState:
    """One `exchange_accounts` row, copied out of the session. See the module docstring."""

    id: int
    user_id: int
    exchange_key: ExchangeKey
    sync_status: AccountSyncStatus
    requested_since: datetime | None
    effective_since: datetime | None
    planned_until: datetime | None
    last_synced_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SyncWindowRow:
    """One pending window, copied out of the session. The bounds are unvalidated here.

    `FillWindow` is what refuses an inverted or off-grid window, and the sync builds one from
    these bounds when it reads the row; this layer only moves the values.
    """

    id: int
    exchange_account_id: int
    since: datetime
    until: datetime
    symbol: str | None
    cursor: str | None


@dataclass(frozen=True, slots=True)
class FillInsertResult:
    """What one page's insert did: how many fills it was given, and how many were new.

    `seen - inserted` is how many the constraint skipped as already stored -- the overlap
    re-read, working as intended.
    """

    seen: int
    inserted: int


class FillConflictError(Exception):
    """A page carried a fill whose trade id is stored already, with different contents.

    Either the venue revised a settled fill under the same id, or its trade ids are not one
    sequence per account. Neither is a re-sync, and keeping the first version silently would
    be worse than stopping: the account fails with `conflict` until somebody looks.

    **The message states a count and never an id.** A trade id is a cursor at Bitget, and
    the message is stored as the outcome's `detail` and served by an endpoint.
    """

    def __init__(self, conflicts: int) -> None:
        """Carry how many fills conflicted, and nothing that identifies them."""
        self.conflicts = conflicts
        super().__init__(
            f"{conflicts} fill(s) in the page share a trade id with a stored fill but differ "
            "from it on an accounting field, so the page was not recorded."
        )


class FillRecord(Protocol):
    """The shape of a fill `insert_page` can store. `NormalizedFill` is one.

    Read-only properties rather than plain attributes, because `NormalizedFill` is a frozen
    dataclass and a protocol declaring settable attributes would not accept it.
    """

    @property
    def external_trade_id(self) -> str:
        """The venue's id for the execution; the key of the constraint."""

    @property
    def external_order_id(self) -> str | None:
        """The venue's id for the order the execution belongs to, if it sends one."""

    @property
    def symbol(self) -> str:
        """The venue's spelling of the pair."""

    @property
    def base_asset(self) -> str:
        """The asset bought or sold."""

    @property
    def quote_asset(self) -> str:
        """The asset it was paid for in."""

    @property
    def side(self) -> FillSide:
        """Which way the base asset moved."""

    @property
    def quantity(self) -> Decimal:
        """In the base asset."""

    @property
    def price(self) -> Decimal:
        """Quote per base."""

    @property
    def quote_quantity(self) -> Decimal:
        """In the quote asset."""

    @property
    def quote_quantity_derived(self) -> bool:
        """Whether the provider derived `quote_quantity` because the venue omitted it."""

    @property
    def fee_amount(self) -> Decimal:
        """Signed: positive a fee paid, negative a rebate."""

    @property
    def fee_asset(self) -> str | None:
        """`None` only when the fee is zero."""

    @property
    def executed_at(self) -> datetime:
        """The venue's clock."""

    @property
    def raw_payload(self) -> str:
        """The venue's own fill object, as canonical JSON."""


def _state_of(row: ExchangeAccount) -> ExchangeAccountState:
    """Copy an account row into its snapshot, turning the column strings into the vocabulary."""
    return ExchangeAccountState(
        id=row.id,
        user_id=row.user_id,
        exchange_key=ExchangeKey(row.exchange_key),
        sync_status=AccountSyncStatus(row.sync_status),
        requested_since=row.requested_since,
        effective_since=row.effective_since,
        planned_until=row.planned_until,
        last_synced_at=row.last_synced_at,
        created_at=row.created_at,
    )


class ExchangeAccountRepository:
    """Every query this application makes against `exchange_accounts`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(self, user_id: int) -> list[ExchangeAccountState]:
        """The owner's accounts, sorted by `exchange_key`."""
        rows = await self._session.scalars(
            select(ExchangeAccount)
            .where(ExchangeAccount.user_id == user_id)
            .order_by(ExchangeAccount.exchange_key)
        )
        return [_state_of(row) for row in rows]

    async def get(self, account_id: int) -> ExchangeAccountState | None:
        """One account by id, read afresh from the database, or `None`.

        `populate_existing` so a row already in the session's identity map is refreshed from
        the database rather than handed back as it was the last time it was loaded -- the
        writes here are `UPDATE` statements, which do not touch the identity map.
        """
        row = await self._session.scalar(
            select(ExchangeAccount)
            .where(ExchangeAccount.id == account_id)
            .execution_options(populate_existing=True)
        )
        return None if row is None else _state_of(row)

    async def ensure(
        self,
        *,
        user_id: int,
        exchange_key: ExchangeKey,
        created_at: datetime,
    ) -> ExchangeAccountState:
        """The owner's account at a venue, created at `never_synced` if it does not exist.

        Read-then-insert rather than an upsert: one process, one coordinator and one run at a
        time make the gap between the two statements unreachable, and the unique constraint
        is still there to refuse a second row if that ever stops being true.
        """
        row = await self._session.scalar(
            select(ExchangeAccount).where(
                ExchangeAccount.user_id == user_id,
                ExchangeAccount.exchange_key == exchange_key,
            )
        )
        if row is None:
            row = ExchangeAccount(
                user_id=user_id,
                exchange_key=exchange_key,
                created_at=created_at,
                sync_status=AccountSyncStatus.NEVER_SYNCED,
            )
            self._session.add(row)
            await self._session.flush()
        return _state_of(row)

    async def set_history(
        self,
        account_id: int,
        *,
        requested_since: datetime,
        effective_since: datetime,
        planned_until: datetime,
    ) -> None:
        """Record a plan: what was asked for, and the floor and ceiling of what is planned."""
        await self._session.execute(
            update(ExchangeAccount)
            .where(ExchangeAccount.id == account_id)
            .values(
                requested_since=requested_since,
                effective_since=effective_since,
                planned_until=planned_until,
            )
        )

    async def set_effective_since(self, account_id: int, effective_since: datetime) -> None:
        """Move the floor of the planned history. The caller decides the direction."""
        await self._session.execute(
            update(ExchangeAccount)
            .where(ExchangeAccount.id == account_id)
            .values(effective_since=effective_since)
        )

    async def set_status(self, account_id: int, status: AccountSyncStatus) -> None:
        """Record where the account stands, leaving `last_synced_at` alone."""
        await self._session.execute(
            update(ExchangeAccount)
            .where(ExchangeAccount.id == account_id)
            .values(sync_status=status)
        )

    async def mark_synced(self, account_id: int, *, synced_at: datetime) -> None:
        """Record a run that left nothing pending: `ok`, and when."""
        await self._session.execute(
            update(ExchangeAccount)
            .where(ExchangeAccount.id == account_id)
            .values(sync_status=AccountSyncStatus.OK, last_synced_at=synced_at)
        )


def _row_of(window: ExchangeSyncWindow) -> SyncWindowRow:
    """Copy a window row into its snapshot."""
    return SyncWindowRow(
        id=window.id,
        exchange_account_id=window.exchange_account_id,
        since=window.since,
        until=window.until,
        symbol=window.symbol,
        cursor=window.cursor,
    )


class ExchangeSyncWindowRepository:
    """Every query this application makes against `exchange_sync_windows`, the pending queue.

    A row is one window not yet finished. The sync deletes it with the page that reads its
    last fill, in the same transaction, which is what makes the queue a checkpoint.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_account(self, account_id: int) -> list[SyncWindowRow]:
        """One account's pending windows, by `id`. The sync sorts them by time, in Python."""
        rows = await self._session.scalars(
            select(ExchangeSyncWindow)
            .where(ExchangeSyncWindow.exchange_account_id == account_id)
            .order_by(ExchangeSyncWindow.id)
            .execution_options(populate_existing=True)
        )
        return [_row_of(row) for row in rows]

    async def count_for_account(self, account_id: int) -> int:
        """How many windows the account still has to read. A row count; no column is read."""
        total = await self._session.scalar(
            select(func.count())
            .select_from(ExchangeSyncWindow)
            .where(ExchangeSyncWindow.exchange_account_id == account_id)
        )
        return total or 0

    async def add(
        self,
        account_id: int,
        *,
        since: datetime,
        until: datetime,
        symbol: str | None,
    ) -> int:
        """Queue a window from its first page (`cursor` `NULL`). Returns the new row's id."""
        window = ExchangeSyncWindow(
            exchange_account_id=account_id,
            since=since,
            until=until,
            symbol=symbol,
            cursor=None,
        )
        self._session.add(window)
        await self._session.flush()
        return window.id

    async def advance(self, window_id: int, cursor: str) -> None:
        """Record the cursor of the next page to request. Commit it with that page's fills."""
        await self._session.execute(
            update(ExchangeSyncWindow)
            .where(ExchangeSyncWindow.id == window_id)
            .values(cursor=cursor)
        )

    async def set_since(
        self, window_id: int, since: datetime, *, keep_cursor: bool = False
    ) -> None:
        """Move a window's start, in place, keeping its cursor only when told to.

        **By default the cursor is cleared** and the window restarts from its first page:
        whether a venue's cursor still means the same thing once the range under it moved is an
        assumption about its semantics, and re-reading a page costs nothing but the request --
        the unique constraint makes the re-read insert nothing. The sync passes
        `keep_cursor=True` for the cursor kinds `cursor_survives_a_moved_since` vouches for, a
        trade-id bound, which is what keeps an interrupted oldest window from restarting.
        """
        values: dict[str, object] = {"since": since}
        if not keep_cursor:
            values["cursor"] = None
        await self._session.execute(
            update(ExchangeSyncWindow).where(ExchangeSyncWindow.id == window_id).values(values)
        )

    async def delete(self, window_id: int) -> None:
        """Remove a window: read to its end, emptied by the retention clamp, or replaced."""
        await self._session.execute(
            delete(ExchangeSyncWindow).where(ExchangeSyncWindow.id == window_id)
        )


def _column_values(
    account_id: int, fill: FillRecord, *, ingested_at: datetime
) -> dict[str, object]:
    """One fill as the column values of its row."""
    return {
        "exchange_account_id": account_id,
        "external_trade_id": fill.external_trade_id,
        "external_order_id": fill.external_order_id,
        "symbol": fill.symbol,
        "base_asset": fill.base_asset,
        "quote_asset": fill.quote_asset,
        "side": fill.side,
        "quantity": fill.quantity,
        "price": fill.price,
        "quote_quantity": fill.quote_quantity,
        "quote_quantity_derived": fill.quote_quantity_derived,
        "fee_amount": fill.fee_amount,
        "fee_asset": fill.fee_asset,
        "executed_at": fill.executed_at,
        "raw_payload": fill.raw_payload,
        "ingested_at": ingested_at,
    }


def _differs(stored: ExchangeFill, incoming: FillRecord) -> bool:
    """Whether a stored fill and a re-read one disagree on any field in `_COMPARED_FIELDS`.

    `==` on each pair, which is exactly the comparison each type needs: `Decimal` equality
    ignores trailing zeros, aware `datetime` equality compares instants, and a `FillSide` is a
    `StrEnum` equal to the string the column holds.
    """
    return any(getattr(stored, field) != getattr(incoming, field) for field in _COMPARED_FIELDS)


class ExchangeFillRepository:
    """Every query this application makes against `exchange_fills`. Inserts and reads only."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_page(
        self,
        account_id: int,
        fills: Sequence[FillRecord],
        *,
        ingested_at: datetime,
    ) -> FillInsertResult:
        """Insert a page of fills, skipping every one already stored, and count both.

        `INSERT ... ON CONFLICT (exchange_account_id, external_trade_id) DO NOTHING RETURNING
        external_trade_id`, one row per fill: **idempotency is the constraint**, so a page
        re-read after a crash, or an overlap re-read on purpose, inserts nothing twice.
        `inserted` is the number of ids the statement returned.

        **Every id the constraint skipped is then compared with the stored row** on the fields
        in `_COMPARED_FIELDS`. A difference is a collision, and this raises rather than
        dropping the second version.

        Neither commits nor rolls back. **On `FillConflictError` the page's other rows are
        still in the open transaction**, and "the page writes nothing" is true once the caller
        rolls back -- which the sync does for any failure in a page. Undoing them here is not
        possible: the append-only trigger refuses a `DELETE`, and a `SAVEPOINT` is unreliable
        on pysqlite's default transaction handling, which is what the runtime engine uses.

        Raises:
            FillConflictError: a skipped id's stored row differs. The message is a count.
        """
        inserted_ids: set[str] = set()
        for start in range(0, len(fills), _INSERT_CHUNK_ROWS):
            chunk = fills[start : start + _INSERT_CHUNK_ROWS]
            statement = (
                sqlite_insert(ExchangeFill)
                .values(
                    [_column_values(account_id, fill, ingested_at=ingested_at) for fill in chunk]
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        ExchangeFill.exchange_account_id,
                        ExchangeFill.external_trade_id,
                    ]
                )
                .returning(ExchangeFill.external_trade_id)
            )
            inserted_ids.update((await self._session.scalars(statement)).all())
        skipped = [fill for fill in fills if fill.external_trade_id not in inserted_ids]
        if skipped:
            await self._refuse_collisions(account_id, skipped)
        return FillInsertResult(seen=len(fills), inserted=len(inserted_ids))

    async def _refuse_collisions(self, account_id: int, skipped: Sequence[FillRecord]) -> None:
        """Raise if any skipped fill differs from the row stored under its id.

        One query for the page, not one per fill. `populate_existing` so the stored values are
        the database's rather than whatever an earlier read left in the identity map.

        A fill skipped by the constraint but with no stored row under its id cannot happen in
        one transaction -- the constraint only skips a row that exists -- except when a page
        carries the same id twice, which `assemble_fill_page` refuses; such a fill is compared
        with the row its twin just inserted, which is still right.
        """
        stored_rows = await self._session.scalars(
            select(ExchangeFill)
            .where(
                ExchangeFill.exchange_account_id == account_id,
                ExchangeFill.external_trade_id.in_({fill.external_trade_id for fill in skipped}),
            )
            .execution_options(populate_existing=True)
        )
        stored = {row.external_trade_id: row for row in stored_rows}
        conflicts = sum(
            1
            for fill in skipped
            if (row := stored.get(fill.external_trade_id)) is None or _differs(row, fill)
        )
        if conflicts:
            raise FillConflictError(conflicts)

    async def count_for_account(self, account_id: int) -> int:
        """How many fills an account has stored. `COUNT(*)`: no money column is read."""
        total = await self._session.scalar(
            select(func.count())
            .select_from(ExchangeFill)
            .where(ExchangeFill.exchange_account_id == account_id)
        )
        return total or 0

    async def list_fills_for_accounting(self, user_id: int) -> list[AccountingFillRecord]:
        """Every fill of every exchange account `user_id` owns, as plain records, by fill id.

        The whole history, every time: the accounting engine replays from the first event, and
        a recompute that read part of the log would compute a different, wrong position. For a
        personal history that is thousands of rows, which is not a performance question.

        **`raw_payload` is never loaded.** The statement names its columns, and
        `select_fills_for_accounting` is public so that a test can compile it and see. The
        venue's own object is forensic data; nothing the accounting needs is in it that is not
        already a column, and a column not loaded cannot reach a log.

        **Nothing is compared or ordered in SQL but integers**: the owner by `user_id` and the
        order by `ExchangeFill.id`. The engine sorts by its own key, in Python, anyway.
        """
        result = await self._session.execute(select_fills_for_accounting(user_id))
        return [
            AccountingFillRecord(
                id=row.id,
                exchange_account_id=row.exchange_account_id,
                exchange_key=ExchangeKey(row.exchange_key),
                external_trade_id=row.external_trade_id,
                external_order_id=row.external_order_id,
                symbol=row.symbol,
                base_asset=row.base_asset,
                quote_asset=row.quote_asset,
                side=row.side,
                quantity=row.quantity,
                price=row.price,
                quote_quantity=row.quote_quantity,
                quote_quantity_derived=row.quote_quantity_derived,
                fee_amount=row.fee_amount,
                fee_asset=row.fee_asset,
                executed_at=row.executed_at,
                ingested_at=row.ingested_at,
            )
            for row in result
        ]

    async def list_fills_for_view(
        self, user_id: int, exchanges: Collection[ExchangeKey] | None
    ) -> Sequence[FillViewRecord]:
        """Every fill of `user_id`'s accounts on `exchanges`, as plain records, by fill id.

        `exchanges` is `None` for every venue; a collection selects those venues, and an empty
        one selects none. **The venue is the only filter here**: the service applies the date
        range and the order in Python, because `executed_at` is text in SQLite (see the module
        docstring), and so it reads the owner's whole history on those venues. For a personal
        history that is thousands of rows; spec 024 records what 50,000 cost.

        **Neither `raw_payload` nor `external_trade_id` is loaded**: the statement names its
        columns, and `select_fills_for_view` is public so that a test can compile it and see.
        """
        result = await self._session.execute(select_fills_for_view(user_id, exchanges))
        return [
            FillViewRecord(
                id=row.id,
                exchange_key=ExchangeKey(row.exchange_key),
                external_order_id=row.external_order_id,
                symbol=row.symbol,
                base_asset=row.base_asset,
                quote_asset=row.quote_asset,
                side=FillSide(row.side),
                quantity=row.quantity,
                price=row.price,
                quote_quantity=row.quote_quantity,
                quote_quantity_derived=row.quote_quantity_derived,
                fee_amount=row.fee_amount,
                fee_asset=row.fee_asset,
                executed_at=row.executed_at,
            )
            for row in result
        ]


@dataclass(frozen=True, slots=True)
class AccountingFillRecord:
    """One stored fill as the accounting reads it: every column but `raw_payload`, plus the venue.

    A frozen record rather than the ORM row, for the reason `ExchangeAccountState` is one, and
    for a second reason of its own: the accounting service hands these to a worker thread,
    where an ORM row attached to an async session must not travel.

    **`side` is the column's text, unconverted.** A `CHECK` keeps it to `buy` or `sell`, but
    turning it into a `FillSide` is part of turning the record into a `Trade`, which is where a
    row that does not convert is reported with its identity (`UnconvertibleFillError`).
    """

    id: int
    exchange_account_id: int
    exchange_key: ExchangeKey
    external_trade_id: str
    external_order_id: str | None
    symbol: str
    base_asset: str
    quote_asset: str
    side: str
    quantity: Decimal
    price: Decimal
    quote_quantity: Decimal
    quote_quantity_derived: bool
    fee_amount: Decimal
    fee_asset: str | None
    executed_at: datetime
    ingested_at: datetime


def select_fills_for_accounting(user_id: int) -> Select[*tuple[Any, ...]]:
    """The statement `list_fills_for_accounting` runs, with every column it loads named.

    Every column of `exchange_fills` except `raw_payload`, and the account's `exchange_key`.
    The join is on the account, so a fill of another owner's account is never read.
    """
    return (
        select(
            ExchangeFill.id,
            ExchangeFill.exchange_account_id,
            ExchangeAccount.exchange_key,
            ExchangeFill.external_trade_id,
            ExchangeFill.external_order_id,
            ExchangeFill.symbol,
            ExchangeFill.base_asset,
            ExchangeFill.quote_asset,
            ExchangeFill.side,
            ExchangeFill.quantity,
            ExchangeFill.price,
            ExchangeFill.quote_quantity,
            ExchangeFill.quote_quantity_derived,
            ExchangeFill.fee_amount,
            ExchangeFill.fee_asset,
            ExchangeFill.executed_at,
            ExchangeFill.ingested_at,
        )
        .join(ExchangeAccount, ExchangeAccount.id == ExchangeFill.exchange_account_id)
        .where(ExchangeAccount.user_id == user_id)
        .order_by(ExchangeFill.id)
    )


@dataclass(frozen=True, slots=True)
class FillViewRecord:
    """One stored fill as the transactions view reads it: what it shows and what it totals.

    Every column but `raw_payload`, `external_trade_id`, `exchange_account_id` and
    `ingested_at`, plus the account's venue. `side` is a `FillSide` here, unlike
    `AccountingFillRecord`'s: `ck_exchange_fills_side` admits nothing else, and the view has no
    per-row error to report a stray value with.
    """

    id: int
    exchange_key: ExchangeKey
    external_order_id: str | None
    symbol: str
    base_asset: str
    quote_asset: str
    side: FillSide
    quantity: Decimal
    price: Decimal
    quote_quantity: Decimal
    quote_quantity_derived: bool
    fee_amount: Decimal
    fee_asset: str | None
    executed_at: datetime


def select_fills_for_view(
    user_id: int, exchanges: Collection[ExchangeKey] | None
) -> Select[*tuple[Any, ...]]:
    """The statement `list_fills_for_view` runs, with every column it loads named.

    Joined on the account, so a fill of another owner's account is never read. The venues are
    an `IN` on `exchange_accounts.exchange_key`, sorted so the statement is the same for the
    same set; `None` leaves the clause out. Ordered by `ExchangeFill.id`, an integer, so the
    read is deterministic; the view's own order is the service's.
    """
    statement = (
        select(
            ExchangeFill.id,
            ExchangeAccount.exchange_key,
            ExchangeFill.external_order_id,
            ExchangeFill.symbol,
            ExchangeFill.base_asset,
            ExchangeFill.quote_asset,
            ExchangeFill.side,
            ExchangeFill.quantity,
            ExchangeFill.price,
            ExchangeFill.quote_quantity,
            ExchangeFill.quote_quantity_derived,
            ExchangeFill.fee_amount,
            ExchangeFill.fee_asset,
            ExchangeFill.executed_at,
        )
        .join(ExchangeAccount, ExchangeAccount.id == ExchangeFill.exchange_account_id)
        .where(ExchangeAccount.user_id == user_id)
        .order_by(ExchangeFill.id)
    )
    if exchanges is not None:
        statement = statement.where(
            ExchangeAccount.exchange_key.in_(sorted({ExchangeKey(key) for key in exchanges}))
        )
    return statement
