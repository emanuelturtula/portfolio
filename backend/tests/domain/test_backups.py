"""Spec 029 (#22): a copy's name, and which copies rotation keeps.

Pure and fast: no fixture, no file, no clock. Every instant is a literal, and every expected
set of kept copies is worked out by hand beside it. The same rule is held to an independent
oracle over generated instants in `test_backups_rotation_property.py`, a module of its own
for the reason that module gives.

What each block pins:

* **the name** is the UTC instant the copy started, to the microsecond, and the pattern
  accepts that name and nothing else -- not a trailing newline, not a path, not a near miss;
* **`instant_of`** is the one parser of a name, and refuses a name that matches the pattern
  but names no instant (a 13th month), because such a file was not written here;
* **rotation** keeps every copy on the `keep_daily` most recent UTC dates that have one
  (R5), and the newest copy of each of the `keep_weekly` most recent ISO weeks that have one
  -- across a pause, across the ISO year boundary, and for instants given in another offset;
* **the refusals**: a naive instant, `keep_daily` below one, `keep_weekly` below zero.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Final

import pytest

from portfolio.domain.backups import (
    BACKUP_NAME_PATTERN,
    BackupErrorKind,
    backup_name,
    backups_to_keep,
    instant_of,
    utc_stamp,
)

#: The spec's own example instant: 2026-10-02T03:00:00.123456Z.
SPEC_INSTANT: Final = datetime(2026, 10, 2, 3, 0, 0, 123456, tzinfo=UTC)
SPEC_NAME: Final = "portfolio-20261002T030000123456Z.sqlite3"

#: The same wall-clock reading with no zone: the value every refusal below is about.
NAIVE: Final = SPEC_INSTANT.replace(tzinfo=None)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    """An instant on `day`, in UTC."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


class NoOffset(tzinfo):
    """A time zone that names itself but answers no offset: aware in form, naive in fact."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        del dt
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        del dt
        return None

    def tzname(self, dt: datetime | None) -> str:
        del dt
        return "nowhere"


# --------------------------------------------------------------------------------------
# The name
# --------------------------------------------------------------------------------------


def test_a_copy_is_named_after_the_utc_instant_it_started_to_the_microsecond() -> None:
    assert backup_name(SPEC_INSTANT) == SPEC_NAME


def test_an_instant_in_another_offset_names_the_same_file() -> None:
    """03:00:00.123456Z is 06:00:00.123456 at +03:00 and 23:00 the day before at -04:00."""
    east = SPEC_INSTANT.astimezone(timezone(timedelta(hours=3)))
    west = SPEC_INSTANT.astimezone(timezone(timedelta(hours=-4)))

    assert east.hour == 6
    assert west.day == 1
    assert backup_name(east) == SPEC_NAME
    assert backup_name(west) == SPEC_NAME


def test_a_whole_second_still_carries_six_digits_of_microseconds() -> None:
    assert backup_name(datetime(2027, 1, 4, 0, 0, 0, tzinfo=UTC)) == (
        "portfolio-20270104T000000000000Z.sqlite3"
    )


def test_a_naive_instant_has_no_name() -> None:
    """Naming it would read the host's time zone, and a local name sorts wrongly."""
    with pytest.raises(ValueError, match="timezone-aware"):
        backup_name(NAIVE)


def test_an_instant_whose_zone_answers_no_offset_has_no_name() -> None:
    """`tzinfo` set but `utcoffset()` `None` is naive by Python's own definition."""
    with pytest.raises(ValueError, match="timezone-aware"):
        backup_name(datetime(2026, 10, 2, 3, 0, 0, tzinfo=NoOffset()))


def test_the_stamp_is_the_utc_instant_alone_whatever_offset_it_is_given_in() -> None:
    """The one spelling of an instant in a file name: a copy, its temporary file, a damaged
    database moved aside (R3). Shared, so all three sort and read the same way."""
    east = SPEC_INSTANT.astimezone(timezone(timedelta(hours=3)))

    assert utc_stamp(SPEC_INSTANT) == "20261002T030000123456Z"
    assert utc_stamp(east) == "20261002T030000123456Z"
    assert utc_stamp(datetime(2027, 1, 4, tzinfo=UTC)) == "20270104T000000000000Z"
    assert backup_name(SPEC_INSTANT) == f"portfolio-{utc_stamp(SPEC_INSTANT)}.sqlite3"


