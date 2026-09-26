"""The venue #15's sync is driven against, and the SQL its tests read the result back with.

Three suites need the same pieces -- the service tests, the endpoint tests and the sentinel
test -- so they live here rather than in any one of them, the arrangement
`tests/balance_harness.py` has for #10.

## `SimulatedVenue` pages the way a venue does, not the way a script says

`tests/providers/exchanges/fakes.py`'s `FakeExchangeProvider` answers from a script keyed by
cursor and ignores the window, which is right for a test of one page and wrong for a sync:
the sync asks about many windows, and a scripted page for cursor `None` would be the answer
to every one of them, with fills outside most. This venue holds a set of fills and answers
each request from it: the fills inside the window (and for the symbol, if one is asked),
newest trade id first, `page_size` at a time, below the cursor for `TRADE_ID_BEFORE`. Every
page goes through `assemble_fill_page`, so the venue cannot answer anything a real provider
would be refused for.

## Faults are keyed by call number or decided by a function

`fault(call_number, window, cursor)` returns an exception to raise instead of answering, or
`None`. `faults_on({3: error})` is the common case -- "the third page fails" -- and a
function covers the rest: a venue whose real retention is shorter than it declares refuses
every window older than an instant, and a venue that is rate limited forever refuses every
call. The exception is raised **after** the call is recorded, so the call log is the list
of requests the venue *received*, which is what "resumes with page 2's cursor" is a
statement about.

`SimulatedPowerLoss` is a `BaseException` that is not an `Exception`: the stand-in for the
Raspberry Pi losing power, which no `except Exception` in the application may catch.

## Nothing here is a real credential, address or hostname

Trade ids are small integers, symbols are the documented examples, and no address appears.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import text

from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.providers.exchanges.base import (
    CursorKind,
    ExchangeCapabilities,
    ExchangeProvider,
    NormalizedFill,
    RateLimit,
    assemble_fill_page,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.exchanges.base import FillPage, FillWindow

#: Small, so a window of nine fills is three pages and a test can name each of them.
PAGE_SIZE: Final = 3

#: The instant the sync tests start at. A whole millisecond, so every bound built from it
#: is exact, and later than every fill a test scripts.
T0: Final = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


class SimulatedPowerLoss(BaseException):
    """The process dying mid-page. A `BaseException`, so no `except Exception` catches it."""


class VenueCallBudgetExceededError(BaseException):
    """More requests than any test scripts: a sync that pages forever, stopped.

    A `BaseException` so the sync cannot record it as an `internal` failure and carry on --
    a loop that never ends must end the test, loudly, rather than hang it until the
    thirty-second ceiling kills the whole run.
    """


#: Far above the most requests any test scripts (a `NONE` window halved down to one
#: millisecond is about fifty), and small enough that a loop ends in milliseconds.
MAX_VENUE_CALLS: Final = 400


@dataclass(frozen=True, slots=True)
class PageCall:
    """One request the venue received: which window, which cursor, which symbol."""

    window: FillWindow
    cursor: str | None
    symbol: str | None


type Fault = Callable[[int, "FillWindow", str | None], BaseException | None]
"""`(call_number, window, cursor) -> exception to raise, or None`. Call numbers start at 1."""


def faults_on(by_call: Mapping[int, BaseException]) -> Fault:
    """A fault that raises the given exception on the given call numbers, and nothing else."""

    def fault(call_number: int, window: FillWindow, cursor: str | None) -> BaseException | None:
        del window, cursor
        return by_call.get(call_number)

    return fault


def always(error: BaseException) -> Fault:
    """A fault that raises `error` on every call."""

    def fault(call_number: int, window: FillWindow, cursor: str | None) -> BaseException | None:
        del call_number, window, cursor
        return error

    return fault


def make_fill(
    trade_id: int,
    executed_at: datetime,
    *,
    symbol: str = "BTCUSDT",
    base_asset: str = "BTC",
    quote_asset: str = "USDT",
    side: FillSide = FillSide.BUY,
    quantity: str = "0.5",
    price: str = "60000",
    quote_quantity: str = "30000",
    fee_amount: str = "0.0005",
    fee_asset: str | None = "BTC",
    order_id: str | None = None,
    raw_payload: str | None = None,
) -> NormalizedFill:
    """One `NormalizedFill`, valid by construction, identified by an integer trade id."""
    return NormalizedFill(
        external_trade_id=str(trade_id),
        external_order_id=order_id if order_id is not None else f"9{trade_id}",
        symbol=symbol,
        base_asset=base_asset,
        quote_asset=quote_asset,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        quote_quantity=Decimal(quote_quantity),
        quote_quantity_derived=False,
        fee_amount=Decimal(fee_amount),
        fee_asset=fee_asset,
        executed_at=executed_at,
        raw_payload=raw_payload if raw_payload is not None else f'{{"tradeId":"{trade_id}"}}',
    )


def fills_between(
    count: int,
    *,
    first_id: int = 1001,
    newest: datetime = T0 - timedelta(minutes=1),
    spacing: timedelta = timedelta(minutes=1),
    symbols: Sequence[str] = ("BTCUSDT",),
) -> list[NormalizedFill]:
    """`count` fills, the newest at `newest`, each `spacing` older than the one before.

    Trade ids rise with time, as a venue's do, so the newest fill has the highest id --
    which is what makes `TRADE_ID_BEFORE` paging newest first.
    """
    return [
        make_fill(
            first_id + index,
            newest - spacing * (count - 1 - index),
            symbol=symbols[index % len(symbols)],
        )
        for index in range(count)
    ]


class SimulatedVenue:
    """A venue that answers from the fills it holds. Structural `ExchangeProvider`.

    Mutable on purpose: a test adds a fill between two runs (`fills.append`), swaps the
    fault, or replaces the next-cursor answers, to model a venue whose history grew or
    whose behaviour changed.
    """

    def __init__(
        self,
        fills: Iterable[NormalizedFill] = (),
        *,
        exchange_key: ExchangeKey = ExchangeKey.BITGET,
        retention: timedelta | None = timedelta(days=90),
        max_query_window: timedelta = timedelta(days=7),
        page_size: int = PAGE_SIZE,
        cursor_kind: CursorKind = CursorKind.TRADE_ID_BEFORE,
        requires_symbol: bool = False,
        symbols: Sequence[str] = (),
        fault: Fault | None = None,
        symbols_fault: BaseException | None = None,
        symbols_fault_times: int | None = None,
        next_cursors: Mapping[str | None, str | None] | None = None,
    ) -> None:
        self.fills: list[NormalizedFill] = list(fills)
        self.fault = fault
        self.symbols_fault = symbols_fault
        #: How many discovery calls fail before it answers; `None` is every one.
        self.symbols_fault_times = symbols_fault_times
        #: Overrides the computed next cursor for a request carrying the key's cursor, for
        #: a venue whose cursor misbehaves -- a cycle, say. Applied to `TRADE_ID_*` kinds.
        self.next_cursors: dict[str | None, str | None] = dict(next_cursors or {})
        self._symbols = tuple(symbols)
        self._capabilities = ExchangeCapabilities(
            exchange_key=exchange_key,
            retention=retention,
            max_query_window=max_query_window,
            page_size=page_size,
            cursor_kind=cursor_kind,
            rate_limit=RateLimit(max_requests=10, per_ms=1000),
            requires_symbol=requires_symbol,
        )
        #: Every page request received, in order, faults included.
        self.calls: list[PageCall] = []
        #: How many times `candidate_symbols` was asked.
        self.symbol_calls = 0
        #: An awaitable run before each answer, after the call is recorded -- for a test
        #: that needs to look at the database from inside a provider call.
        self.on_call: Callable[[PageCall], Any] | None = None

    @property
    def capabilities(self) -> ExchangeCapabilities:
        return self._capabilities

    async def fetch_fill_page(
        self, window: FillWindow, *, cursor: str | None, symbol: str | None
    ) -> FillPage:
        call = PageCall(window=window, cursor=cursor, symbol=symbol)
        self.calls.append(call)
        if len(self.calls) > MAX_VENUE_CALLS:
            message = f"the sync asked this venue for more than {MAX_VENUE_CALLS} pages"
            raise VenueCallBudgetExceededError(message)
        if self.on_call is not None:
            await self.on_call(call)
        if self.fault is not None:
            error = self.fault(len(self.calls), window, cursor)
            if error is not None:
                raise error
        matching = sorted(
            (
                fill
                for fill in self.fills
                if window.contains(fill.executed_at) and (symbol is None or fill.symbol == symbol)
            ),
            key=lambda fill: int(fill.external_trade_id),
            reverse=True,
        )
        size = self._capabilities.page_size
        if self._capabilities.cursor_kind is CursorKind.NONE:
            return assemble_fill_page(
                window,
                matching[:size],
                capabilities=self._capabilities,
                cursor=cursor,
                next_cursor=None,
                symbol=symbol,
            )
        below = [
            fill for fill in matching if cursor is None or int(fill.external_trade_id) < int(cursor)
        ]
        page = below[:size]
        next_cursor = page[-1].external_trade_id if len(below) > size else None
        if cursor in self.next_cursors:
            next_cursor = self.next_cursors[cursor]
        return assemble_fill_page(
            window,
            page,
            capabilities=self._capabilities,
            cursor=cursor,
            next_cursor=next_cursor,
            symbol=symbol,
        )

    async def candidate_symbols(self) -> Sequence[str]:
        self.symbol_calls += 1
        failing = self.symbols_fault_times is None or self.symbol_calls <= self.symbols_fault_times
        if self.symbols_fault is not None and failing:
            raise self.symbols_fault
        return self._symbols

    # -- what a test reads ---------------------------------------------------------------

    def cursors(self) -> list[str | None]:
        """The cursor of every request, in order."""
        return [call.cursor for call in self.calls]


_CONFORMS: ExchangeProvider = SimulatedVenue()
"""`mypy --strict` is the assertion; see `tests/providers/exchanges/fakes.py`."""


def changed(fill: NormalizedFill, **fields: object) -> NormalizedFill:
    """The same fill with some fields replaced -- a venue revising a settled trade."""
    return replace(fill, **fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Clocks and a sleeper, all under the test's control
# --------------------------------------------------------------------------------------


class SettableClock:
    """A wall clock the test moves by hand. Counts its reads."""

    def __init__(self, moment: datetime = T0) -> None:
        self.moment = moment
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return self.moment

    def advance(self, by: timedelta) -> None:
        self.moment += by


class TickingMonotonic:
    """A monotonic millisecond clock that moves `step` on every read."""

    def __init__(self, start: int = 1_000, step: int = 7) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> int:
        value = self.now
        self.now += self.step
        return value


class RecordingSleeper:
    """The injected sleep: records the whole seconds it was asked for and never waits."""

    def __init__(self) -> None:
        self.slept: list[int] = []

    async def __call__(self, seconds: int) -> None:
        self.slept.append(seconds)


# --------------------------------------------------------------------------------------
# Reading the tables back off the disk
# --------------------------------------------------------------------------------------

FILLS_SQL: Final = (
    "SELECT id, exchange_account_id, external_trade_id, external_order_id, symbol, side, "
    "quantity, price, quote_quantity, fee_amount, fee_asset, executed_at, raw_payload, "
    "ingested_at FROM exchange_fills ORDER BY id"
)
ACCOUNTS_SQL: Final = (
    "SELECT id, user_id, exchange_key, sync_status, requested_since, effective_since, "
    "planned_until, last_synced_at FROM exchange_accounts ORDER BY exchange_key"
)
WINDOWS_SQL: Final = (
    'SELECT id, exchange_account_id, "since", "until", symbol, "cursor" '
    "FROM exchange_sync_windows ORDER BY id"
)
RUNS_SQL: Final = (
    "SELECT id, trigger, status, started_at, finished_at, duration_ms, accounts_total, "
    "accounts_succeeded, accounts_failed, accounts_skipped FROM exchange_sync_runs ORDER BY id"
)
OUTCOMES_SQL: Final = (
    "SELECT o.exchange_sync_run_id, a.exchange_key, o.status, o.windows_completed, o.pages, "
    "o.fills_seen, o.fills_inserted, o.error_kind, o.detail "
    "FROM exchange_sync_run_accounts o JOIN exchange_accounts a "
    "ON a.id = o.exchange_account_id ORDER BY o.exchange_sync_run_id, a.exchange_key"
)


async def rows(factory: async_sessionmaker[AsyncSession], sql: str) -> list[dict[str, Any]]:
    """One of the statements above, over a session of its own, as plain dictionaries.

    A session of its own, so what it reads is what was **committed** -- never a row still
    pending in the identity map of the session the sync is using.
    """
    async with factory() as session:
        result = await session.execute(text(sql))
        return [dict(row) for row in result.mappings().all()]


async def trade_ids(factory: async_sessionmaker[AsyncSession]) -> list[str]:
    """Every stored trade id, in insertion order."""
    return [str(row["external_trade_id"]) for row in await rows(factory, FILLS_SQL)]


def sqlite_timestamp(moment: datetime) -> str:
    """A datetime as `UtcDateTime` stores it: UTC, fixed width, six fractional digits."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
