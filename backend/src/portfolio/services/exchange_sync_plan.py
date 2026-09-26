"""The arithmetic of an exchange sync: which windows to read, in what order, and how long to wait.

Pure functions and constants. **No I/O, no clock, no session**: `now` is an argument, so every
bound here is testable to the millisecond, and `services/exchange_sync.py` is the only caller.
It imports `providers.exchanges.base` for `FillWindow` and the clamp, which is why this module
is on the write side of the `api-never-reaches-an-exchange-provider` contract and nothing a
router imports may import it.

## Every bound is a whole millisecond

Venues are asked and answer in epoch milliseconds, and `FillWindow` refuses a bound off that
grid (spec 012's lesson: two helpers on different grids refused a correct page at the same
boundary every run). `now` is floored with `floor_to_millisecond` before anything is built
from it, the clamp is already floored, and every duration here is a whole number of
milliseconds -- so every sum stays on the grid.

## Newest first

A window list comes back newest first, because the recent end is what the owner is looking
for and because a backfill interrupted by a restart has then already stored the part that
matters most. Within one range the oldest window is the one that may be shorter.

## Contiguity holds by construction

The planned history of an account is one interval, `[effective_since, planned_until)`, and
every range planned here touches it: the top overlaps it by `OVERLAP`, the bottom abuts it.
So the union of done and pending windows is always one interval, and the done part is
complete once the queue is empty. The one exception is a stall longer than the venue's
retention, which leaves a hole no request can fill; `plan_account` says what it does then.

## What is confirmed and what is assumed

Nothing here is vendor-specific. `OVERLAP`, `RETENTION_STEP` and the rate-limit numbers are
choices, and `RETENTION_STEP` is a guess recorded as one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from portfolio.providers.exchanges.base import CursorKind, FillWindow, floor_to_millisecond

if TYPE_CHECKING:
    from collections.abc import Sequence

    from portfolio.providers.exchanges.base import RetentionClamp

__all__ = [
    "HISTORY_GENESIS",
    "MAX_RATE_LIMIT_WAIT_SECONDS",
    "MAX_RETENTION_STEPS",
    "OVERLAP",
    "RATE_LIMIT_RETRIES",
    "RETENTION_STEP",
    "AccountPlan",
    "MovedSince",
    "NormalisedQueue",
    "PendingWindow",
    "Replacement",
    "cursor_survives_a_moved_since",
    "normalise_pending",
    "plan_account",
    "seconds_to_wait",
    "split_in_half",
    "split_newest_first",
]

HISTORY_GENESIS: Final = datetime(2009, 1, 3, tzinfo=UTC)
"""What "all of it" means when no history start is configured: the Bitcoin genesis block.

Earlier than any venue's history, so the retention clamp decides where a request really
starts. A date rather than "the epoch" so that `requested_since` reads as a choice.
"""

OVERLAP: Final = timedelta(minutes=5)
"""How far each new top window reaches back into history already planned.

A fill a venue records a moment after the instant it happened -- settlement, replication
lag, a clock a little behind ours -- lands in a window that was read before it appeared.
Re-reading the last five minutes of the previous plan catches it, and costs nothing: the
unique constraint makes the re-read insert zero rows.
"""

RETENTION_STEP: Final = timedelta(days=1)
"""How far a window's `since` moves after the venue refuses it as too old. **A guess.**

`clamp_to_retention` keeps requests `RETENTION_MARGIN` inside the declared retention, so a
refusal means the venue's real retention is shorter than declared -- Bitget's "the last
three months" may be 89 days against a declared 90. A day is the granularity such a
difference is likely to have. The owner's first real sync is the first measurement.
"""

MAX_RETENTION_STEPS: Final = 3
"""How many times one window may be stepped forward in one run before the account fails.

Three days past the declared retention is already a venue that does not keep what it says;
stepping further would hide that behind a history quietly growing shorter every run.
"""

RATE_LIMIT_RETRIES: Final = 3
"""Retries of one request after a rate limit: four attempts in all."""

MAX_RATE_LIMIT_WAIT_SECONDS: Final = 60
"""The longest wait for a rate limit this sync sits through, in whole seconds.