@pytest.mark.parametrize(
    "instant",
    [NAIVE, datetime(2026, 10, 2, 3, 0, 0, tzinfo=NoOffset())],
    ids=["naive", "a zone with no offset"],
)
def test_a_naive_instant_has_no_stamp(instant: datetime) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_stamp(instant)


def test_the_pattern_accepts_the_name_backup_name_writes() -> None:
    assert BACKUP_NAME_PATTERN.match(SPEC_NAME) is not None


@pytest.mark.parametrize(
    "name",
    [
        f"{SPEC_NAME}\n",
        f" {SPEC_NAME}",
        f"backups/{SPEC_NAME}",
        f"../{SPEC_NAME}",
        f".{SPEC_NAME}",
        SPEC_NAME.replace("Z.", "z."),
        SPEC_NAME.replace(".sqlite3", ".sqlite"),
        SPEC_NAME.replace(".sqlite3", ".sqlite3.partial"),
        SPEC_NAME.replace("portfolio-", "Portfolio-"),
        SPEC_NAME.replace("123456Z", "12345Z"),
        SPEC_NAME.replace("123456Z", "1234567Z"),
        SPEC_NAME.replace("T", "_"),
        ".portfolio-20261002T030000123456Z.partial",
        "portfolio-2026-10-02T03:00:00.123456Z.sqlite3",
        "portfolio.db",
        "",
    ],
)
def test_the_pattern_and_instant_of_refuse_anything_else(name: str) -> None:
    assert BACKUP_NAME_PATTERN.match(name) is None
    assert instant_of(name) is None


# --------------------------------------------------------------------------------------
# instant_of
# --------------------------------------------------------------------------------------


def test_instant_of_reads_the_instant_back_in_utc() -> None:
    instant = instant_of(SPEC_NAME)

    assert instant == SPEC_INSTANT
    assert instant is not None
    assert instant.tzinfo is UTC


@pytest.mark.parametrize(
    "instant",
    [
        SPEC_INSTANT,
        datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        datetime(2027, 1, 1, 0, 0, 0, 1, tzinfo=UTC),
        datetime(2028, 2, 29, 12, 0, 0, tzinfo=UTC),
    ],
)
def test_instant_of_inverts_backup_name(instant: datetime) -> None:
    assert instant_of(backup_name(instant)) == instant


@pytest.mark.parametrize(
    "name",
    [
        "portfolio-20261302T030000123456Z.sqlite3",  # month 13
        "portfolio-20260230T030000123456Z.sqlite3",  # 30 February
        "portfolio-20261000T030000123456Z.sqlite3",  # day 0
        "portfolio-20261002T240000123456Z.sqlite3",  # hour 24
        "portfolio-20261002T036000123456Z.sqlite3",  # minute 60
        "portfolio-20261002T030060123456Z.sqlite3",  # second 60
        "portfolio-00001002T030000123456Z.sqlite3",  # year 0
    ],
)
def test_a_name_in_the_pattern_that_names_no_instant_is_not_a_copy(name: str) -> None:
    """Matched by the pattern, refused by the calendar: not a file this code wrote."""
    assert BACKUP_NAME_PATTERN.match(name) is not None
    assert instant_of(name) is None


# --------------------------------------------------------------------------------------
# Rotation
# --------------------------------------------------------------------------------------

#: Copies at 03:00 and 15:00 every day from 1 August to 2 October 2026, as a daily timer
#: and an operator's hand copy would leave them after two months.
TODAY: Final = date(2026, 10, 2)  # a Friday, in ISO week 2026-W40
FIRST_DAY: Final = date(2026, 8, 1)
TWICE_A_DAY: Final = [
    at(FIRST_DAY + timedelta(days=offset), hour)
    for offset in range((TODAY - FIRST_DAY).days + 1)
    for hour in (3, 15)
]
ONCE_A_DAY: Final = [instant for instant in TWICE_A_DAY if instant.hour == 3]


def on(*days: date, hours: tuple[int, ...] = (3, 15)) -> set[datetime]:
    """Every copy of `TWICE_A_DAY` on `days` at `hours`."""
    return {at(day, hour) for day in days for hour in hours}


def september(*days: int) -> tuple[date, ...]:
    return tuple(date(2026, 9, day) for day in days)


