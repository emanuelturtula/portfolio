"""The seam every exchange provider is on the other side of: fills, pages, capabilities.

An exchange provider answers one question -- which spot fills happened on this account in
this window -- one page at a time, and declares what it can do so that the sync (#15) can
plan its requests without knowing which venue it is talking to.

## A page, not a stream

`fetch_fill_page` returns one `FillPage` and not an async generator of fills, because the
sync must commit a checkpoint between pages. A generator hides exactly the boundary a
restart needs to resume from.

## The contract is enforced by construction, as `align_balances` enforces the chain one

A provider parses its response into `NormalizedFill`s and hands them to
`assemble_fill_page`, and the rules follow from the code rather than from the implementer
having remembered them: every fill inside the window, no trade id twice, no more fills than
the declared page size, and a cursor that moved. `NormalizedFill` itself refuses any amount
the column would change.

## Money is `Decimal` from the parser to the column

`require_fill_amount` is the parser-side boundary -- a JSON string, a `Decimal` from
`decode_json`, or an `int`, and nothing else -- and `NormalizedFill` refuses any amount
finer than `FILL_SCALE`, because `NumericText` would round it and a fill is stored "as
reported". **A value a column would transform is refused by the parser that received it**,
which is the rule `docs/providers.md` already states for prices.

## Time is integer milliseconds, and never a float

`datetime_from_epoch_ms` is `EPOCH + timedelta(milliseconds=value)`. The obvious
`datetime.fromtimestamp(ms / 1000)` is a float division in `providers/`, and a float is how
the millisecond of a fill at the edge of a window ends up on the wrong side of it.

## What is confirmed and what is assumed

Nothing vendor-specific is here, by design. That both target venues use epoch-millisecond
timestamps, numeric error codes and one of the four `CursorKind` shapes is belief, not
measurement; #13 and #14 confirm or correct it against each venue's documentation.
`RETENTION_MARGIN` and `FILL_SCALE` are guesses recorded as guesses at their definitions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Real imports, not `TYPE_CHECKING` ones: both are checked against at run time.
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

from portfolio.db.models import FILL_SCALE
from portfolio.domain.exchanges import FillSide
from portfolio.domain.money import MONEY_PRECISION, multiply, quantize
from portfolio.providers.exchanges.errors import ExchangeSchemaError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from portfolio.domain.exchanges import ExchangeKey

__all__ = [
    "EPOCH",
    "MAX_AMOUNT_DIGITS",
    "MAX_FILL_INTEGER_DIGITS",
    "MAX_RAW_PAYLOAD_DEPTH",
    "RETENTION_MARGIN",
    "CursorKind",
    "ExchangeCapabilities",
    "ExchangeProvider",
    "FillPage",
    "FillWindow",
    "NormalizedFill",
    "RateLimit",
    "RetentionClamp",
    "assemble_fill_page",
    "clamp_to_retention",
    "datetime_from_epoch_ms",
    "derive_quote_quantity",
    "encode_raw_payload",
    "epoch_ms",
    "floor_to_millisecond",
    "require_cursor_advanced",
    "require_fill_amount",
]

EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
"""The instant epoch milliseconds count from, as an aware UTC datetime."""

_ONE_MILLISECOND: Final = timedelta(milliseconds=1)
"""The granularity of this seam: every window bound and every duration is a multiple of it."""

RETENTION_MARGIN: Final = timedelta(minutes=5)
"""How far inside a venue's retention edge the oldest request is placed. **A guess.**

Without a margin the oldest window is at the retention edge when it is computed and past it
by the time the request lands -- a few hundred milliseconds of clock skew or queueing is
enough -- and the venue refuses it. Five minutes trades up to five minutes of the oldest
history for not being refused at the edge. A venue that measures retention in calendar days
rather than as a rolling duration may need more; #13 finds out.
"""

MAX_FILL_INTEGER_DIGITS: Final = MONEY_PRECISION - FILL_SCALE
"""Digits a fill amount may carry before the decimal point: 38 - 18 = 20.

Derived from the column, not invented here, for the reason `MAX_PRICE_INTEGER_DIGITS` gives:
an amount this application cannot store is not an amount it should accept.
"""

MAX_AMOUNT_DIGITS: Final = 100
"""The most digits an amount may have written out in full, before and after the point.

A fill amount this application can store is at most 20 + 18 = 38 digits written out, and a
venue padding it with trailing zeros -- `0.00012300` is the ordinary spelling -- adds a few
more. A hundred leaves room for any padding a venue plausibly uses and refuses a number no
venue sends: `1.` followed by five thousand ones is valid JSON, and `1e999999999999999999`
is a finite `Decimal`. Neither is an amount, and both are values the next arithmetic step
would spend its time on or fail on.

Counted written out rather than as significant digits, so the bound covers the exponent as
well: a single digit a billion places from the point is refused here, not in a `quantize`
three layers later. And a hundred digits is far inside the interpreter's 4300-digit
limit on converting an integer to or from a string, so no amount this admits can trip it.
"""

MAX_RAW_PAYLOAD_DEPTH: Final = 32
"""How deeply the containers in a venue's fill object may nest before it is refused.

