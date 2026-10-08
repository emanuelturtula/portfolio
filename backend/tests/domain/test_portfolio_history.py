"""`domain.portfolio_history`: spec 037's rules R3 to R6. Pure, so no fixtures.

Every expected figure is a literal, never recomputed with the arithmetic under test.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Final

import pytest

from portfolio.domain.portfolio_history import (
    DailyReading,
    DayValue,
    HistoryRange,
    WalletDay,
    WalletReadings,
    days_of,
    portfolio_days,
    wallet_days,
)

TODAY: Final = date(2026, 10, 8)
D1: Final = date(2026, 10, 1)
D2: Final = date(2026, 10, 2)
D3: Final = date(2026, 10, 3)
D4: Final = date(2026, 10, 4)
DAYS: Final = (D1, D2, D3, D4)


def readings(asset: str, *entries: tuple[date, str]) -> WalletReadings:
    return WalletReadings(
        asset=asset,
        readings=tuple(DailyReading(day, Decimal(quantity)) for day, quantity in entries),
    )


# --------------------------------------------------------------------------------------
# The days of a range (R6)
# --------------------------------------------------------------------------------------


def test_the_range_members_are_their_wire_forms() -> None:
    assert [member.value for member in HistoryRange] == ["30d", "90d", "1y", "all"]


@pytest.mark.parametrize(
    ("history_range", "length"),
    [(HistoryRange.DAYS_30, 30), (HistoryRange.DAYS_90, 90), (HistoryRange.YEAR, 365)],
)
def test_a_fixed_range_is_that_many_days_ending_today(
    history_range: HistoryRange, length: int
) -> None:
    days = days_of(history_range, today=TODAY, first_reading=D1)

    assert len(days) == length
    assert days[-1] == TODAY
    assert days[0] == TODAY - timedelta(days=length - 1)
    assert list(days) == sorted(set(days))


def test_all_starts_on_the_first_reading() -> None:
    assert days_of(HistoryRange.ALL, today=TODAY, first_reading=date(2026, 10, 5)) == (
        date(2026, 10, 5),
        date(2026, 10, 6),
        date(2026, 10, 7),
        TODAY,
    )


@pytest.mark.parametrize(
    "first_reading",
    [None, TODAY + timedelta(days=3)],
    ids=["nothing read", "a first reading after today"],
)
def test_all_is_today_alone_without_a_usable_first_reading(first_reading: date | None) -> None:
    assert days_of(HistoryRange.ALL, today=TODAY, first_reading=first_reading) == (TODAY,)


# --------------------------------------------------------------------------------------
# Every wallet together (R3, R4)
# --------------------------------------------------------------------------------------


def test_a_balance_carries_forward_until_the_next_reading() -> None:
    """Read on D1 and D3: D2 is D1's balance, D4 is D3's. 0.1 BTC at 60000 is 6000."""
    wallet = readings("BTC", (D1, "0.1"), (D3, "0.2"))
    prices = {
        "BTC": {D1: Decimal(60000), D2: Decimal(61000), D3: Decimal(62000), D4: Decimal(63000)}
    }

    points = portfolio_days(DAYS, [wallet], prices)

    assert points == (
        DayValue(D1, Decimal(6000)),
        DayValue(D2, Decimal(6100)),
        DayValue(D3, Decimal(12400)),
        DayValue(D4, Decimal(12600)),
    )


def test_two_wallets_add_up_exactly_at_eighteen_places() -> None:
    btc = readings("BTC", (D1, "0.5"))
    kas = readings("KAS", (D1, "1000"))
    prices = {"BTC": {D1: Decimal(60000)}, "KAS": {D1: Decimal("0.042")}}

    (point,) = portfolio_days((D1,), [btc, kas], prices)

    assert point.value == Decimal("30042")
    assert str(point.value) == "30042.000000000000000000"


def test_a_day_before_any_reading_is_none_not_zero() -> None:
    wallet = readings("BTC", (D3, "0.1"))
    prices = {"BTC": dict.fromkeys(DAYS, Decimal(60000))}

    points = portfolio_days(DAYS, [wallet], prices)

    assert [point.value for point in points] == [None, None, Decimal(6000), Decimal(6000)]


def test_with_no_wallet_every_day_is_none() -> None:
    assert portfolio_days(DAYS, [], {}) == tuple(DayValue(day, None) for day in DAYS)


def test_an_unpriced_holding_makes_the_whole_day_none() -> None:
    """KAS has no price on D2: a sum without it would be believed as the portfolio's value."""
    btc = readings("BTC", (D1, "0.1"))
    kas = readings("KAS", (D1, "1000"))
    prices = {"BTC": {D1: Decimal(60000), D2: Decimal(60000)}, "KAS": {D1: Decimal("0.05")}}

    points = portfolio_days((D1, D2), [btc, kas], prices)

    assert points == (DayValue(D1, Decimal(6050)), DayValue(D2, None))


def test_an_asset_with_no_prices_at_all_is_none() -> None:
    (point,) = portfolio_days((D1,), [readings("KAS", (D1, "1"))], {})

    assert point.value is None


def test_a_wallet_holding_nothing_needs_no_price() -> None:
    """An empty KAS wallet before KAS was listed does not turn the day into a gap."""
    btc = readings("BTC", (D1, "0.1"))
    empty = readings("KAS", (D1, "0"))

    (point,) = portfolio_days((D1,), [btc, empty], {"BTC": {D1: Decimal(60000)}})

    assert point.value == Decimal(6000)


def test_a_day_whose_only_wallet_is_empty_is_zero_because_it_is_known() -> None:
    """Zero is a value here, not an absence: the wallet was read and held nothing."""
    (point,) = portfolio_days((D1,), [readings("BTC", (D1, "0"))], {})

    assert point.value == 0
    assert str(point.value) == "0E-18"


def test_a_wallet_not_yet_read_adds_nothing_to_a_day_another_wallet_values() -> None:
    """R3: nothing is known about the late wallet on D1, so D1 is the early wallet alone."""
    early = readings("BTC", (D1, "0.1"))
    late = readings("BTC", (D2, "0.2"))
    prices = {"BTC": {D1: Decimal(60000), D2: Decimal(60000)}}

    points = portfolio_days((D1, D2), [early, late], prices)

    assert points == (DayValue(D1, Decimal(6000)), DayValue(D2, Decimal(18000)))


# --------------------------------------------------------------------------------------
# One wallet
# --------------------------------------------------------------------------------------


def test_one_wallet_carries_its_quantity_and_values_it() -> None:
    wallet = readings("BTC", (D2, "0.1"))
    prices = {D2: Decimal(60000), D3: Decimal(62000)}

    points = wallet_days((D1, D2, D3, D4), wallet, prices)

    assert points == (
        WalletDay(D1, None, None),
        WalletDay(D2, Decimal("0.1"), Decimal(6000)),
        WalletDay(D3, Decimal("0.1"), Decimal(6200)),
        WalletDay(D4, Decimal("0.1"), None),
    )


def test_an_empty_wallet_is_worth_zero_with_or_without_a_price() -> None:
    points = wallet_days((D1,), readings("KAS", (D1, "0")), {})

    assert points == (WalletDay(D1, Decimal(0), Decimal(0)),)


def test_a_reading_after_the_range_is_not_carried_backwards() -> None:
    points = wallet_days((D1, D2), readings("BTC", (D4, "1")), {D1: Decimal(1), D2: Decimal(1)})

    assert points == (WalletDay(D1, None, None), WalletDay(D2, None, None))
