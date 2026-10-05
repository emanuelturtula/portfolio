"""The cost-basis snapshot: recomputed from the stored events, kept, and served valued (#19).

Two jobs, one class. `recompute` loads an owner's fills and manual adjustments, turns each into
the engine's event -- a `Trade`, or an `Adjustment` (#18) -- runs `domain.accounting.replay`
over **one** list of both, and replaces the stored snapshot when the answer changed.
`positions` reads the stored snapshot back and values it at the cached prices.

A third, small read sits beside them: `first_trades` (#111) says when each asset's imported
history begins. It reads the fills the recompute reads, and nothing of the snapshot.

**No provider here, and none reachable.** A router imports this module, so it imports
`repositories` and `services/prices.py` and nothing from `providers`: the
`prices-are-never-fetched-in-a-request` and `api-never-reaches-an-exchange-provider` import
contracts both hold for it without an edit to `.importlinter`. The fills are read from the
table the sync wrote, and the prices from the cache the refresh wrote.

## Recompute: skip when nothing changed, replace whole when something did

1. The fills and the adjustments are loaded as plain records -- never `raw_payload`, never
   an adjustment's note -- and handed to a **worker thread**, where each becomes its event and
   `replay` runs. Both are CPU-bound and pure, and on the Pi a long history run on the event
   loop would stall every request for as long as it took (spec 021, *Rulings*). The engine
   orders the events by its own key, so an adjustment lands among the fills by `occurred_at`,
   and after a fill at the same instant (spec 023, *The event*).
2. If the stored header's `input_fingerprint` equals the new one, nothing is written and the
   outcome is `UNCHANGED`: `computed_at` does not move. `ENGINE_VERSION` is in the
   fingerprint, so an engine upgrade always writes.
3. Otherwise the old header is deleted -- the cascade takes its children -- and the new one
   written, **in one transaction**, and the outcome is `WRITTEN`. A failure rolls it back and
   the previous snapshot stays exactly as it was.

## A row that does not convert fails the recompute, loudly

Every stored fill should convert: `NormalizedFill` refuses the shapes `Trade` refuses since
#99, and the one-time backfills hold zero fees. A row that does not is **not skipped**: a
skipped fill is a position that is wrong with nothing to say so, which is the
confident-wrong-number failure the engine exists to avoid (spec 020, *For #19*). It raises
`UnconvertibleFillError`, which carries the row's identity as attributes and never in its
message, and the snapshot already stored stays.

A stored adjustment is held to the same rule, with `UnconvertibleAdjustmentError`. It is as
unreachable: `services/adjustments.py` validates an adjustment by building the very
`Adjustment` this module builds, so nothing is stored that `adjustment_of` refuses (spec 023).
`adjustment_of` lives here, beside `trade_of`, because both are the one mapping from a stored
row to an event; `services/adjustments.py` re-exports it.

## Reading: one snapshot, never two

Reads on this engine are autocommit statements -- pysqlite opens no transaction for a
`SELECT` -- and SQLite reuses the header's id when a snapshot is replaced. So a recompute that
commits between reading the header and reading its positions would serve one snapshot's header
over another's rows. `read_snapshot` therefore reads the header, the positions and the
warnings, then reads the header again, and uses what it read only if the two headers are the
same snapshot; otherwise it reads again, up to `SNAPSHOT_READ_ATTEMPTS` times (spec 021, R5).
`positions` reads the prices afterwards: they are not part of the snapshot, and a price that
moves between two reads is simply the newer price.

**`read_snapshot` is public because it is the one implementation of that read.**
`services/reconciliation.py` (#104) compares the snapshot's quantities with the balances
held, and it reads them through the same method rather than through a second copy of the
header-children-header loop.

An explicit read transaction would do the same in one pass, but on this engine it would mean
issuing `BEGIN` behind SQLAlchemy's back or changing the transaction mode of every
connection, and neither is worth it for a read that a recompute interrupts a few times a day.

## Valuing: USD, chain assets only, a reason for every missing price

The unit of account is USDT/USDC pinned at 1, so the valuation is in USD. EUR would need a
historical FX rate at every acquisition, which does not exist (spec 021, *Non-goals*).

An asset is priced only if it is a chain's native asset (`PRICED_ASSETS`), because only those
pairs are ever fetched. Anything else is `unsupported_pair` without a lookup -- which is the
true reason, where a lookup would answer `never_fetched` and send an operator to check a
refresh that will never price it. The arithmetic is `domain.accounting.value_position`'s.

## First trades: the stored columns, compared in Python

`first_trades` answers one question for the adjustments form (spec 027): when is the earliest
imported fill an asset takes part in, so that an opening balance can be dated before it. It
loads the fills as the recompute does and takes the minimum of `executed_at` **in Python**, on
`datetime` values: the column is text in SQLite, and a `MIN()` or an `ORDER BY` on it would
compare strings. It builds no `Trade`, so a stored row the recompute would refuse still
answers here; and it reads no adjustment, because the question is about the imported history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from anyio import to_thread

from portfolio.domain.accounting import (
    DEFAULT_CASH_ASSETS,
    METHOD,
    VALUE_SCALE,
    Adjustment,
    EventKey,
    Trade,
    Transfer,
    event_kind,
    replay,
    value_portfolio,
    value_position,
)
from portfolio.domain.chains import CHAIN_ASSET_SYMBOLS
from portfolio.domain.currencies import QuoteCurrency
from portfolio.domain.exchanges import FillSide
from portfolio.repositories.accounting import (
    AccountingSnapshotRepository,
    AccountingWarningKind,
    SnapshotWarning,
)
from portfolio.repositories.adjustments import ManualAdjustmentRepository
from portfolio.repositories.exchanges import ExchangeFillRepository
from portfolio.services.prices import Price, PriceUnavailable, build_price_service

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.domain.accounting import (
        AccountingResult,
        PortfolioTotals,
        Position,
        PositionValue,
    )
    from portfolio.domain.accounting.events import AccountingEvent
    from portfolio.repositories.accounting import SnapshotHeader
    from portfolio.repositories.adjustments import AdjustmentRecord
    from portfolio.repositories.exchanges import AccountingFillRecord
    from portfolio.services.prices import PriceLookup, PriceService

__all__ = [
    "ACCOUNTING_QUOTE_CURRENCY",
    "ADJUSTMENT_SOURCE",
    "PRICED_ASSETS",
    "SNAPSHOT_READ_ATTEMPTS",
    "AccountingService",
    "AccountingStatus",
    "AccountingWarningKind",
    "FirstTrade",
    "PositionsView",
    "PricedPosition",
    "RecomputeOutcome",
    "RecomputeReason",
    "RecomputeReport",
    "SnapshotReadError",
    "SnapshotWarning",
    "StoredSnapshot",
    "UnconvertibleAdjustmentError",
    "UnconvertibleFillError",
    "adjustment_of",
    "build_accounting_service",
    "external_id_of",
    "first_trades_of",
    "lot_kinds_of",
    "trade_of",
    "utc_now",
]
"""`AccountingWarningKind` and `SnapshotWarning` are **re-exported** from
`repositories/accounting.py`, for the reason `services/balances.py` re-exports its run
vocabulary: `api/schemas/accounting.py` renders them and may not import a repository."""

ACCOUNTING_QUOTE_CURRENCY: Final = QuoteCurrency.USD
"""The currency positions are valued in. Not a parameter: see the module docstring."""

PRICED_ASSETS: Final[frozenset[str]] = frozenset(CHAIN_ASSET_SYMBOLS.values())
"""The assets a price is looked up for: every chain's native asset, spelled as `assets` holds it.