A fill object is a flat record with, at most, a list or an object inside it -- a fee
breakdown, say -- so a depth of three is realistic and thirty-two is an order of magnitude
past it. **The bound is ours rather than the interpreter's on purpose.** Leaving it to the
recursion limit would refuse at a depth that differs by platform -- `decode_json`'s own
docstring measured its parser failing near 3000 levels on Windows and past 5000 on the
Pi -- and would surface as a `RecursionError` outside the taxonomy. This is the same
refusal on every machine, and it is a schema error, because the depth is the venue's choice.
"""

_DECIMAL_TEXT: Final = re.compile(r"\A-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
"""A decimal number as a venue writes one in a JSON string, and nothing looser.

`Decimal()` itself accepts far more than a venue should send: surrounding whitespace,
underscores between digits, every Unicode digit (`Decimal("١٢")` is twelve), `Infinity` and
`NaN`. Each of those in a price or a quantity is a vendor sending something other than a
number, and the place to say so is the boundary rather than a column three layers later.
"""

_EPOCH_MS_TEXT: Final = re.compile(r"\A[0-9]{1,15}\Z")
"""An epoch-millisecond count written as a string: ASCII digits only, at most fifteen.

Fifteen digits reaches the year 33658, which is past anything `datetime` can hold anyway; the
cap is there so a hostile string of thousands of digits is refused by its length rather than
by `int()` running into the interpreter's digit limit.
"""


class CursorKind(StrEnum):
    """How a venue pages through fills. Which one each venue uses is #13's and #14's to confirm.

    The four shapes both target venues are believed to have, and the sync handles each
    differently -- which is why this is declared rather than discovered.
    """

    TRADE_ID_BEFORE = "trade_id_before"
    """Page backwards from the last trade id seen."""
    TRADE_ID_AFTER = "trade_id_after"
    """Page forwards from the last trade id seen."""
    TIME = "time"
    """The window's start advances past the last fill seen."""
    NONE = "none"
    """One page per window; a full page means the window must be split."""


