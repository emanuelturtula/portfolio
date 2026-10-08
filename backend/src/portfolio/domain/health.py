"""How the application's own sources stand: the rules behind `GET /api/health/detail` (#23).

Pure, like the rest of `domain`: nothing here reads a clock, a table or a timer. The callers
(`services/scheduler.py`, `services/health.py`) hand in instants and what they read, and act on
what comes back, which is what lets every state be tested with literals (spec 030).

## Every state is its wire form

Each enum here is a `StrEnum` whose members are the strings `GET /api/health/detail` serves, so
the generated TypeScript types are unions of exactly these strings and the Health page's
wording tables can be total over them.

## What is judged here, and what is not

* **A timer** (`scheduler_state`): `stopped` when its task is not running, `late` when its
  last finished tick -- or, before the first, its start -- is more than `LATE_AFTER_INTERVALS`
  intervals old, and `ok` otherwise. `disabled`, a timer the settings never built, is the
  service's answer: there is no timer to judge.
* **A source's last attempt** (`source_state`): `ok`, `failing` or `never`.
* **The prices** (`price_state`): `fresh`, `stale` past the age limit the caller hands in, or
  `never`.

`unavailable` is never an outcome of a rule here. It is what the service serves for a section
whose read raised, so it is a member of the section enums and no function returns it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from datetime import datetime, timedelta

__all__ = [
    "LATE_AFTER_INTERVALS",
    "PriceHealthState",
    "SchedulerName",
    "SchedulerState",
    "SectionState",
    "SourceState",
    "price_state",
    "scheduler_state",
    "source_state",
]

LATE_AFTER_INTERVALS: Final = 2
"""How many intervals a running timer may go without finishing a tick before it is `late`.

Two, because the first tick after a start may wait up to one interval (the scheduler sleeps
what is left of the last one), and a tick then takes time of its own. A tick in flight for
longer than two intervals, counted from its own start, is late as well, which is the point: a
tick that never returns is a timer that has stopped working while its task still runs.
"""


class SchedulerName(StrEnum):
    """The five timers, by the names their log lines and task names already carry."""

    BALANCE_SYNC = "balance-sync"
    PRICE_REFRESH = "price-refresh"
    PRICE_BACKFILL = "price-backfill"
    BALANCE_REBUILD = "balance-rebuild"
    BACKUP = "backup"


class SchedulerState(StrEnum):
    """How one timer stands. The member is its wire form.

    * `ok` -- running, and not late.
    * `late` -- running, and more than `LATE_AFTER_INTERVALS` intervals old: the tick in flight
      since it started, or otherwise its last finished tick, or its start before the first.
    * `stopped` -- the timer was built and its task is not running.
    * `disabled` -- the settings switched the timer off, so it was never built.
    """

    OK = "ok"
    LATE = "late"
    STOPPED = "stopped"
    DISABLED = "disabled"


class SectionState(StrEnum):
    """Whether a section of the health detail could be read. The member is its wire form.

    `unavailable` means its read raised: the log has `health_section_failed` naming it, and
    every other field of the section is null or empty.
    """

    OK = "ok"
    UNAVAILABLE = "unavailable"


class SourceState(StrEnum):
    """What a source's last recorded attempt says. The member is its wire form.

    * `ok` -- the last attempt succeeded.
    * `failing` -- the last attempt failed, or succeeded only in part.
    * `never` -- no attempt is recorded.
    """

    OK = "ok"
    FAILING = "failing"
    NEVER = "never"


class PriceHealthState(StrEnum):
    """How the stored prices stand. The member is its wire form.

    * `fresh` -- the newest price row is at most the age limit old.
    * `stale` -- it is older than that: the refresh has stopped writing rows.
    * `never` -- no price row exists.
    * `unavailable` -- the read raised.
    """

    FRESH = "fresh"
    STALE = "stale"
    NEVER = "never"
    UNAVAILABLE = "unavailable"


def scheduler_state(
    *,
    running: bool,
    interval: timedelta,
    started_at: datetime | None,
    last_tick_started_at: datetime | None,
    last_tick_finished_at: datetime | None,
    now: datetime,
) -> SchedulerState:
    """`stopped`, `late` or `ok`, as of `now`. Never `disabled`: that timer does not exist.

    `started_at` is when the loop was last started, and `None` for a timer that never was,
    which is `stopped`. Otherwise a timer is `late` when `now` is more than
    `LATE_AFTER_INTERVALS` intervals past a reference instant, which has two cases (R13, S4):

    * **a tick in flight** -- one has started, and none has finished since: the instant it
      started;
    * **otherwise**: the instant the last tick finished, or the start before the first tick.

    The loop sleeps an interval *after* a tick finishes, so measuring a tick in flight from the
    last finish made it late one interval into its run rather than two. Aware datetimes,
    compared in Python.

    **In flight is judged by the wall clock** (R16): the scheduler records instants from the
    wall clock, not a flag, and a tick is in flight when its start is later than the last
    finish. A clock stepped back between a finish and the next start can record that start
    before the finish -- or at the same instant. The tick is then measured from the finish,
    which is later than its start by less than the step, so it turns `late` that much later
    than it should, and never sooner. A reference after `now` -- the clock stepped back since
    it was recorded -- is not late.
    """
    if not running or started_at is None:
        return SchedulerState.STOPPED
    if last_tick_started_at is not None and (
        last_tick_finished_at is None or last_tick_started_at > last_tick_finished_at
    ):
        reference = last_tick_started_at
    else:
        reference = last_tick_finished_at if last_tick_finished_at is not None else started_at
    if now - reference > interval * LATE_AFTER_INTERVALS:
        return SchedulerState.LATE
    return SchedulerState.OK


def source_state(succeeded: bool | None) -> SourceState:
    """The state of a source whose last attempt `succeeded`, or `None` when none is recorded."""
    if succeeded is None:
        return SourceState.NEVER
    return SourceState.OK if succeeded else SourceState.FAILING


def price_state(
    latest_fetched_at: datetime | None,
    *,
    now: datetime,
    stale_after: timedelta,
) -> PriceHealthState:
    """`never` with no price row, `stale` when the newest is older than `stale_after`, else
    `fresh`. The comparison is `now - latest_fetched_at > stale_after`, the rule a price itself
    is flagged by, so the section and the dashboard turn stale at the same instant."""
    if latest_fetched_at is None:
        return PriceHealthState.NEVER
    if now - latest_fetched_at > stale_after:
        return PriceHealthState.STALE
    return PriceHealthState.FRESH
