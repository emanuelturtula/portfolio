"""Which months owe the manual exchange exports (spec 040)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from portfolio.domain.export_reminders import (
    EXPORT_EXCHANGES,
    FIRST_REMINDED_MONTH,
    MonthNotClosedError,
    MonthNotRemindedError,
    closed_months,
    parse_month,
    pending_months,
    require_closed,
)

SEPTEMBER = date(2026, 9, 1)
OCTOBER = date(2026, 10, 1)
NOVEMBER = date(2026, 11, 1)
DECEMBER = date(2026, 12, 1)


def test_the_first_reminded_month_and_the_exchanges_are_the_owners() -> None:
    assert FIRST_REMINDED_MONTH == SEPTEMBER
    assert EXPORT_EXCHANGES == ("Binance", "Bitget", "BingX", "Nexo")


def test_nothing_is_owed_before_the_first_month_ends() -> None:
    assert closed_months(datetime(2026, 9, 20, tzinfo=UTC)) == []


def test_a_month_ends_at_midnight_argentina_time_not_utc() -> None:
    """Midnight on 1 October in Argentina is 03:00 UTC: September is owed from then, not before."""
    assert closed_months(datetime(2026, 10, 1, 2, 59, 59, tzinfo=UTC)) == []
    assert closed_months(datetime(2026, 10, 1, 3, 0, tzinfo=UTC)) == [SEPTEMBER]


def test_an_aware_time_in_any_zone_reads_the_same_instant() -> None:
    tokyo = timezone(timedelta(hours=9))
    assert closed_months(datetime(2026, 10, 1, 12, 0, tzinfo=tokyo)) == [SEPTEMBER]


def test_every_closed_month_is_listed_oldest_first_across_a_year_end() -> None:
    now = datetime(2027, 2, 1, 3, 0, tzinfo=UTC)
    assert closed_months(now) == [
        SEPTEMBER,
        OCTOBER,
        NOVEMBER,
        DECEMBER,
        date(2027, 1, 1),
    ]


def test_a_month_marked_done_is_no_longer_pending() -> None:
    now = datetime(2026, 12, 15, tzinfo=UTC)
    assert pending_months(now, done={SEPTEMBER, NOVEMBER}) == [OCTOBER]
    assert pending_months(now, done={SEPTEMBER, OCTOBER, NOVEMBER}) == []


def test_a_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        closed_months(datetime(2026, 10, 9))  # noqa: DTZ001 - the input under test


def test_a_first_month_that_is_not_a_month_start_is_refused() -> None:
    with pytest.raises(ValueError, match="first day of a month"):
        closed_months(datetime(2026, 10, 9, tzinfo=UTC), first=date(2026, 9, 2))


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2026-09", SEPTEMBER), ("2026-12", DECEMBER), ("2027-01", date(2027, 1, 1))],
)
def test_a_month_is_parsed_from_year_and_month(text: str, expected: date) -> None:
    assert parse_month(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "2026-9",
        "2026-13",
        "2026-00",
        "26-09",
        "2026/09",
        "2026-09-01",
        "\uff12\uff10\uff12\uff16-09",
        "",
    ],
)
def test_anything_else_is_not_a_month(text: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - the message differs by cause
        parse_month(text)


def test_only_a_closed_month_from_the_first_can_be_marked_done() -> None:
    now = datetime(2026, 10, 9, tzinfo=UTC)
    require_closed(SEPTEMBER, now)
    with pytest.raises(MonthNotClosedError):
        require_closed(OCTOBER, now)
    with pytest.raises(MonthNotRemindedError):
        require_closed(date(2026, 8, 1), now)