Taken from `domain.chains` rather than from `providers/prices/base.py`'s `SUPPORTED_PAIRS`,
which a request path may not import. A test holds the two sets equal, the way the three
spellings of the quote currencies are held together.
"""

SNAPSHOT_READ_ATTEMPTS: Final = 3
"""How many times `read_snapshot` reads the snapshot before giving up on a consistent one.

A read is retried only when a recompute committed in the middle of it, and a recompute writes
at startup, after an exchange sync that stored a fill or followed a failed recompute, and
after an adjustment changes -- a few times a day, each commit a fraction of a second. Two in
a row inside one read is already beyond what the triggers do; three is a margin, not a
measurement. See `SnapshotReadError` for what happens past it.
"""

_NOTHING: Final = Decimal((0, (0,), -VALUE_SCALE))
"""Zero at the scale every figure is carried at: the unallocated costs of no snapshot."""

ADJUSTMENT_SOURCE: Final = "manual"
"""The `EventKey.source` of every manual adjustment (spec 023, *The event*).

The source `lot_kinds_of` already expects for an adjustment, and one that sorts after every
venue key -- `bingx`, `bitget` -- so an adjustment at the same instant as a fill replays after it.
An owner recording an opening balance dates it before the first sale it has to cover.
"""

_EXTERNAL_ID_DIGITS: Final = 20
"""How wide `external_id_of` pads an id: enough for any SQLite `INTEGER` primary key."""


def utc_now() -> datetime:
    """The clock, in one place, so a test can replace it with a value it chose."""
    return datetime.now(UTC)


class RecomputeOutcome(StrEnum):
    """What a recompute did. The member is its wire form and its log field (spec 021, R1).

    `recompute` itself returns only `UNCHANGED` or `WRITTEN`, and raises for anything else.
    `FAILED` is what the trigger in `main.py` records when it raised, so that `last_recompute`
    on the endpoint is one vocabulary rather than two.
    """

    UNCHANGED = "unchanged"
    WRITTEN = "written"
    FAILED = "failed"


class RecomputeReason(StrEnum):
    """Why a recompute ran. Logged with every outcome, and nothing else reads it."""

    STARTUP = "startup"
    EXCHANGE_SYNC = "exchange_sync"
    ADJUSTMENT = "adjustment"
    """An owner created, replaced or deleted a manual adjustment (#18)."""


@dataclass(frozen=True, slots=True)
class RecomputeReport:
    """What one owner's recompute did, and over how many events (after deduplication)."""

    outcome: RecomputeOutcome
    event_count: int


@dataclass(frozen=True, slots=True)
class AccountingStatus:
    """The last recompute attempt since the process started, as `last_recompute` shows it.

    Held in memory on `app.state.accounting_status`, so a restart clears it and the startup
    recompute sets it again (spec 021, *Risks*). `at` is when the attempt ended, on our clock.
    `error` is the exception's class name when `outcome` is `FAILED`, and `None` otherwise --
    **never its message**, which is the rule `UnconvertibleFillError` is built around.
    """

    at: datetime
    outcome: RecomputeOutcome
    error: str | None


class UnconvertibleFillError(ValueError):
    """A stored fill could not be turned into a `Trade`, so the recompute stopped.

    **The message identifies nothing.** `exchange_account_id` and `external_trade_id` are
    attributes, for whoever catches it and needs to find the row; the text is what reaches a
    log, and this codebase keeps trade ids out of logs (spec 020, *For #19*). The refusal that
    caused it -- a `ValueError` or `TypeError` naming the field and the rule, never a value --
    is its `__cause__`.

    **It pickles and copies**, the way `ConflictingEventError` does and for the same reason:
    `BaseException` rebuilds itself by calling the class with `self.args`, which holds the one
    message rather than the two arguments `__init__` takes, and the recompute raises it inside
    a worker thread.
    """

    def __init__(self, exchange_account_id: int, external_trade_id: str) -> None:
        """Record which row it was, as attributes only."""
        super().__init__(
            "a stored exchange fill does not convert to a trade the accounting engine can replay"
        )
        self.exchange_account_id = exchange_account_id
        self.external_trade_id = external_trade_id

    def __reduce__(
        self,
    ) -> tuple[type[UnconvertibleFillError], tuple[int, str], dict[str, object]]:
        """Rebuild from the row's identity, and restore anything else set on the instance."""
        return (
            type(self),
            (self.exchange_account_id, self.external_trade_id),
            dict(self.__dict__),
        )


class UnconvertibleAdjustmentError(ValueError):
    """A stored manual adjustment does not convert to an `Adjustment`, so the recompute stopped.

    `UnconvertibleFillError`'s counterpart, built to the same rules (spec 023, *Loading and
    recompute*):

    * **The message identifies nothing.** `adjustment_id` is an attribute, for whoever needs to
      find the row. The refusal that caused it -- naming the field and the rule, never a value
      -- is its `__cause__`.
    * **It pickles and copies**, rebuilding itself from the id, because the recompute raises it
      inside a worker thread.

    **Unreachable in practice**: the service validates an adjustment by building this same
    `Adjustment` before anything is stored. It exists so that a row written some other way --
    by hand on the Pi, say -- fails the recompute loudly instead of being skipped.
    """

    def __init__(self, adjustment_id: int) -> None:
        """Record which row it was, as an attribute only."""
        super().__init__(
            "a stored manual adjustment does not convert to an adjustment the accounting "
            "engine can replay"
        )
        self.adjustment_id = adjustment_id

    def __reduce__(
        self,
    ) -> tuple[type[UnconvertibleAdjustmentError], tuple[int], dict[str, object]]:
        """Rebuild from the row's id, and restore anything else set on the instance."""
        return (type(self), (self.adjustment_id,), dict(self.__dict__))


class SnapshotReadError(RuntimeError):
    """The snapshot changed during every one of `SNAPSHOT_READ_ATTEMPTS` reads.

    Not reachable with the triggers this application has (see `SNAPSHOT_READ_ATTEMPTS`), and
    raised rather than answered, because the alternative is a response built from two
    snapshots. It is unhandled on purpose: the request fails with a 500, and the next one
    reads a snapshot that has stopped moving.
    """

    def __init__(self) -> None:
        """A fixed message: the snapshot's figures have no place in it."""
        super().__init__(
            f"the accounting snapshot changed during each of {SNAPSHOT_READ_ATTEMPTS} reads"
        )


@dataclass(frozen=True, slots=True)
class StoredSnapshot:
    """One snapshot as read: a header and the children read under it, checked to belong.

    What `AccountingService.read_snapshot` returns: the figures as the last recompute stored
    them, before any price is applied.
    """

    header: SnapshotHeader
    positions: tuple[Position, ...]
    warnings: tuple[SnapshotWarning, ...]


def _same_snapshot(first: SnapshotHeader, again: SnapshotHeader | None) -> bool:
    """Whether two reads of the header saw the same snapshot (spec 021, R5).

    The id alone cannot say so, because SQLite reuses it. The fingerprint says the content is
    the same, and `computed_at` -- a fresh clock reading at every write -- says it is the same
    write.
    """
    return again is not None and (
        again.id,
        again.input_fingerprint,
        again.computed_at,
    ) == (first.id, first.input_fingerprint, first.computed_at)


@dataclass(frozen=True, slots=True)
class PricedPosition:
    """One stored position, valued, and the price it was valued at if there was one.

    `price` is the service's `Price`, with its age and staleness, because the endpoint shows
    both; `value` carries the price's amount and everything computed from it.
    """

    value: PositionValue
    price: Price | None


@dataclass(frozen=True, slots=True)
class PositionsView:
    """An owner's snapshot, valued: everything `GET /api/accounting/positions` returns.

    `computed_at` is `None` when no snapshot has been written yet, and then `positions` and
    `warnings` are empty and every total is zero. That is "not computed", which the endpoint
    says with the `null`, and not "holds nothing", which a snapshot with no positions says.
    """

    method: str
    quote_currency: QuoteCurrency
    computed_at: datetime | None
    event_count: int
    positions: tuple[PricedPosition, ...]
    totals: PortfolioTotals
    unallocated_costs: Decimal
    warnings: tuple[SnapshotWarning, ...]


@dataclass(frozen=True, slots=True)
class FirstTrade:
    """When an asset's imported history begins: the instant of its earliest fill.

    `first_trade_at` is the fill's `executed_at` exactly as it is stored, timezone-aware UTC.
    """

    asset: str
    first_trade_at: datetime


@dataclass(frozen=True, slots=True)
class _Replayed:
    """What the worker thread hands back: the result, and the kind of event behind each lot."""

    result: AccountingResult
    lot_kinds: Mapping[EventKey, str]


def trade_of(record: AccountingFillRecord) -> Trade:
    """One stored fill as the engine's `Trade`, keyed by venue and trade id.

    `EventKey(executed_at, source=exchange_key, external_id=external_trade_id)`, and every
    trade field copied as it is stored. `side` is converted here, from the column's text.

    Raises:
        UnconvertibleFillError: the row breaks a rule `Trade` or `EventKey` enforces -- one
            of spec 019's R8 shapes, most plausibly, written before #99 refused them.
    """
    try:
        return Trade(
            key=EventKey(
                occurred_at=record.executed_at,
                source=record.exchange_key.value,
                external_id=record.external_trade_id,
            ),
            base_asset=record.base_asset,
            quote_asset=record.quote_asset,
            side=FillSide(record.side),
            quantity=record.quantity,
            quote_quantity=record.quote_quantity,
            fee_amount=record.fee_amount,
            fee_asset=record.fee_asset,
        )
    except (TypeError, ValueError) as exc:
        raise UnconvertibleFillError(record.exchange_account_id, record.external_trade_id) from exc


def external_id_of(adjustment_id: int) -> str:
    """An adjustment's `EventKey.external_id`: its id, zero-padded to twenty digits.

    `external_id` compares as text in the replay order, and `"10"` sorts before `"9"`. Padded,
    the text order is the numeric order, so two adjustments at one instant replay in the order
    they were entered -- and `AUTOINCREMENT` means that order is never reshuffled by a reused
    id. Twenty digits hold any id SQLite can assign.
    """
    return f"{adjustment_id:0{_EXTERNAL_ID_DIGITS}d}"


def adjustment_of(record: AdjustmentRecord) -> Adjustment:
    """One stored adjustment as the engine's `Adjustment`, keyed as a manual event.

    `EventKey(occurred_at, source=ADJUSTMENT_SOURCE, external_id=external_id_of(id))`, and
    `asset`, `quantity` and `unit_cost` copied as they are stored. **The same constructors the
    service validates an entry with**, so the rule a row is held to here is the rule it was
    accepted under (spec 023, *Validation*).

    Raises:
        UnconvertibleAdjustmentError: the row breaks a rule `Adjustment` or `EventKey`
            enforces, which only a row written around the service can.
    """
    try:
        return Adjustment(
            key=EventKey(
                occurred_at=record.occurred_at,
                source=ADJUSTMENT_SOURCE,
                external_id=external_id_of(record.id),
            ),
            asset=record.asset,
            quantity=record.quantity,
            unit_cost=record.unit_cost,
        )
    except (TypeError, ValueError) as exc:
        raise UnconvertibleAdjustmentError(record.id) from exc


def lot_kinds_of(events: Iterable[AccountingEvent]) -> dict[EventKey, str]:
    """The kind of the event behind each key a lot can carry (spec 021, R2 and R8).

    A `Lot` carries its event's key and not its kind, and the stored lot needs both to name the
    event by its identity, `(kind, source, external_id)`. So the kind is looked up by key, and
    this map is built so that the lookup can never pick the wrong event:

    * **Transfers never enter it.** They never acquire anything, so no lot can carry their key.
    * **Two events of different kinds under one key are refused**, rather than one silently
      overwriting the other. The key's `source` keeps them apart in every case that exists --
      a fill's is its venue, an adjustment's is `manual` -- so this is a guard on that
      invariant, not a case the data can produce.
    * An event repeated under one key with its own kind is the same event read twice, which
      `replay` counts once (I6), and it maps to the kind it has.

    Raises:
        ValueError: a trade and an adjustment share one `EventKey`. The message names neither.
    """
    kinds: dict[EventKey, str] = {}
    for event in events:
        if isinstance(event, Transfer):
            continue
        kind = event_kind(event)
        if kinds.setdefault(event.key, kind) != kind:
            message = (
                "two events of different kinds share one event key, so the lots they acquire "
                "could not be told apart"
            )
            raise ValueError(message)
    return kinds


def _replay_events(
    fills: Sequence[AccountingFillRecord],
    adjustments: Sequence[AdjustmentRecord],
) -> _Replayed:
    """Convert and replay, in the worker thread. Pure: no session, no clock, no I/O.

    **The one place the owner's events are assembled**, from both sources, into one list. The
    order they are listed in does not matter: `replay` sorts by `(occurred_at, source,
    external_id, kind)` and the fingerprint is taken over that order, so the same stored rows
    give the same fingerprint however they were read. A row of either kind that does not
    convert raises before anything is replayed.
    """
    events: list[AccountingEvent] = [trade_of(record) for record in fills]
    events.extend(adjustment_of(record) for record in adjustments)
    return _Replayed(result=replay(events), lot_kinds=lot_kinds_of(events))


def first_trades_of(fills: Iterable[AccountingFillRecord]) -> tuple[FirstTrade, ...]:
    """The earliest fill each non-cash asset takes part in, sorted by asset. Pure.

    **An asset takes part in a fill** as its base asset, as its quote asset, or as its fee
    asset when the fee amount is not zero: a zero fee moves nothing, so it does not count
    (spec 027). **Cash assets are left out** (`DEFAULT_CASH_ASSETS`): an adjustment of one is
    refused, so a date for one has no use.

    The minimum is taken here, on `datetime` values, and the order is the assets' code-point
    order, which is what `sorted` gives a `str`. No fills gives an empty tuple.

    **It reads the record's own columns and converts nothing** -- no `Trade`, no `FillSide`,
    no arithmetic -- so no stored fill can make it raise. `Decimal.is_zero` is false for a
    NaN rather than an error, and `executed_at` is always aware, which `UtcDateTime` sees to.
    """
    earliest: dict[str, datetime] = {}
    for record in fills:
        assets = [record.base_asset, record.quote_asset]
        if record.fee_asset is not None and not record.fee_amount.is_zero():
            assets.append(record.fee_asset)
        for asset in assets:
            if asset in DEFAULT_CASH_ASSETS:
                continue
            known = earliest.get(asset)
            if known is None or record.executed_at < known:
                earliest[asset] = record.executed_at
    return tuple(
        FirstTrade(asset=asset, first_trade_at=earliest[asset]) for asset in sorted(earliest)
    )


class AccountingService:
    """Recomputes and stores an owner's snapshot, and serves it valued. Owns its transaction.

    It holds the session, unlike the read-only `BalanceService`, because `recompute` commits:
    the snapshot's replacement is one transaction and this is the class that decides it.
    `positions`, `read_snapshot` and `first_trades` only read.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        fills: ExchangeFillRepository,
        adjustments: ManualAdjustmentRepository,
        snapshots: AccountingSnapshotRepository,
        prices: PriceService,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._fills = fills
        self._adjustments = adjustments
        self._snapshots = snapshots
        self._prices = prices
        self._clock = clock

    async def recompute(self, user_id: int) -> RecomputeReport:
        """Replay the owner's fills and adjustments, and replace the snapshot if the answer changed.

        See the module docstring for the three steps. The worker thread is abandoned rather
        than waited for when the caller is cancelled: it touches no session, so shutdown does
        not have to wait out a long replay whose result nobody will write.

        Returns:
            `UNCHANGED` when the stored fingerprint matched and nothing was written, or
            `WRITTEN`; and the number of events replayed.

        Raises:
            UnconvertibleFillError: a stored fill does not convert. Nothing is written.
            UnconvertibleAdjustmentError: a stored adjustment does not convert. Nothing is
                written.
            ConflictingEventError: two events share an identity and differ -- unreachable from
                stored rows, whose unique constraints and primary keys forbid it.
            decimal.InvalidOperation: replay left the engine's range (spec 019, *Risks*).
            Anything the write raises, including a figure `NumericText(18)` refuses; the
                transaction is rolled back first, so the previous snapshot stays.
        """
        fills = await self._fills.list_fills_for_accounting(user_id)
        adjustments = await self._adjustments.list_adjustments_for_accounting(user_id)
        replayed = await to_thread.run_sync(
            _replay_events, fills, adjustments, abandon_on_cancel=True
        )
        result = replayed.result
        stored = await self._snapshots.get_header(user_id, result.method)
        if stored is not None and stored.input_fingerprint == result.input_fingerprint:
            return RecomputeReport(RecomputeOutcome.UNCHANGED, result.event_count)
        try:
            await self._snapshots.replace(
                user_id,
                result,
                lot_kinds=replayed.lot_kinds,
                computed_at=self._clock(),
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return RecomputeReport(RecomputeOutcome.WRITTEN, result.event_count)

    async def positions(self, user_id: int) -> PositionsView:
        """The owner's stored snapshot, each position valued at its cached USD price.

        Reads only: the snapshot as the last recompute left it, and the price cache. **No
        recompute and no fetch happen here**; a request is served from what is stored. The
        snapshot is read whole and checked to be one snapshot before any price is read (see
        the module docstring).

        Raises:
            SnapshotReadError: the snapshot changed during every read attempt.
        """
        snapshot = await self.read_snapshot(user_id)
        if snapshot is None:
            return PositionsView(
                method=METHOD,
                quote_currency=ACCOUNTING_QUOTE_CURRENCY,
                computed_at=None,
                event_count=0,
                positions=(),
                totals=value_portfolio(()),
                unallocated_costs=_NOTHING,
                warnings=(),
            )
        header = snapshot.header
        priced = tuple([await self._priced(position) for position in snapshot.positions])
        return PositionsView(
            method=header.method,
            quote_currency=ACCOUNTING_QUOTE_CURRENCY,
            computed_at=header.computed_at,
            event_count=header.event_count,
            positions=priced,
            totals=value_portfolio(entry.value for entry in priced),
            unallocated_costs=header.unallocated_costs,
            warnings=snapshot.warnings,
        )

    async def read_snapshot(self, user_id: int) -> StoredSnapshot | None:
        """The owner's snapshot as one consistent read, or `None` when there is none.

        The header, then its positions and warnings, then the header again: if the second read
        is the same snapshot, nothing was replaced in between and the children belong to the
        header. Otherwise the whole read is repeated (spec 021, R5).

        Reads only, and no price: `positions` values what this returns, and the reconciliation
        (#104) compares its quantities with the balances held.

        Raises:
            SnapshotReadError: every one of `SNAPSHOT_READ_ATTEMPTS` reads was interrupted.
        """
        for _ in range(SNAPSHOT_READ_ATTEMPTS):
            header = await self._snapshots.get_header(user_id, METHOD)
            if header is None:
                return None
            positions = await self._snapshots.list_positions(header.id)
            warnings = await self._snapshots.list_warnings(header.id)
            again = await self._snapshots.get_header(user_id, METHOD)
            if _same_snapshot(header, again):
                return StoredSnapshot(header=header, positions=positions, warnings=warnings)
        raise SnapshotReadError

    async def first_trades(self, user_id: int) -> tuple[FirstTrade, ...]:
        """When each non-cash asset's imported history begins, sorted by asset.

        Reads the owner's fills, the ones `recompute` reads and through the same repository
        method, and reduces them with `first_trades_of`. Manual adjustments are not read: the
        answer is where the *imported* history of an asset starts, which is what an opening
        balance has to be dated before. Nothing is written, and nothing of the snapshot is
        read, so the answer does not wait for a recompute.
        """
        return first_trades_of(await self._fills.list_fills_for_accounting(user_id))

    async def _priced(self, position: Position) -> PricedPosition:
        """Value one position at its price, or with the reason it has none."""
        lookup = await self._price_of(position.asset)
        if isinstance(lookup, Price):
            return PricedPosition(value=value_position(position, lookup.amount, None), price=lookup)
        return PricedPosition(value=value_position(position, None, lookup.value), price=None)

    async def _price_of(self, asset: str) -> PriceLookup:
        """The asset's USD price, or why there is none. A non-chain asset is not looked up."""
        if asset not in PRICED_ASSETS:
            return PriceUnavailable.UNSUPPORTED_PAIR
        return await self._prices.lookup_price(asset, ACCOUNTING_QUOTE_CURRENCY)


def build_accounting_service(
    session: AsyncSession,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> AccountingService:
    """Assemble the service over one session.

    The clock is injectable and is shared with the price service, so that a test can name the
    instant a snapshot is computed at and the instant a price turns stale.
    """
    return AccountingService(
        session=session,
        fills=ExchangeFillRepository(session),
        adjustments=ManualAdjustmentRepository(session),
        snapshots=AccountingSnapshotRepository(session),
        prices=build_price_service(session, clock=clock),
        clock=clock,
    )
