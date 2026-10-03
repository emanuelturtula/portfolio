"""Spec 029 (#22), criterion 3: rotation against an independent oracle, for any instants.

`backups_to_keep` groups instants by `date()` and by `isocalendar()`, sorts the keys and
slices. The oracle below shares none of that: it walks the instants once, newest first. It
keeps **every** copy it meets on the first `keep_daily` dates it meets (R5: a copy taken by
hand, or a restore's safety copy, must survive the next rotation), and the first copy it
meets in each of the first `keep_weekly` weeks it meets, which is that week's newest. It
spells the ISO week with `strftime("%G-%V")` rather than with `isocalendar`. Two statements
of the rule that agree on every generated example are the evidence; one statement checked
against itself is not.

The instants are drawn from a few weeks around three anchors -- one mid-year, and two that
straddle a new year whose ISO week 53 spills into January -- so that several copies share a
date, several dates share a week, and weeks cross a year boundary. Offsets other than UTC are
generated too, because the rule groups by the UTC date whatever offset an instant arrives in.

## Why this is a module of its own, and must stay a short one

Hypothesis formats the traceback of every failing example while it shrinks, and pytest
parses the whole module of the failing frame each time. In a large module a property broken
on purpose overran the suite's 30-second timeout during #116. So: nothing else belongs
here, and `report_multiple_bugs` is off, which shrinks the first broken assertion alone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from portfolio.domain.backups import backups_to_keep

ANCHORS: Final = (date(2026, 6, 10), date(2026, 12, 20), date(2020, 12, 21))
SPAN_DAYS: Final = 45
MICROSECONDS_PER_DAY: Final = 86_400_000_000

OFFSETS: Final = st.sampled_from(
    [UTC, timezone(timedelta(hours=2)), timezone(timedelta(hours=-5)), timezone(timedelta(0))]
)


@st.composite
def instants(draw: st.DrawFn) -> datetime:
    """An instant within `SPAN_DAYS` of one anchor, in one of a few offsets."""
    anchor = draw(st.sampled_from(ANCHORS))
    day = draw(st.integers(min_value=0, max_value=SPAN_DAYS))
    within = draw(st.integers(min_value=0, max_value=MICROSECONDS_PER_DAY - 1))
    start = datetime(anchor.year, anchor.month, anchor.day, tzinfo=UTC)
    instant = start + timedelta(days=day, microseconds=within)
    return instant.astimezone(draw(OFFSETS))


def oracle(copies: list[datetime], *, keep_daily: int, keep_weekly: int) -> set[datetime]:
    """One pass, newest first. Every copy on an early-met date; a week's first copy met."""
    kept: set[datetime] = set()
    dates_met: list[str] = []
    weeks_met: list[str] = []
    for instant in sorted(copies, reverse=True):
        utc = instant.astimezone(UTC)
        day = utc.strftime("%Y-%m-%d")
        week = utc.strftime("%G-%V")
        if day not in dates_met:
            dates_met.append(day)
        if dates_met.index(day) < keep_daily:
            kept.add(utc)
        if week not in weeks_met:
            weeks_met.append(week)
            if len(weeks_met) <= keep_weekly:
                kept.add(utc)
    return kept


@settings(max_examples=300, deadline=None, report_multiple_bugs=False)
@given(
    given_instants=st.lists(instants(), max_size=40),
    keep_daily=st.integers(min_value=1, max_value=9),
    keep_weekly=st.integers(min_value=0, max_value=6),
)
def test_rotation_keeps_exactly_what_the_oracle_keeps(
    given_instants: list[datetime], keep_daily: int, keep_weekly: int
) -> None:
    kept = backups_to_keep(given_instants, keep_daily=keep_daily, keep_weekly=keep_weekly)

    assert kept == oracle(given_instants, keep_daily=keep_daily, keep_weekly=keep_weekly)
    # What follows is implied by the oracle, and stated so a failure says which part broke.
    assert kept <= set(given_instants)
    assert len({instant.astimezone(UTC).date() for instant in kept}) <= keep_daily + keep_weekly
    if given_instants:
        newest = max(given_instants).astimezone(UTC).date()
        assert {each for each in given_instants if each.astimezone(UTC).date() == newest} <= kept
    # Rotating what was kept deletes nothing more: a second pass is a no-op.
    assert backups_to_keep(kept, keep_daily=keep_daily, keep_weekly=keep_weekly) == kept
