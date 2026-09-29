"""The cost-basis snapshot: recomputed from the stored fills, kept, and served valued (#19).

Two jobs, one class. `recompute` loads an owner's fills, turns each into a `Trade`, runs
`domain.accounting.replay`, and replaces the stored snapshot when the answer changed.
`positions` reads the stored snapshot back and values it at the cached prices.

**No provider here, and none reachable.** A router imports this module, so it imports
`repositories` and `services/prices.py` and nothing from `providers`: the
`prices-are-never-fetched-in-a-request` and `api-never-reaches-an-exchange-provider` import
contracts both hold for it without an edit to `.importlinter`. The fills are read from the
table the sync wrote, and the prices from the cache the refresh wrote.

## Recompute: skip when nothing changed, replace whole when something did

1. The fills are loaded as plain records -- never `raw_payload` -- and handed to a **worker
   thread**, where each becomes a `Trade` and `replay` runs. Both are CPU-bound and pure, and
   on the Pi a long history run on the event loop would stall every request for as long as it
   took (spec 021, *Rulings*).
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

## Valuing: USD, chain assets only, a reason for every missing price

The unit of account is USDT/USDC pinned at 1, so the valuation is in USD. EUR would need a
historical FX rate at every acquisition, which does not exist (spec 021, *Non-goals*).

An asset is priced only if it is a chain's native asset (`PRICED_ASSETS`), because only those
pairs are ever fetched. Anything else is `unsupported_pair` without a lookup -- which is the
true reason, where a lookup would answer `never_fetched` and send an operator to check a
refresh that will never price it. The arithmetic is `domain.accounting.value_position`'s.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from anyio import to_thread

from portfolio.domain.accounting import (
    METHOD,
    VALUE_SCALE,
    EventKey,
    Trade,
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
from portfolio.repositories.exchanges import ExchangeFillRepository
from portfolio.services.prices import Price, PriceUnavailable, build_price_service

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.domain.accounting import (
        AccountingResult,
        Adjustment,
        PortfolioTotals,
        Position,
        PositionValue,
    )
    from portfolio.repositories.exchanges import AccountingFillRecord
    from portfolio.services.prices import PriceLookup, PriceService

__all__ = [
    "ACCOUNTING_QUOTE_CURRENCY",
    "PRICED_ASSETS",
    "AccountingService",
    "AccountingStatus",
    "AccountingWarningKind",
    "PositionsView",
    "PricedPosition",
    "RecomputeOutcome",
    "RecomputeReason",
    "RecomputeReport",
    "SnapshotWarning",
    "UnconvertibleFillError",
    "build_accounting_service",
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

_NOTHING: Final = Decimal((0, (0,), -VALUE_SCALE))
"""Zero at the scale every figure is carried at: the unallocated costs of no snapshot."""


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


def _lot_kinds(events: Sequence[Trade | Adjustment]) -> dict[EventKey, str]:
    """The kind of each event that can acquire a lot, by key (spec 021, R2).

    A `Lot` carries its event's key and not its kind, and the stored lot needs both to name
    the event. Trades and adjustments are the only events that acquire anything; a transfer
    never does, so it is not in the map. #18 adds its adjustments to `events` here.
    """
    return {event.key: event_kind(event) for event in events}


def _replay_fills(records: Sequence[AccountingFillRecord]) -> _Replayed:
    """Convert and replay, in the worker thread. Pure: no session, no clock, no I/O.

    **The one place the owner's events are assembled.** #18 adds a second source -- the
    adjustments -- to the list built here and to the loader that feeds it, not a second
    pipeline.
    """
    events = [trade_of(record) for record in records]
    return _Replayed(result=replay(events), lot_kinds=_lot_kinds(events))


class AccountingService:
    """Recomputes and stores an owner's snapshot, and serves it valued. Owns its transaction.

    It holds the session, unlike the read-only `BalanceService`, because `recompute` commits:
    the snapshot's replacement is one transaction and this is the class that decides it.
    `positions` only reads.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        fills: ExchangeFillRepository,
        snapshots: AccountingSnapshotRepository,
        prices: PriceService,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._fills = fills
        self._snapshots = snapshots
        self._prices = prices
        self._clock = clock

    async def recompute(self, user_id: int) -> RecomputeReport:
        """Replay the owner's fills, and replace the stored snapshot if the answer changed.

        See the module docstring for the three steps. The worker thread is abandoned rather
        than waited for when the caller is cancelled: it touches no session, so shutdown does
        not have to wait out a long replay whose result nobody will write.

        Returns:
            `UNCHANGED` when the stored fingerprint matched and nothing was written, or
            `WRITTEN`; and the number of events replayed.

        Raises:
            UnconvertibleFillError: a stored fill does not convert. Nothing is written.
            ConflictingEventError: two events share an identity and differ -- unreachable from
                stored rows, whose unique constraints forbid it.
            decimal.InvalidOperation: replay left the engine's range (spec 019, *Risks*).
            Anything the write raises, including a figure `NumericText(18)` refuses; the
                transaction is rolled back first, so the previous snapshot stays.
        """
        records = await self._fills.list_fills_for_accounting(user_id)
        replayed = await to_thread.run_sync(_replay_fills, records, abandon_on_cancel=True)
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
        recompute and no fetch happen here**; a request is served from what is stored.
        """
        header = await self._snapshots.get_header(user_id, METHOD)
        if header is None:
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
        stored = await self._snapshots.list_positions(header.id)
        priced = tuple([await self._priced(position) for position in stored])
        return PositionsView(
            method=header.method,
            quote_currency=ACCOUNTING_QUOTE_CURRENCY,
            computed_at=header.computed_at,
            event_count=header.event_count,
            positions=priced,
            totals=value_portfolio(entry.value for entry in priced),
            unallocated_costs=header.unallocated_costs,
            warnings=await self._snapshots.list_warnings(header.id),
        )

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
        snapshots=AccountingSnapshotRepository(session),
        prices=build_price_service(session, clock=clock),
        clock=clock,
    )