def test_the_spec_example_keeps_nine_copies_of_a_daily_timer() -> None:
    """Worked by hand: the spec's own `"count": 9`.

    Daily, the 7 most recent dates: 26 September to 2 October. Weekly, the 4 most recent
    ISO weeks: W40 (28 Sep - 4 Oct), W39 (21 - 27 Sep), W38 (14 - 20 Sep) and W37
    (7 - 13 Sep), the newest copy of each -- 2 Oct, 27 Sep, 20 Sep and 13 Sep. The first two
    are already daily, so the union is 7 + 2 = 9.
    """
    kept = backups_to_keep(ONCE_A_DAY, keep_daily=7, keep_weekly=4)

    daily = on(*september(26, 27, 28, 29, 30), date(2026, 10, 1), TODAY, hours=(3,))
    weekly = on(*september(20, 13), hours=(3,))
    assert kept == frozenset(daily | weekly)
    assert len(kept) == 9


def test_every_copy_of_the_seven_days_is_kept_and_the_newest_of_each_older_week() -> None:
    """R5, worked by hand on two copies a day.

    Both copies of each of the 7 most recent dates: 14. Of W38 and W37, which are older than
    those dates, the newest copy alone: 20 and 13 September at 15:00, and not at 03:00.
    """
    kept = backups_to_keep(TWICE_A_DAY, keep_daily=7, keep_weekly=4)

    daily = on(*september(26, 27, 28, 29, 30), date(2026, 10, 1), TODAY)
    weekly = on(*september(20, 13), hours=(15,))
    assert kept == frozenset(daily | weekly)
    assert len(kept) == 16


def test_one_day_and_no_weeks_keeps_every_copy_of_the_newest_date() -> None:
    assert backups_to_keep(TWICE_A_DAY, keep_daily=1, keep_weekly=0) == on(TODAY)


def test_no_weeks_means_the_daily_set_alone() -> None:
    kept = backups_to_keep(TWICE_A_DAY, keep_daily=3, keep_weekly=0)

    assert kept == on(date(2026, 9, 30), date(2026, 10, 1), TODAY)


def test_weeks_alone_reach_further_back_than_the_days() -> None:
    kept = backups_to_keep(TWICE_A_DAY, keep_daily=1, keep_weekly=3)

    assert kept == on(TODAY) | on(*september(27, 20), hours=(15,))


def test_a_copy_taken_by_hand_survives_the_next_rotation() -> None:
    """The reviewer's case for R5: the copy from before a bad import is not rotated away.

    The timer copies at 03:00 every day. On 1 October the owner takes a copy at 12:00, to be
    safe, before an import; the 03:00 copy of 2 October then rotates. Keeping only the newest
    copy per date would delete the 12:00 copy -- and with it the database before the import.
    """
    by_hand = at(date(2026, 10, 1), 12)

    kept = backups_to_keep([*ONCE_A_DAY, by_hand], keep_daily=7, keep_weekly=0)

    assert by_hand in kept
    assert at(date(2026, 10, 1), 3) in kept
    assert len(kept) == 8


def test_a_pause_does_not_empty_the_daily_set() -> None:
    """The dates *that have a copy*, not the last N calendar days.

    Copies on 1 to 5 September, then nothing for three weeks, then one on 2 October. Counted
    in calendar days, a 3-day window would hold only 2 October. Counted in days that have a
    copy, it holds 2 October, 5 and 4 September.
    """
    before = [at(date(2026, 9, day), 3) for day in range(1, 6)]
    kept = backups_to_keep([*before, at(TODAY, 3)], keep_daily=3, keep_weekly=0)

    assert kept == {at(TODAY, 3), at(date(2026, 9, 5), 3), at(date(2026, 9, 4), 3)}


def test_every_copy_of_a_kept_date_is_kept_in_whatever_order_they_come() -> None:
    """Six copies on one date, shuffled: all six are kept, not the newest alone."""
    same_day = [at(TODAY, hour, minute) for hour in (1, 9, 23) for minute in (0, 59)]
    shuffled = [same_day[3], same_day[5], same_day[0], same_day[4], same_day[1], same_day[2]]

    assert backups_to_keep(shuffled, keep_daily=1, keep_weekly=4) == frozenset(same_day)


def test_a_week_older_than_the_days_keeps_its_newest_copy_whatever_the_order() -> None:
    """Of three copies in W38, given newest first, the weekly rule keeps the newest alone."""
    newest_of_week = at(date(2026, 9, 18), 23, 59)
    others = [at(date(2026, 9, 16), 9), at(date(2026, 9, 14), 1)]
    now = at(TODAY, 3)

    kept = backups_to_keep([newest_of_week, now, *others], keep_daily=1, keep_weekly=2)

    assert kept == {now, newest_of_week}