A venue asking for more than a minute is not throttling a burst, it is refusing the account
for a while. The account fails with `rate_limited` for this run and the next scheduled one
tries again, rather than holding the coordinator -- and a manual sync joined to it -- for as
long as the venue likes.
"""

_ONE_MILLISECOND: Final = timedelta(milliseconds=1)
_MILLISECONDS_PER_SECOND: Final = 1000
_MIN_SPLITTABLE_MILLISECONDS: Final = 2


@dataclass(frozen=True, slots=True)
class AccountPlan:
    """The windows to add to an account's queue, and the planned history once they are.

    `windows` is newest first, the top range's before the bottom range's. Empty when there is
    nothing new to ask about -- the clock has not moved, or it was stepped back.
    """

    windows: tuple[FillWindow, ...]
    effective_since: datetime
    planned_until: datetime


@dataclass(frozen=True, slots=True)
class PendingWindow:
    """A queued window as this module sees it: its identity, its range, what it carries.

    `symbol` and `cursor` are carried, not used: they are what the caller needs to write a
    change back. A replacement always starts from its first page; a window whose `since` only
    moved keeps its cursor when the venue's cursor kind allows -- see `normalise_pending`.
    """

    window_id: int
    window: FillWindow
    symbol: str | None
    cursor: str | None


@dataclass(frozen=True, slots=True)
class Replacement:
    """A queued window and the windows that replace it. `windows == ()` means it is dropped."""

    original: PendingWindow
    windows: tuple[FillWindow, ...]


@dataclass(frozen=True, slots=True)
class MovedSince:
    """A queued window whose `since` moves up to `since` and **nothing else changes**.

    The row is updated in place, so it keeps its id. Whether it keeps its cursor too is the
    caller's decision, by `cursor_survives_a_moved_since` for the venue's cursor kind.
    """

    original: PendingWindow
    since: datetime


@dataclass(frozen=True, slots=True)
class NormalisedQueue:
    """The queue after `normalise_pending`: what stays, what changes, and whether any was lost.

    `moved` holds the windows whose `since` alone moved up; `replaced` the windows dropped or
    re-split. `truncated` is true when a window was dropped or had its `since` moved up --
    history the plan held has aged out of what the venue keeps. A re-split alone loses nothing
    and is not truncation.
    """

    kept: tuple[PendingWindow, ...]
    moved: tuple[MovedSince, ...]
    replaced: tuple[Replacement, ...]
    truncated: bool


def split_newest_first(
    since: datetime,
    until: datetime,
    *,
    max_window: timedelta,
) -> tuple[FillWindow, ...]:
    """`[since, until)` as windows laid end to end, each at most `max_window`, newest first.

    Cut from the newest end, so every window is exactly `max_window` long except the oldest,
    which may be shorter. `()` when `since >= until`: an empty range is not an error, it is
    nothing to ask about.

    Raises:
        ValueError: a bound is naive or not a whole millisecond (from `FillWindow`), or
            `max_window` is not a positive whole number of milliseconds.
    """
    _require_positive_millisecond_duration(max_window, field="max_window")
    windows: list[FillWindow] = []
    upper = until
    while upper > since:
        lower = max(since, upper - max_window)
        windows.append(FillWindow(since=lower, until=upper))
        upper = lower
    return tuple(windows)


def plan_account(
    *,
    requested_since: datetime | None,
    effective_since: datetime | None,
    planned_until: datetime | None,
    clamp: RetentionClamp,
    now: datetime,
    max_window: timedelta,
) -> AccountPlan:
    """What to add to an account's queue this run, and its planned history afterwards.

    `requested_since`, `effective_since` and `planned_until` are what the account recorded at
    its last plan; `clamp` is this run's, built from what the owner asks for now. `now` is
    floored to the millisecond here.

    **Two arguments the spec's signature did not list**, both needed for the plan to be
    right: `max_window`, because the plan is persisted before any fetch and must already be
    split; and the recorded `requested_since`, because it is the only way to tell "the owner
    asked for more history" from "the floor sits above the declared retention edge" -- see
    the bottom, below.

    * **First sync** (`effective_since` or `planned_until` `None`):
      `[clamp.effective_since, now)`.
    * **Later syncs**, newest first:
      * the **top**, `[max(planned_until - OVERLAP, clamp.effective_since), now)`, only when
        `now > planned_until`;
      * the **bottom**, `[clamp.effective_since, effective_since)`, only when the owner
        **moved the history start earlier** -- `clamp.requested_since` before the recorded
        `requested_since` -- and the clamp still reaches below the recorded floor.
    * `effective_since` becomes the earlier of the floor and the bottom's start,
      `planned_until` the later of the two ceilings.
    * **A clock behind the plan** (`now < planned_until`) plans nothing, and **pulls
      `planned_until` back to `now`** -- never below `effective_since`.

    **Why the ceiling is pulled back rather than kept.** A clock that once ran ahead leaves a
    `planned_until` in the future. Kept, it would plan no top until real time caught up with
    it, and every fill in between would fall into a range the plan claims to have read: the
    account reports `ok`, and the trades are silently missing. Pulled back, the next run's top
    starts at the corrected ceiling minus `OVERLAP` and re-covers everything.

    That is right for a **signed venue**, which refuses every request made while the clock is
    ahead -- Bitget's timestamp window is thirty seconds -- so the ahead run read nothing past
    real time, and a pending window it planned into the future is harmless: once the clock is
    corrected, reading it returns what exists and the next top re-covers the rest. Two
    residuals, stated rather than hidden:

    * an **unsigned venue** that answers while the clock is ahead reads windows that end in
      the future; pulling the ceiling back then costs a re-read of what it already read, and
      nothing else, because the constraint makes the re-read insert nothing;
    * a **stale clock running behind** real time pulls the ceiling back to a moment the venue
      has already passed, and the next run re-reads from there. That, too, costs only a
      re-read -- unless the clock is behind by more than the venue's retention, when the next
      top starts past the pulled-back ceiling and the hole rule below moves the floor up.

    **Why the bottom needs the owner to have asked.** The spec states the bottom "only
    happens when the owner moves the history start earlier", and under a rolling retention
    that is true of the clamp alone -- the edge only ever moves forward -- except after a
    retention step. When the venue refuses a window as too old, the sync steps the window
    forward and raises `effective_since` above the declared edge. Planning a bottom whenever
    the clamp reached below the floor would then re-plan, on every run, exactly the history
    the venue had just refused. A recorded `requested_since` of `None` alongside a recorded
    floor cannot be written by this application, and is treated as "not moved".

    **Retention passing the floor does not move it.** History already held stays held when
    the rolling retention passes it; `effective_since` is where the held history starts, not
    where the venue's does. The one case it moves forward is a **hole**: when
    `clamp.effective_since` is after `planned_until` -- the account stalled for longer than
    the venue keeps -- nothing can fill `[planned_until, clamp.effective_since)`, and
    `[effective_since, planned_until)` would otherwise claim a complete history across it.
    The floor then becomes `clamp.effective_since`, and `history_truncated` says so. That
    rule is not in the spec; it is the reading of "the floor of the planned history" that
    keeps the claim true.

    Raises:
        ValueError: an instant is naive or off the millisecond grid, or `max_window` is not a
            positive whole number of milliseconds.
    """
    ceiling = floor_to_millisecond(now)
    edge = clamp.effective_since
    if effective_since is None or planned_until is None:
        return AccountPlan(
            windows=split_newest_first(edge, ceiling, max_window=max_window),
            effective_since=edge,
            planned_until=max(edge, ceiling),
        )
    if ceiling < planned_until:
        # The clock is behind the plan: see "Why the ceiling is pulled back" above. Never below
        # the floor, so `[effective_since, planned_until)` stays a range rather than inverting.
        return AccountPlan(
            windows=(),
            effective_since=effective_since,
            planned_until=max(ceiling, effective_since),
        )

    top: tuple[FillWindow, ...] = ()
    if ceiling > planned_until:
        top = split_newest_first(max(planned_until - OVERLAP, edge), ceiling, max_window=max_window)
    new_floor = effective_since
    bottom: tuple[FillWindow, ...] = ()
    asked_for_more = requested_since is not None and clamp.requested_since < requested_since
    if asked_for_more and edge < effective_since:
        bottom = split_newest_first(edge, effective_since, max_window=max_window)
        new_floor = edge
    if edge > planned_until:
        new_floor = edge
    return AccountPlan(
        windows=top + bottom,
        effective_since=new_floor,
        planned_until=max(planned_until, ceiling),
    )


def normalise_pending(
    windows: Sequence[PendingWindow],
    *,
    floor: datetime,
    max_window: timedelta,
) -> NormalisedQueue:
    """Re-clamp a queue to the current retention floor and the venue's current window limit.

    Each window, in the order given:

    | Window | Becomes |
    |---|---|
    | wholly older than `floor` (`until <= floor`) | `replaced` by nothing: its history aged out |
    | partly older (`since < floor < until`), within `max_window` | `moved`: `since` up to `floor` |
    | longer than `max_window`, its `since` moved or not | `replaced` by a newest-first re-split |
    | anything else | `kept` as it is, cursor and all |

    **A replaced window restarts from its first page**: its replacements are new windows, and
    a cursor describes a position within one window. **A moved window keeps its row**, and the
    caller keeps its cursor exactly when `cursor_survives_a_moved_since` says the venue's kind
    allows. That matters for the oldest window of a backfill: the rolling retention floor
    passes its `since` by the next run, every run, so restarting it from page one would make
    an interrupted backfill re-read its oldest window in full.

    Raises:
        ValueError: `floor` is naive or off the millisecond grid, or `max_window` is not a
            positive whole number of milliseconds.
    """
    _require_positive_millisecond_duration(max_window, field="max_window")
    kept: list[PendingWindow] = []
    moved: list[MovedSince] = []
    replaced: list[Replacement] = []
    truncated = False
    for pending in windows:
        window = pending.window
        if window.until <= floor:
            replaced.append(Replacement(original=pending, windows=()))
            truncated = True
            continue
        since = window.since
        if since < floor:
            since = floor
            truncated = True
        if window.until - since > max_window:
            replaced.append(
                Replacement(
                    original=pending,
                    windows=split_newest_first(since, window.until, max_window=max_window),
                )
            )
        elif since != window.since:
            moved.append(MovedSince(original=pending, since=since))
        else:
            kept.append(pending)
    return NormalisedQueue(
        kept=tuple(kept),
        moved=tuple(moved),
        replaced=tuple(replaced),
        truncated=truncated,
    )


def cursor_survives_a_moved_since(cursor_kind: CursorKind) -> bool:
    """Whether a window's cursor still holds after only its `since` moved forward.

    **Yes for the two trade-id kinds.** A `TRADE_ID_BEFORE` or `TRADE_ID_AFTER` cursor is a
    bound on trade ids -- "older than id X", "newer than id X" -- and it means the same thing
    whatever time range it is combined with. Moving `since` forward only narrows the range the
    venue filters by, so the pages still to come are the same pages, minus the fills that are
    now outside it.

    **No for `TIME` and `NONE`.** A time cursor is a position in the very range that moved,
    and whether it still lies inside it, or means the same thing once the range changed, is an
    assumption about a venue this application does not have; `NONE` has no cursor to keep.
    Restarting costs a re-read, which the unique constraint makes free.
    """
    return cursor_kind in {CursorKind.TRADE_ID_BEFORE, CursorKind.TRADE_ID_AFTER}


def split_in_half(window: FillWindow) -> tuple[FillWindow, FillWindow]:
    """`(newer, older)`: the window cut at its midpoint, floored to a whole millisecond.

    For a venue with no cursor (`CursorKind.NONE`), whose full page means the window held
    more than one page: each half is read on its own, the newer first. Integer milliseconds
    throughout, so the midpoint is on the grid and no `float` is involved.

    Raises:
        ValueError: the window is shorter than two milliseconds, so a half would be empty.
            The sync turns that into a schema error: the venue returned a full page for a
            window no split can make smaller.
    """
    milliseconds = window.duration // _ONE_MILLISECOND
    if milliseconds < _MIN_SPLITTABLE_MILLISECONDS:
        message = "A window shorter than two milliseconds cannot be split in half."
        raise ValueError(message)
    midpoint = window.since + timedelta(milliseconds=milliseconds // 2)
    return (
        FillWindow(since=midpoint, until=window.until),
        FillWindow(since=window.since, until=midpoint),
    )


def seconds_to_wait(retry_after_ms: int | None, *, attempt: int) -> int | None:
    """How long to wait before retrying a rate-limited request, in whole seconds, or `None`.

    The venue's `retry_after_ms` when it gave one, otherwise `2 ** attempt`, where `attempt`
    is the number of the retry about to be made, from 1: two, four, then eight seconds.
    **Rounded up**: waking a fraction of a second early is a request inside the window the
    venue asked us to stay out of.

    `None` means "do not wait": the wait is over `MAX_RATE_LIMIT_WAIT_SECONDS`, and the
    account stops for this run instead. A wait of exactly the cap is waited.

    Raises:
        ValueError: `attempt` is negative, or `retry_after_ms` is negative.
    """
    if attempt < 0:
        message = "attempt must not be negative"
        raise ValueError(message)
    if retry_after_ms is None:
        # `1 << attempt` is `2 ** attempt` for the non-negative `attempt` checked above, and
        # is typed `int`: `int ** int` is not, because a negative exponent makes a float.
        seconds = 1 << attempt
    else:
        if retry_after_ms < 0:
            message = "retry_after_ms must not be negative"
            raise ValueError(message)
        seconds = -(-retry_after_ms // _MILLISECONDS_PER_SECOND)
    if seconds > MAX_RATE_LIMIT_WAIT_SECONDS:
        return None
    return seconds


def _require_positive_millisecond_duration(duration: timedelta, *, field: str) -> None:
    """Refuse a duration that is not a positive whole number of milliseconds."""
    if duration <= timedelta(0) or duration % _ONE_MILLISECOND:
        message = f"{field} must be a positive duration of a whole number of milliseconds"
        raise ValueError(message)