@dataclass(frozen=True, slots=True)
class RateLimit:
    """A venue's request budget: at most `max_requests` every `per_ms` milliseconds.

    Integers, because `float` is banned in `providers/` and because a rate limit is a count
    over a duration, which is exactly what two integers say.

    **It refuses itself at construction**, rather than leaving the refusal to the
    capabilities that hold it: a `RateLimit(0, 1000)` would divide by zero the first time
    anything asked for its interval, and a type that can do that should not be
    constructible.
    """

    max_requests: int
    per_ms: int

    def __post_init__(self) -> None:
        """Refuse a budget that describes no real venue.

        Raises:
            ValueError: either field is not an `int` (a `bool` included) or is below one.
        """
        _require_positive_int(self.max_requests, field="RateLimit.max_requests")
        _require_positive_int(self.per_ms, field="RateLimit.per_ms")

    @property
    def min_interval_ms(self) -> int:
        """The shortest gap between two requests that keeps within the budget.

        `ceil(per_ms / max_requests)`, in integer arithmetic: rounding *up*, because an
        interval one millisecond too short is a budget exceeded by one request per window,
        and a venue that bans for it does not care that it was only one. Three requests a
        second is 334 ms, never 333.
        """
        return -(-self.per_ms // self.max_requests)


@dataclass(frozen=True, slots=True)
class ExchangeCapabilities:
    """What a venue can do, declared rather than assumed. Constant for a provider's life.

    Every field is one the sync has to plan around, and each is here because the issue
    names it:

    | Field | What the sync does with it |
    |---|---|
    | `retention` | clamps the oldest request (`clamp_to_retention`); `None` keeps all |
    | `max_query_window` | splits a range into windows no longer than this |
    | `page_size` | the most fills one page may carry (`assemble_fill_page`) |
    | `cursor_kind` | how it asks for the next page |
    | `rate_limit` | how far apart it spaces requests |
    | `requires_symbol` | whether it asks per symbol, after `candidate_symbols` |

    Four of them are consumed in this module -- `retention` by the clamp, and
    `max_query_window`, `page_size` and `requires_symbol` by the page check -- because a
    capability nothing reads is decoration, and it drifts out of date without anything
    noticing.
    """

    exchange_key: ExchangeKey
    retention: timedelta | None
    max_query_window: timedelta
    page_size: int
    cursor_kind: CursorKind
    rate_limit: RateLimit
    requires_symbol: bool

    def __post_init__(self) -> None:
        """Refuse a declaration that cannot describe a real venue.

        A rate limit with a field below one never reaches this: `RateLimit` refuses itself
        at construction.

        A retention shorter than `RETENTION_MARGIN` is legal, if implausible, and is not
        refused: `clamp_to_retention` never moves a request past `now`, so such a venue
        simply has nothing old enough to ask about.

        Both durations must be whole milliseconds, the granularity of the seam: a window
        built from millisecond bounds is a whole number of milliseconds long, and a limit
        off that grid would be one no window could exactly meet.

        Raises:
            ValueError: a page size below one, or a query window or retention that is zero,
                negative or not a whole number of milliseconds.
            TypeError: `rate_limit` is not a `RateLimit`.
        """
        _require_positive_int(self.page_size, field="page_size")
        _require_millisecond_duration(self.max_query_window, field="max_query_window")
        if self.retention is not None:
            _require_millisecond_duration(self.retention, field="retention")
        _require_rate_limit(self.rate_limit)


@dataclass(frozen=True, slots=True)
class FillWindow:
    """A half-open time range, `[since, until)`: a fill at `since` is in, one at `until` is not.

    Half-open so that consecutive windows tile a range with no instant counted twice and
    none skipped -- the sync splits a range into windows end to end, and a closed interval
    would put a fill executed exactly on a boundary into two pages.
    """

    since: datetime
    until: datetime

    def __post_init__(self) -> None:
        """Refuse a window that cannot be placed in time, is off the grid, or is empty.

        **Both bounds must be whole milliseconds**, because milliseconds are the granularity
        of the seam: a venue is asked in epoch milliseconds (`epoch_ms` floors), and it
        answers in them (`datetime_from_epoch_ms`). A `since` of `12:00:00.000500` would be
        sent as `12:00:00.000`, and a venue that correctly returned a fill from `.000200`
        would have its page refused for answering outside a window it was never actually
        told about. Build bounds with `floor_to_millisecond`.

        Raises:
            ValueError: either bound is naive or not a whole millisecond, or `since` is not
                before `until`.
        """
        _require_aware(self.since, field="FillWindow.since")
        _require_aware(self.until, field="FillWindow.until")
        _require_whole_millisecond(self.since, field="FillWindow.since")
        _require_whole_millisecond(self.until, field="FillWindow.until")
        if self.since >= self.until:
            message = "FillWindow.since must be before FillWindow.until"
            raise ValueError(message)

    @property
    def duration(self) -> timedelta:
        """How long the window is. Derived, never stored."""
        return self.until - self.since

    def contains(self, moment: datetime) -> bool:
        """Whether `moment` falls in `[since, until)`. The one place the edge is decided."""
        return self.since <= moment < self.until


@dataclass(frozen=True, slots=True)
class NormalizedFill:
    """One spot trade execution, in the one shape every venue's fill is translated into.

    **It refuses what the column would transform.** `__post_init__` raises
    `ExchangeSchemaError` for:

    * an amount that is not a `Decimal` (a `bool`, a `float` or an `int` included -- an
      `int` goes through `require_fill_amount` first), or is not finite;
    * a `quantity`, `price` or `quote_quantity` at or below zero;
    * any amount with more than `MAX_FILL_INTEGER_DIGITS` digits before the point;
    * **any amount with more than `FILL_SCALE` fractional digits**, tested as
      `quantize(value, FILL_SCALE) != value` so trailing zeros are not a false refusal.
      `NumericText` would round such a value silently -- right for a price, wrong for a
      quote quantity stored "as reported" -- so it is refused before it gets there;
    * an empty or whitespace `external_trade_id`, `symbol`, `base_asset` or `quote_asset`,
      and any text field -- those four, `external_order_id`, `fee_asset`, `raw_payload` --
      that does not encode as UTF-8, which in practice is a lone surrogate from a
      `\\ud800` escape the JSON parser accepted;
    * a `side` that is not a `FillSide`;
    * a `fee_asset` of `None` beside a non-zero fee, or a blank one;
    * a naive `executed_at`;
    * a `quote_quantity_derived` that is not exactly a `bool`, or a blank `raw_payload`.

    **No message quotes an amount or a trade id**: a fill quantity is the owner's holdings.
    Each names the field and the rule.

    **`external_trade_id` must be unique per account across every symbol.** It is what
    `uq_exchange_fills_account_trade` is keyed on, so a venue whose ids are unique only
    within a symbol must namespace them -- `BTC-USDT:12345` -- or the constraint turns two
    different fills into one and silently drops the second. #14 must check its venue.

    **`quote_quantity` is as reported**, unless the venue omitted it: then the provider
    calls `derive_quote_quantity` and sets `quote_quantity_derived`. Never recomputed when
    reported -- a one-unit disagreement with the venue's rounding haunts every
    reconciliation after it.

    `fee_amount` is signed: positive is a fee paid, negative a rebate. `raw_payload` is the
    venue's own fill object through `encode_raw_payload`, never the envelope or the request.
    """

    external_trade_id: str
    external_order_id: str | None
    symbol: str
    """The venue's spelling, e.g. `BTCUSDT`."""
    base_asset: str
    quote_asset: str
    side: FillSide
    quantity: Decimal
    """In the base asset. Greater than zero."""
    price: Decimal
    """Quote asset per unit of base asset. Greater than zero."""
    quote_quantity: Decimal
    """In the quote asset. Greater than zero. As reported unless `quote_quantity_derived`."""
    quote_quantity_derived: bool
    fee_amount: Decimal
    """Signed: positive is a fee paid, negative a rebate."""
    fee_asset: str | None
    """`None` only when `fee_amount` is zero."""
    executed_at: datetime
    """The venue's clock. Timezone-aware."""
    raw_payload: str
    """Canonical JSON of the venue's own fill object, from `encode_raw_payload`."""

    def __post_init__(self) -> None:
        """Refuse a fill the column would change, or that cannot be accounted for.

        Raises:
            ExchangeSchemaError: any rule in the class docstring is broken. The detail
                names the field and the rule, never the value.
        """
        _require_text(self.external_trade_id, field="external_trade_id")
        if self.external_order_id is not None:
            # Not `_require_text`: whether a venue ever sends a blank order id is unknown,
            # and blank is not what breaks an insert. Unencodable text is.
            _require_utf8(self.external_order_id, field="external_order_id")
        _require_text(self.symbol, field="symbol")
        _require_text(self.base_asset, field="base_asset")
        _require_text(self.quote_asset, field="quote_asset")
        _require_side(self.side)
        _require_storable_amount(self.quantity, field="quantity", positive=True)
        _require_storable_amount(self.price, field="price", positive=True)
        _require_storable_amount(self.quote_quantity, field="quote_quantity", positive=True)
        _require_storable_amount(self.fee_amount, field="fee_amount", positive=False)
        if self.fee_asset is None:
            if not self.fee_amount.is_zero():
                detail = "fee_asset must name an asset when fee_amount is not zero"
                raise ExchangeSchemaError(detail)
        else:
            _require_text(self.fee_asset, field="fee_asset")
        if not _is_aware(self.executed_at):
            detail = "executed_at must be a timezone-aware datetime"
            raise ExchangeSchemaError(detail)
        _require_flag(self.quote_quantity_derived, field="quote_quantity_derived")
        _require_text(self.raw_payload, field="raw_payload")


@dataclass(frozen=True, slots=True)
class FillPage:
    """One page of fills, with what was asked and where the next page starts.

    Built by `assemble_fill_page`, which is where the fetch contract is enforced; nothing is
    validated again here, for the reason `AddressBalance` gives -- a second refusal in the
    constructor would give one condition two exception types.

    `next_cursor` is `None` when the venue has nothing after this page in this window. What
    that means depends on `CursorKind`: for `NONE` there is never a next cursor, and a page
    holding `page_size` fills means the window must be split rather than that it is done.
    #15 owns that decision; this type only carries the facts it is made from.
    """

    window: FillWindow
    fills: tuple[NormalizedFill, ...]
    cursor: str | None
    next_cursor: str | None
    symbol: str | None


@dataclass(frozen=True, slots=True)
class RetentionClamp:
    """What was asked for and what can actually be asked, and whether they differ."""

    requested_since: datetime
    effective_since: datetime

    @property
    def clamped(self) -> bool:
        """Whether the request was moved forward. Derived, never stored."""
        return self.effective_since > self.requested_since


class ExchangeProvider(Protocol):
    """What an exchange provider must offer. Structural, and checked by `mypy`.

    Not `@runtime_checkable`, for the reason `ChainProvider` is not: `isinstance` against a
    runtime-checkable protocol compares attribute names and nothing else, so a class whose
    `fetch_fill_page` takes the wrong arguments, or is not a coroutine function, passes it.
    The real check is `mypy --strict` deciding assignability.

    **Every failure is one of the seven classes in `providers.exchanges.errors`**, and
    nothing else: a `ProviderResponseError` out of `decode_json`, an `httpx.TransportError`
    or a `KeyError` from a parser is translated at the provider's boundary, `from` the
    original.
    """

    @property
    def capabilities(self) -> ExchangeCapabilities:
        """What this venue can do. Constant for the life of the instance."""

    async def fetch_fill_page(
        self,
        window: FillWindow,
        *,
        cursor: str | None,
        symbol: str | None,
    ) -> FillPage:
        """Read one page of the account's fills inside `window`.

        `cursor` is `None` for the first page of a window and the previous page's
        `next_cursor` after that. `symbol` is given exactly when the capabilities say
        `requires_symbol`. The result is built with `assemble_fill_page`, which enforces
        the rest.

        Raises:
            ExchangeError: one of the seven classes, and only those.
        """

    async def candidate_symbols(self) -> Sequence[str]:
        """The symbols worth asking about, for a venue that `requires_symbol`.

        The discovery hook: a venue that can only list fills per symbol needs a list of
        symbols to ask about, and where that list comes from -- balances, a symbols
        endpoint, an order history -- is the venue's business. A venue that does not need
        it returns an empty sequence. In the protocol now so that #14 does not change a
        contract #13 already implements.

        Raises:
            ExchangeError: one of the seven classes, and only those.
        """


def clamp_to_retention(
    requested_since: datetime,
    *,
    now: datetime,
    capabilities: ExchangeCapabilities,
) -> RetentionClamp:
    """Move a request that reaches past the venue's retention to the edge, plus a margin.

    `effective_since = max(requested_since, now - retention + RETENTION_MARGIN)`, and
    `requested_since` untouched for a venue that keeps everything. The margin moves the
    edge *forward*, inside what the venue still holds, so the oldest request is not refused
    for having aged past the edge between being computed and landing.

    **It never raises for a request older than retention.** Surfacing `effective_since`
    beside what was asked is #13's criterion, and this is where it is decided once: the
    owner asked for history from a date, the venue cannot give it, and the honest answer is
    both dates -- not an error and not a silent truncation.

    **`effective_since` is never after `now`.** A venue whose retention is shorter than the
    margin would otherwise place the oldest safe request in the future; it is held at `now`
    instead -- nothing old enough to ask about -- so the result can always open a
    `FillWindow` ending at `now`, or be seen to have nothing to open one over.

    **`effective_since` is always a whole millisecond**, floored after everything above,
    so it can open a `FillWindow` as it stands. Flooring asks for slightly more history,
    never less, and moves the instant by under a millisecond -- well inside
    `RETENTION_MARGIN`, so a floored edge is still inside what the venue keeps. A floor is
    not a clamp: `clamped` stays `False` for a request that was only floored, because
    `clamped` means the request was moved *forward* and history the owner asked for is
    not being fetched.

    `now` is an argument, not a clock read, so this is pure and the margin's direction is
    testable to the second.

    Raises:
        ValueError: either instant is naive, or `requested_since` is after `now`.
    """
    _require_aware(requested_since, field="requested_since")
    _require_aware(now, field="now")
    if requested_since > now:
        message = "requested_since must not be after now"
        raise ValueError(message)
    if capabilities.retention is None:
        effective_since = requested_since
    else:
        oldest = now - capabilities.retention + RETENTION_MARGIN
        effective_since = min(max(requested_since, oldest), now)
    return RetentionClamp(
        requested_since=requested_since,
        effective_since=floor_to_millisecond(effective_since),
    )


def floor_to_millisecond(moment: datetime) -> datetime:
    """`moment` moved back to the start of the millisecond it falls in. Build window bounds with it.

    Milliseconds are the granularity of this seam -- venues are asked and answer in epoch
    milliseconds -- and `FillWindow` refuses a bound off that grid. Flooring rather than
    rounding, because for the start of a window it asks for slightly more and never less,
    and the page check refuses nothing a venue could correctly return.

    The grid is measured from `EPOCH` in absolute time, so the result is on it whatever the
    offset of `moment`'s zone. The zone itself is kept: this moves an instant, it does not
    convert one.

    Raises:
        ValueError: `moment` is naive.
    """
    _require_aware(moment, field="moment")
    return moment - (moment - EPOCH) % _ONE_MILLISECOND


def assemble_fill_page(
    window: FillWindow,
    fills: Sequence[NormalizedFill],
    *,
    capabilities: ExchangeCapabilities,
    cursor: str | None,
    next_cursor: str | None,
    symbol: str | None,
) -> FillPage:
    """Turn what a provider parsed into the page the contract promises, or refuse it.

    The `align_balances` of this seam. Each row is a decision:

    | Case | Outcome |
    |---|---|
    | the window is longer than `max_query_window` | `ValueError` -- the caller's mistake |
    | `symbol` given and not required, or missing and required | `ValueError` |
    | a fill executed outside `[since, until)` | `ExchangeSchemaError` |
    | `symbol` given and a fill is for another symbol | `ExchangeSchemaError` |
    | two fills in the page share an `external_trade_id` | `ExchangeSchemaError` |
    | more fills than `page_size` | `ExchangeSchemaError` |
    | `cursor` or `next_cursor` that does not encode as UTF-8 | `ExchangeSchemaError` |
    | `next_cursor` equal to `cursor` (and not `None`) | `ExchangeSchemaError` |

    The first two are `ValueError` because the caller built the request wrongly; the rest
    are the venue answering something other than what was asked -- a fill outside the
    window, or for a symbol nobody asked about, is an answer to a different question, and
    dropping it quietly would hide a paging or correlation bug behind a history that still
    looks plausible.

    The symbol is compared exactly, in the venue's spelling: the provider passes the
    symbol it asked with and builds each fill's `symbol` from the same venue's response, so
    the two are the same string or the venue answered about something else.

    **No message names a trade id, a symbol, a cursor or an amount.** Counts and field
    names are enough to act on.

    Returns:
        The page, with `fills` as a tuple in the order the provider gave them.
    """
    if window.duration > capabilities.max_query_window:
        message = "The window is longer than the venue's max_query_window."
        raise ValueError(message)
    if symbol is not None and not capabilities.requires_symbol:
        message = "A symbol was given for a venue that does not require one."
        raise ValueError(message)
    if symbol is None and capabilities.requires_symbol:
        message = "This venue requires a symbol and none was given."
        raise ValueError(message)
    if len(fills) > capabilities.page_size:
        detail = (
            f"the page carries {len(fills)} fills, more than the declared page size of "
            f"{capabilities.page_size}"
        )
        raise ExchangeSchemaError(detail)
    outside = sum(1 for fill in fills if not window.contains(fill.executed_at))
    if outside:
        detail = f"{outside} fill(s) have an executed_at outside the requested window"
        raise ExchangeSchemaError(detail)
    if symbol is not None:
        elsewhere = sum(1 for fill in fills if fill.symbol != symbol)
        if elsewhere:
            detail = f"{elsewhere} fill(s) are for a symbol other than the one requested"
            raise ExchangeSchemaError(detail)
    distinct = len({fill.external_trade_id for fill in fills})
    if distinct != len(fills):
        detail = (
            f"the page carries {len(fills)} fills but only {distinct} distinct "
            "external_trade_id values"
        )
        raise ExchangeSchemaError(detail)
    for name, value in (("cursor", cursor), ("next_cursor", next_cursor)):
        if value is not None:
            _require_utf8(value, field=name)
    require_cursor_advanced(cursor, next_cursor)
    return FillPage(
        window=window,
        fills=tuple(fills),
        cursor=cursor,
        next_cursor=next_cursor,
        symbol=symbol,
    )


def require_cursor_advanced(cursor: str | None, next_cursor: str | None) -> None:
    """Refuse a next cursor that is the cursor this page was fetched with.

    A venue that hands back the cursor it was given would page forever, each request
    returning the same page. This catches a repeat; **it cannot catch a cycle** between two
    or more cursors (A -> B -> A), which needs the history only the sync loop has -- #15
    owns that.

    `None` is never a repeat: it is how a venue says there is nothing further, and a first
    page with nothing after it has `None` on both sides.

    Raises:
        ExchangeSchemaError: `next_cursor` equals `cursor` and is not `None`. The message
            does not show the cursor, which may be a trade id.
    """
    if next_cursor is not None and next_cursor == cursor:
        detail = "the next page cursor is the cursor this page was fetched with"
        raise ExchangeSchemaError(detail)


def require_fill_amount(value: object, *, field: str) -> Decimal:
    """Turn what a venue put in an amount field into a `Decimal`, or refuse it.

    The parser-side boundary, and the counterpart of `prices.base.require_price`. Accepted:

    | Shape | Arrives as |
    |---|---|
    | a JSON string holding a decimal number | `str`, e.g. `"0.00012300"` |
    | a JSON number with a point or an exponent | `Decimal`, via `decode_json` |
    | a JSON integer | `int` |

    Refused with `ExchangeSchemaError`, naming `field` and never the value: a `bool` (an
    `int` subclass that would become one unit), a `float` (which cannot come out of
    `decode_json`, but could from a parser that built one some other way), a string that
    is not a plain decimal number, and a NaN or an infinity in any form.

    **The string must look like a number and nothing looser.** `Decimal()` accepts
    whitespace, underscores, Unicode digits and `NaN`; a venue sending any of those in an
    amount is sending something that is not an amount.

    **Nor may the number be absurdly long.** More than `MAX_AMOUNT_DIGITS` digits written
    out in full -- five thousand decimal places, or one digit a billion places from the
    point -- is refused here, as a schema error, so that nothing downstream spends its time
    on the number or fails on it with an exception outside the taxonomy. From here to
    `derive_quote_quantity` to `NormalizedFill`, every refusal is an `ExchangeSchemaError`.

    Sign and the column's scale are not checked here: `NormalizedFill` checks them, once,
    for every amount however it arrived.
    """
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        detail = f"{field} must be a number or a string carrying one, got {type(value).__name__}"
        raise ExchangeSchemaError(detail)
    if isinstance(value, str) and _DECIMAL_TEXT.match(value) is None:
        detail = f"{field} is a string that is not a decimal number"
        raise ExchangeSchemaError(detail)
    try:
        amount = Decimal(value)
    except InvalidOperation:
        # Reached only for an exponent past what `Decimal` can hold -- the pattern has
        # already refused every other malformed string.
        detail = f"{field} is a number this application cannot represent"
        raise ExchangeSchemaError(detail) from None
    if not amount.is_finite():
        detail = f"{field} must be a finite number"
        raise ExchangeSchemaError(detail)
    if _written_length(amount) > MAX_AMOUNT_DIGITS:
        detail = f"{field} has more than {MAX_AMOUNT_DIGITS} digits written out in full"
        raise ExchangeSchemaError(detail)
    return amount


def _written_length(amount: Decimal) -> int:
    """How many digits `amount` has in plain positional notation, both sides of the point.

    `0.00012300` is 8 (no integer digits, eight places), `1E+2` is 3, `12.5` is 3. Computed
    from the coefficient length and the exponent, so an exponent of a billion costs nothing
    to measure.
    """
    _, digits, exponent = amount.as_tuple()
    # An `int` on a finite Decimal; the caller has refused the other kinds.
    places = int(exponent)
    integer_digits = max(len(digits) + places, 0)
    fractional_digits = max(-places, 0)
    return integer_digits + fractional_digits


def derive_quote_quantity(quantity: Decimal, price: Decimal) -> Decimal:
    """`quantity * price` rounded to `FILL_SCALE`, for a venue that omitted the quote amount.

    Rounded, necessarily -- two amounts of eighteen places multiply to thirty-six -- and that
    is what `quote_quantity_derived` records. The product itself is exact
    (`domain.money.multiply`), so `quantize` is the one rounding and the calling thread's
    decimal context cannot introduce a second one.

    Raises:
        ExchangeSchemaError: the product has more than `MAX_FILL_INTEGER_DIGITS` digits
            before the point, so no column could hold it.
        TypeError: either argument is not a `Decimal` -- a provider bug; parse amounts with
            `require_fill_amount` first.
        ValueError: either argument is not finite.
    """
    try:
        return quantize(multiply(quantity, price), FILL_SCALE)
    except InvalidOperation:
        detail = (
            "quote_quantity, derived as quantity times price, has more than "
            f"{MAX_FILL_INTEGER_DIGITS} digits before the decimal point"
        )
        raise ExchangeSchemaError(detail) from None


def datetime_from_epoch_ms(value: object) -> datetime:
    """An epoch-millisecond timestamp as an aware UTC `datetime`, without a float.

    `EPOCH + timedelta(milliseconds=value)`, which is exact integer arithmetic.
    `datetime.fromtimestamp(ms / 1000)` is a float division and fails the ban -- and a
    float is how a fill executed on the last millisecond of a window lands in the next one.

    Accepts an `int` or a string of ASCII digits. Refused with `ExchangeSchemaError`,
    never quoting the value: a `bool`, a `float`, a `Decimal`, a negative number, a string
    with anything but digits in it, and an instant `datetime` cannot represent.
    """
    milliseconds: int | None = None
    if isinstance(value, bool):
        milliseconds = None
    elif isinstance(value, int):
        milliseconds = value
    elif isinstance(value, str) and _EPOCH_MS_TEXT.match(value) is not None:
        milliseconds = int(value)
    if milliseconds is None or milliseconds < 0:
        detail = (
            "a timestamp must be a non-negative whole number of epoch milliseconds, "
            f"as an int or a string of digits, got {type(value).__name__}"
        )
        raise ExchangeSchemaError(detail)
    try:
        return EPOCH + timedelta(milliseconds=milliseconds)
    except OverflowError:
        detail = "a timestamp is past the last instant this application can represent"
        raise ExchangeSchemaError(detail) from None


def epoch_ms(moment: datetime) -> int:
    """An aware `datetime` as whole epoch milliseconds, for building a request.

    `(moment - EPOCH) // timedelta(milliseconds=1)`: integer division of two durations,
    exact, and floored -- a sub-millisecond remainder is dropped, which for a window's
    `since` asks for slightly more and never less.

    Raises:
        ValueError: `moment` is naive. A naive instant is a guess about a timezone, and a
            request built from a guess reads the wrong hours of history.
    """
    _require_aware(moment, field="moment")
    return (moment - EPOCH) // timedelta(milliseconds=1)


def encode_raw_payload(document: object) -> str:
    """The venue's decoded fill object as canonical JSON, every `Decimal` intact.

    Canonical: keys sorted, no whitespace, strings escaped to ASCII -- so the same fill
    renders to the same text on every run and a stored payload can be compared byte for
    byte.

    **The invariant is that every `Decimal`'s sign, digits and exponent survive**, and so do
    the type and value of every other leaf: `decode_json(encode_raw_payload(d)) == d`, and
    each `Decimal` decoded back has the same `as_tuple()` as the one encoded. That is a
    promise about the number, not about the text the venue sent -- `0.00012300` comes back
    as `0.00012300` and `1E+2` as `1E+2`, but `1.5e1` and `15E0` decode to the same
    `Decimal("15")`, and it is written `15E0`. The `E0` is there because `str()` renders a
    zero exponent as a bare integer, which would decode as an `int`: equal in value, a
    different type. `json.dumps` cannot do any of this: it has no `Decimal` support, and the
    obvious workaround -- converting to `float` -- destroys the digits the payload is kept
    to preserve.

    **Pass the fill object, never the envelope or the request.** An envelope can carry a
    request echo, and a request carries a key and a signature.

    Raises:
        ExchangeSchemaError: containers nest more than `MAX_RAW_PAYLOAD_DEPTH` deep. How
            deep is the venue's choice, so it is the venue's error, and it is refused at the
            same depth on every platform rather than wherever the interpreter's recursion
            limit happens to fall.
        TypeError: anything `decode_json` cannot produce -- a `float`, a `set`, a `tuple`,
            a `datetime`, a non-finite `Decimal`, a non-string key, or a subclass of a
            JSON type. That is a provider bug, not a vendor's: the provider was meant to
            pass what the decoder gave it.
    """
    return "".join(_encode_json(document, depth=0))


def _encode_json(value: object, *, depth: int) -> Iterator[str]:
    """Render one decoded JSON value, `depth` containers down from the top.

    **Exact types, compared with `type(...) is`, not `isinstance`.** The decoder produces a
    `dict`, a `list`, a `str`, an `int`, a `Decimal`, a `bool` or `None` and never a
    subclass of any of them, so a `StrEnum`, an `OrderedDict` or an `IntEnum` here is
    something the provider built rather than something the venue sent -- and a `bool`,
    which `isinstance(value, int)` would accept, has to be told apart from an `int` anyway.
    """
    if type(value) is list or type(value) is dict:
        _require_shallow(depth + 1)
    if value is None:
        yield "null"
    elif type(value) is bool:
        yield "true" if value else "false"
    elif type(value) is int or type(value) is str:
        yield json.dumps(value)
    elif type(value) is Decimal:
        if not value.is_finite():
            message = "encode_raw_payload cannot render a non-finite Decimal as JSON"
            raise TypeError(message)
        text = str(value)
        # `str()` writes a Decimal whose exponent is zero -- `1.5e1` decodes to one -- as a
        # bare integer, `15`, which decodes back as the `int` 15: equal, but no longer a
        # Decimal. `15E0` is a valid JSON number that decodes to exactly `Decimal("15")`,
        # coefficient and exponent both, so the round trip keeps the type as well as the
        # value. Every other exponent already renders with a point or an `E`.
        yield f"{text}E0" if value.as_tuple().exponent == 0 else text
    elif type(value) is list:
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ","
            yield from _encode_json(item, depth=depth + 1)
        yield "]"
    elif type(value) is dict:
        yield from _encode_object(value, depth=depth + 1)
    else:
        message = (
            f"encode_raw_payload cannot render a {type(value).__name__}: only what "
            "decode_json produces is accepted"
        )
        raise TypeError(message)


def _encode_object(document: dict[object, object], *, depth: int) -> Iterator[str]:
    """Render a JSON object, `depth` containers deep, keys sorted. Every key is a `str`."""
    keys: list[str] = []
    for key in document:
        if type(key) is not str:
            message = "encode_raw_payload requires every object key to be a str"
            raise TypeError(message)
        keys.append(key)
    yield "{"
    for index, key in enumerate(sorted(keys)):
        if index:
            yield ","
        yield json.dumps(key)
        yield ":"
        yield from _encode_json(document[key], depth=depth)
    yield "}"


def _require_shallow(depth: int) -> None:
    """Refuse a container nested deeper than `MAX_RAW_PAYLOAD_DEPTH`.

    Checked before the container is opened, so a document a thousand levels deep costs
    thirty-three frames to refuse rather than a `RecursionError` to discover.
    """
    if depth > MAX_RAW_PAYLOAD_DEPTH:
        detail = (
            f"the fill object nests more than {MAX_RAW_PAYLOAD_DEPTH} levels deep, which is "
            "too deep to record"
        )
        raise ExchangeSchemaError(detail)


def _require_storable_amount(value: object, *, field: str, positive: bool) -> None:
    """Refuse an amount `NumericText(FILL_SCALE)` could not store exactly as it is.

    One `quantize` answers both scale questions, by the same rule the column uses: a
    `decimal.InvalidOperation` means more integer digits than the scale leaves room for,
    and a result that differs from the input means fractional digits the column would
    round away. Comparing the values rather than counting digits is what lets trailing
    zeros through: `1.50000000000000000000` has twenty places and loses nothing.
    """
    if not isinstance(value, Decimal):
        detail = f"{field} must be a Decimal, got {type(value).__name__}"
        raise ExchangeSchemaError(detail)
    if not value.is_finite():
        detail = f"{field} must be a finite number"
        raise ExchangeSchemaError(detail)
    if positive and value <= 0:
        detail = f"{field} must be greater than zero"
        raise ExchangeSchemaError(detail)
    try:
        stored = quantize(value, FILL_SCALE)
    except InvalidOperation:
        detail = f"{field} has more than {MAX_FILL_INTEGER_DIGITS} digits before the decimal point"
        raise ExchangeSchemaError(detail) from None
    if stored != value:
        detail = (
            f"{field} has more than {FILL_SCALE} decimal places, and storing it would round "
            "it rather than keep it as reported"
        )
        raise ExchangeSchemaError(detail)


def _require_side(value: object) -> None:
    """Refuse a side that is not a `FillSide` -- a plain `"buy"` string included.

    Takes `object` so the check is not statically dead, for the reason
    `providers.base._require_base_units` gives: the annotation is a promise `mypy` keeps
    for our code and nobody keeps for a value a parser built at run time.
    """
    if not isinstance(value, FillSide):
        detail = "side must be a FillSide"
        raise ExchangeSchemaError(detail)


def _require_flag(value: object, *, field: str) -> None:
    """Refuse a flag that is not exactly a `bool`.

    `type(...) is bool` rather than `isinstance`, which is the same test for a `bool` and
    lets nothing else through: a `1` from a parser that copied a venue's integer flag would
    satisfy the column's `CHECK (... IN (0, 1))` and still be the wrong type on the fill.
    """
    if type(value) is not bool:
        detail = f"{field} must be a bool"
        raise ExchangeSchemaError(detail)


def _require_text(value: object, *, field: str) -> None:
    """Refuse a field that is not a non-blank string of UTF-8-encodable text.

    Names the field, never the value.
    """
    if not isinstance(value, str) or not value.strip():
        detail = f"{field} must be a non-empty string"
        raise ExchangeSchemaError(detail)
    _require_utf8(value, field=field)


def _require_utf8(value: object, *, field: str) -> None:
    """Refuse text that cannot be encoded as UTF-8 -- in practice, a lone surrogate.

    **`"\\ud800"` is valid JSON** (RFC 8259 escapes code units, not code points, so an
    unpaired surrogate escape parses), and `json.loads` hands it back as a Python `str`
    that passes every check a string can be put through -- until something encodes it.
    That something is the database driver inserting the fill, or the signing helper
    building the next request from a cursor, and it fails there with a bare
    `UnicodeEncodeError`: outside the taxonomy, far from the venue that sent it, and in the
    case of a cursor, halfway through signing a request. Refused here instead, as the
    schema error it is. The message names the field; the text itself is exactly what
    cannot be rendered.
    """
    if not isinstance(value, str):
        detail = f"{field} must be a string"
        raise ExchangeSchemaError(detail)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        detail = f"{field} does not encode as UTF-8"
        raise ExchangeSchemaError(detail) from None


def _is_aware(value: object) -> bool:
    """Whether `value` is a `datetime` that can be placed in time."""
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _require_aware(value: object, *, field: str) -> None:
    """Refuse a naive datetime, or anything that is not a datetime, with a `ValueError`."""
    if not _is_aware(value):
        message = f"{field} must be a timezone-aware datetime"
        raise ValueError(message)


def _require_whole_millisecond(moment: datetime, *, field: str) -> None:
    """Refuse an instant that is not on the millisecond grid measured from `EPOCH`."""
    if (moment - EPOCH) % _ONE_MILLISECOND:
        message = (
            f"{field} must be a whole millisecond, the granularity venues are asked in; "
            "build it with floor_to_millisecond"
        )
        raise ValueError(message)


def _require_millisecond_duration(duration: timedelta, *, field: str) -> None:
    """Refuse a duration that is not a positive whole number of milliseconds."""
    if duration <= timedelta(0) or duration % _ONE_MILLISECOND:
        message = f"{field} must be a positive duration of a whole number of milliseconds"
        raise ValueError(message)


def _require_positive_int(value: object, *, field: str) -> None:
    """Refuse a count that is not a whole number of at least one. Names the field."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        message = f"{field} must be an int of at least 1"
        raise ValueError(message)


def _require_rate_limit(value: object) -> None:
    """Refuse a rate limit that is not a `RateLimit`, which has already validated itself."""
    if not isinstance(value, RateLimit):
        message = f"rate_limit must be a RateLimit, got {type(value).__name__}"
        raise TypeError(message)