def test_iso_weeks_cross_the_year_boundary_as_iso_8601_says() -> None:
    """29 Dec 2026 and 2 Jan 2027 are both in ISO week 2026-W53; 5 Jan 2027 is 2027-W01.

    Three weeks are asked for and only two exist. A rule that grouped by calendar year and
    ISO week would see three -- (2026, 53), (2027, 53), (2027, 1) -- and keep 29 December
    too.
    """
    late_december = at(date(2026, 12, 29), 10)
    new_year = at(date(2027, 1, 2), 10)
    first_week = at(date(2027, 1, 5), 10)

    kept = backups_to_keep([late_december, new_year, first_week], keep_daily=1, keep_weekly=3)

    assert kept == {first_week, new_year}


def test_the_same_week_number_in_two_years_is_two_weeks() -> None:
    """2025-W40 and 2026-W40: a rule keyed on the week number alone would merge them."""
    a_year_ago = at(date(2025, 10, 1), 3)
    now = at(TODAY, 3)

    assert backups_to_keep([a_year_ago, now], keep_daily=1, keep_weekly=2) == {a_year_ago, now}


def test_dates_are_utc_dates_whatever_offset_an_instant_is_given_in() -> None:
    """One UTC date, 1 October, written in three offsets; and 30 September before it.

    00:30 on 2 October at +02:00 is 22:30 on 1 October in UTC, and 22:00 on 30 September at
    -05:00 is 03:00 on 1 October in UTC. Grouped by UTC date, the newest date is 1 October
    and holds all three. Grouped by the offset each was given in, the newest date would be
    2 October and hold one copy alone, and the -05:00 copy would be on 30 September.
    """
    late = datetime(2026, 10, 1, 23, 30, tzinfo=UTC)
    given_east = datetime(2026, 10, 2, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    given_west = datetime(2026, 9, 30, 22, 0, tzinfo=timezone(timedelta(hours=-5)))
    day_before = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

    kept = backups_to_keep([day_before, late, given_east, given_west], keep_daily=1, keep_weekly=0)

    assert kept == {late, given_east, given_west}


def test_nothing_to_keep_from_nothing() -> None:
    assert backups_to_keep([], keep_daily=7, keep_weekly=4) == frozenset()


def test_it_takes_any_iterable_once() -> None:
    """A generator is consumed once; the answer is the same as for a list."""
    kept = backups_to_keep((instant for instant in TWICE_A_DAY), keep_daily=7, keep_weekly=4)

    assert kept == backups_to_keep(TWICE_A_DAY, keep_daily=7, keep_weekly=4)


@pytest.mark.parametrize(
    ("keep_daily", "keep_weekly", "message"),
    [
        (0, 4, "keep_daily must be at least 1, got 0"),
        (-3, 4, "keep_daily must be at least 1, got -3"),
        (7, -1, "keep_weekly must be at least 0, got -1"),
    ],
)
def test_a_retention_that_would_delete_the_newest_copy_is_refused(
    keep_daily: int, keep_weekly: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        backups_to_keep(TWICE_A_DAY, keep_daily=keep_daily, keep_weekly=keep_weekly)


def test_the_smallest_retention_is_accepted() -> None:
    assert backups_to_keep([at(TODAY, 3)], keep_daily=1, keep_weekly=0) == {at(TODAY, 3)}


def test_a_naive_instant_cannot_be_rotated() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        backups_to_keep([at(TODAY, 3), NAIVE], keep_daily=7, keep_weekly=4)


def test_the_retention_is_checked_before_the_instants() -> None:
    """A bad setting is named as the setting, even beside a naive instant."""
    with pytest.raises(ValueError, match="keep_daily"):
        backups_to_keep([NAIVE], keep_daily=0, keep_weekly=4)


# --------------------------------------------------------------------------------------
# The error kinds are wire values
# --------------------------------------------------------------------------------------


def test_the_error_kinds_are_the_three_the_spec_names() -> None:
    """Served as `last_error_kind` and worded by the frontend, so pinned as strings."""
    assert {kind.value for kind in BackupErrorKind} == {
        "database_error",
        "integrity_failed",
        "storage_error",
    }
    assert BackupErrorKind("database_error") is BackupErrorKind.DATABASE_ERROR
    assert BackupErrorKind("integrity_failed") is BackupErrorKind.INTEGRITY_FAILED
    assert BackupErrorKind("storage_error") is BackupErrorKind.STORAGE_ERROR
