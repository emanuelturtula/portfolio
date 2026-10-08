"""Spec 038's older price source: BTC/USD daily closes from Coinbase Exchange's candles.

`parse_candles` is driven with bodies shaped like the one measured on 2026-10-08:

```
[[1696118400,26955.25,28062.62,26961,27995.46,8747.06888783], ...]
```

## The prices are JSON numbers, and that is the subject

Coinbase documents decimals as strings and sends candles as numbers, measured. A plain
`json.loads` turns `27995.46` into the nearest double; `decode_json` builds a `Decimal` from
the digits instead. So the assertions here compare the close with `Decimal("27995.46")` and
with its string form: a float anywhere on the path would show as a different string.

## The bodies are literals

For the reason `harness.py` gives. A candle is assembled from JSON tokens written as text,
never from Python numbers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import httpx
import pytest

from portfolio.providers.errors import (
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from portfolio.providers.http import ASSET_DAILY_CLOSES, ENDPOINT_EXTENSION
from portfolio.providers.prices.base import BTC, EUR, KAS, USD, DailyClose
from portfolio.providers.prices.coinbase import (
    CANDLE_PRODUCTS,
    CANDLES_PATH,
    COINBASE,
    COINBASE_EXCHANGE_API_URL,
    DAILY_GRANULARITY_SECONDS,
    EARLIEST_CANDLE_DAY,
    MAX_DAYS_PER_REQUEST,
    CoinbaseDailyCloses,
    candle_windows,
    parse_candles,
)
from tests.providers.prices.harness import (
    COINBASE_EXCHANGE_HOST,
    COINBASE_HOST,
    PriceFake,
    Reply,
    ScriptedVendor,
    coinbase_candle,
    coinbase_candles_body,
    coinbase_candles_echo,
    price_client,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from portfolio.providers.prices.base import HistoricalCloseSource, PricePair

PRODUCT: Final = "BTC-USD"
BTC_USD: Final[PricePair] = (BTC, USD)

#: The measured candle, byte for byte: 2023-10-01, close `27995.46`, open a bare integer.
MEASURED_CANDLE: Final = "[1696118400,26955.25,28062.62,26961,27995.46,8747.06888783]"
MEASURED_DAY: Final = date(2023, 10, 1)
MEASURED_TIME: Final = 1696118400

#: Three consecutive UTC midnights, as the integers the vendor sends.
DAY_ONE: Final = 1696118400
DAY_TWO: Final = DAY_ONE + 86_400
DAY_THREE: Final = DAY_TWO + 86_400

#: "Now" for the source under test: 2026-10-08 at 15:00 UTC, so yesterday is 2026-10-07.
NOW: Final = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
TODAY: Final = date(2026, 10, 8)
YESTERDAY: Final = date(2026, 10, 7)


def epoch(day: date) -> int:
    """A day's UTC midnight as epoch seconds, computed apart from the parser."""
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def query(start: date, end: date) -> str:
    """The query one window must carry, written out in full."""
    return f"granularity=86400&start={start.isoformat()}T00:00:00Z&end={end.isoformat()}T00:00:00Z"


def fixed_clock(now: datetime = NOW) -> Callable[[], datetime]:
    return lambda: now


AT_NOW: Final = fixed_clock()


# --------------------------------------------------------------------------------------
# The parser: exact digits, index 4, keyed by time, the window
# --------------------------------------------------------------------------------------


def test_the_measured_candles_close_is_the_exact_decimal_the_vendor_wrote() -> None:
    """`27995.46` as a JSON number comes back as `Decimal("27995.46")`, digit for digit.

    `Decimal(27995.46)` -- built from the float -- is
    `27995.4599999999991268850862979888916015625`; the string comparison is what would
    show a float anywhere on the way.
    """
    (only,) = parse_candles(f"[{MEASURED_CANDLE}]", PRODUCT, MEASURED_DAY, MEASURED_DAY)

    assert only == DailyClose(day=MEASURED_DAY, close=Decimal("27995.46"))
    assert isinstance(only.close, Decimal)
    assert str(only.close) == "27995.46"


def test_the_close_is_index_four_and_not_the_low_the_high_the_open_or_the_volume() -> None:
    """`[time, low, high, open, close, volume]`: every other number is a different one."""
    (only,) = parse_candles(f"[{MEASURED_CANDLE}]", PRODUCT, MEASURED_DAY, MEASURED_DAY)

    for wrong in ("26955.25", "28062.62", "26961", "8747.06888783"):
        assert only.close != Decimal(wrong)


def test_a_close_sent_as_a_bare_json_integer_is_that_integer_as_a_decimal() -> None:
    """111 of 1,096 measured prices were bare integers: `26961` becomes `Decimal(26961)`."""
    body = coinbase_candles_body([coinbase_candle(str(DAY_ONE), "26961")])

    (only,) = parse_candles(body, PRODUCT, MEASURED_DAY, MEASURED_DAY)

    assert only.close == Decimal(26961)
    assert isinstance(only.close, Decimal)
    assert str(only.close) == "26961"


def test_newest_first_comes_back_oldest_first_keyed_by_time() -> None:
    """The vendor was measured newest first; the order is undocumented and not relied on."""
    body = coinbase_candles_body(
        [
            coinbase_candle(str(DAY_THREE), "3.5"),
            coinbase_candle(str(DAY_ONE), "1.5"),
            coinbase_candle(str(DAY_TWO), "2.5"),
        ]
    )

    closes = parse_candles(body, PRODUCT, date(2023, 10, 1), date(2023, 10, 3))

    assert closes == (
        DailyClose(day=date(2023, 10, 1), close=Decimal("1.5")),
        DailyClose(day=date(2023, 10, 2), close=Decimal("2.5")),
        DailyClose(day=date(2023, 10, 3), close=Decimal("3.5")),
    )


def test_a_candle_outside_the_asked_window_is_dropped_on_either_side() -> None:
    """Documented: some candles "may precede your declared `start`". Neither end leaks in."""
    body = coinbase_candles_body(
        [
            coinbase_candle(str(DAY_THREE), "3.5"),
            coinbase_candle(str(DAY_TWO), "2.5"),
            coinbase_candle(str(DAY_ONE), "1.5"),
        ]
    )

    closes = parse_candles(body, PRODUCT, date(2023, 10, 2), date(2023, 10, 2))

    assert closes == (DailyClose(day=date(2023, 10, 2), close=Decimal("2.5")),)


def test_a_time_no_date_can_hold_is_outside_the_window_rather_than_an_overflow() -> None:
    """A midnight ten thousand years out is compared as an integer and dropped, never dated.

    `datetime.fromtimestamp` of it raises `OverflowError` or `ValueError`, neither of which
    the backfill catches; the parser's contract is `ProviderResponseError` or an answer.
    """
    far = 10**12 * DAILY_GRANULARITY_SECONDS
    body = coinbase_candles_body(
        [coinbase_candle(str(far), "1.5"), coinbase_candle(str(-far), "1.5")]
    )

    assert parse_candles(body, PRODUCT, date(2023, 10, 1), date(2023, 10, 3)) == ()


def test_an_empty_array_is_an_empty_series() -> None:
    """2014 answers `[]`, measured: no candle is a gap, never a zero."""
    assert parse_candles("[]", PRODUCT, date(2014, 1, 1), date(2014, 6, 1)) == ()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json", id="not JSON"),
        pytest.param('{"message": "NotFound"}', id="an object rather than an array"),
        pytest.param(coinbase_candles_body(['{"time": 1696118400}']), id="a candle object"),
        pytest.param(coinbase_candles_body(['"1696118400"']), id="a candle that is a string"),
        pytest.param(
            coinbase_candles_body(["[1696118400,26955.25,28062.62,26961]"]),
            id="a candle of four, with no close",
        ),
        pytest.param(
            coinbase_candles_body([coinbase_candle(f'"{DAY_ONE}"', "1.5")]),
            id="a time that is a string",
        ),
        pytest.param(
            coinbase_candles_body([coinbase_candle(f"{DAY_ONE}.0", "1.5")]),
            id="a time that is a JSON fraction",
        ),
        pytest.param(
            coinbase_candles_body([coinbase_candle(str(DAY_ONE + 3600), "1.5")]),
            id="a time an hour past midnight",
        ),
        pytest.param(
            coinbase_candles_body([coinbase_candle(str(DAY_ONE + 1), "1.5")]),
            id="a time a second past midnight",
        ),
        pytest.param(
            coinbase_candles_body([coinbase_candle("false", "1.5")]),
            id="a time of false, which equals the midnight 0",
        ),
        pytest.param(coinbase_candles_body([coinbase_candle(str(DAY_ONE), "0")]), id="zero"),
        pytest.param(coinbase_candles_body([coinbase_candle(str(DAY_ONE), "-1.5")]), id="neg"),
        pytest.param(coinbase_candles_body([coinbase_candle(str(DAY_ONE), "null")]), id="null"),
        pytest.param(coinbase_candles_body([coinbase_candle(str(DAY_ONE), "true")]), id="true"),
        pytest.param(
            coinbase_candles_body([coinbase_candle(str(DAY_ONE), '"NaN"')]), id="a NaN string"
        ),
        pytest.param(
            coinbase_candles_body([coinbase_candle(str(DAY_ONE), '"not a number"')]),
            id="a string that is not a number",
        ),
        pytest.param(
            coinbase_candles_body([coinbase_candle(str(DAY_ONE), "1e-20")]),
            id="a close too small to store",
        ),
        pytest.param(
            coinbase_candles_body(
                [coinbase_candle(str(DAY_ONE), "1.5"), coinbase_candle(str(DAY_ONE), "1.6")]
            ),
            id="two candles for one day",
        ),
    ],
)
def test_a_body_that_cannot_be_trusted_is_a_typed_refusal(body: str) -> None:
    """Every row of the docstring's table, from the token that causes it.

    `ProviderResponseError` in every case and never a `KeyError`, `TypeError` or
    `IndexError`: the backfill catches `ProviderError` and nothing else.
    """
    with pytest.raises(ProviderResponseError):
        parse_candles(body, PRODUCT, date(2023, 10, 1), date(2023, 10, 3))


def test_the_refusals_name_the_product_and_never_a_value() -> None:
    """A product id is public vocabulary; a body's contents go in no message."""
    bad_time = coinbase_candles_body([coinbase_candle("1696118401", "1.5")])
    bad_close = coinbase_candles_body([coinbase_candle(str(DAY_ONE), '"a-suspicious-value"')])
    twice = coinbase_candles_body(
        [coinbase_candle(str(DAY_ONE), "4321.5"), coinbase_candle(str(DAY_ONE), "4321.6")]
    )
    window = (date(2023, 10, 1), date(2023, 10, 3))

    with pytest.raises(ProviderResponseError) as time_refusal:
        parse_candles(bad_time, PRODUCT, *window)
    with pytest.raises(ProviderResponseError) as close_refusal:
        parse_candles(bad_close, PRODUCT, *window)
    with pytest.raises(ProviderResponseError) as twice_refusal:
        parse_candles(twice, PRODUCT, *window)
    with pytest.raises(ProviderResponseError) as shape_refusal:
        parse_candles('{"secret": 4321.5}', PRODUCT, *window)

    assert PRODUCT in str(time_refusal.value)
    assert "1696118401" not in str(time_refusal.value)
    assert "a-suspicious-value" not in str(close_refusal.value)
    assert PRODUCT in str(twice_refusal.value)
    assert "4321" not in str(twice_refusal.value)
    assert "4321" not in str(shape_refusal.value)


# --------------------------------------------------------------------------------------
# The windows
# --------------------------------------------------------------------------------------


def test_a_700_day_range_is_three_windows_of_at_most_300_days_with_no_overlap() -> None:
    first = date(2020, 1, 1)
    last = first + timedelta(days=699)

    assert candle_windows(first, last) == (
        (first, first + timedelta(days=299)),
        (first + timedelta(days=300), first + timedelta(days=599)),
        (first + timedelta(days=600), last),
    )


@pytest.mark.parametrize(
    ("days", "windows"),
    [
        pytest.param(1, 1, id="one day"),
        pytest.param(300, 1, id="exactly 300 days, 299 intervals"),
        pytest.param(301, 2, id="one more"),
        pytest.param(600, 2, id="two full windows"),
    ],
)
def test_the_window_count_is_the_days_over_300_rounded_up(days: int, windows: int) -> None:
    first = date(2020, 1, 1)

    found = candle_windows(first, first + timedelta(days=days - 1))

    assert len(found) == windows
    assert all((end - start).days < MAX_DAYS_PER_REQUEST for start, end in found)
    assert sum((end - start).days + 1 for start, end in found) == days


def test_an_empty_range_is_no_window() -> None:
    assert candle_windows(date(2020, 1, 2), date(2020, 1, 1)) == ()


def test_the_measured_sweep_back_to_the_first_candle_is_twelve_requests() -> None:
    """2015-07-20 to the day before Kraken's first BTC candle (2024-10-18): 12, as measured."""
    windows = candle_windows(EARLIEST_CANDLE_DAY, date(2024, 10, 17))

    assert len(windows) == 12
    assert windows[0][0] == EARLIEST_CANDLE_DAY
    assert windows[-1][1] == date(2024, 10, 17)


# --------------------------------------------------------------------------------------
# The source: the request, the narrowing, the clock
# --------------------------------------------------------------------------------------


def every_day_from(first: date, last: date) -> dict[int, str]:
    """A close for every day in the range, each one distinct: `<n>.25` for the n-th day."""
    return {epoch(first + timedelta(days=n)): f"{n + 1}.25" for n in range((last - first).days + 1)}


def echoing(closes: Mapping[int, str]) -> PriceFake:
    return PriceFake(
        coinbase_exchange=ScriptedVendor(Reply(renderer=coinbase_candles_echo(closes)))
    )


async def closes_between(
    fake: PriceFake,
    first: date,
    last: date,
    *,
    pair: PricePair = BTC_USD,
    clock: Callable[[], datetime] = AT_NOW,
) -> tuple[DailyClose, ...]:
    async with price_client(fake) as client:
        return await CoinbaseDailyCloses(client, clock=clock).daily_closes_between(
            pair, first, last
        )


async def test_a_700_day_range_is_three_sequential_requests_with_both_bounds() -> None:
    """`GET /products/BTC-USD/candles?granularity=86400&start=...Z&end=...Z`, three times.

    On the Exchange host and never the retail one, labelled `asset_daily_closes`, the
    windows oldest first and adjacent, and every day back exactly once, oldest first.
    """
    first = date(2020, 1, 1)
    last = first + timedelta(days=699)
    fake = echoing(every_day_from(first, last))

    closes = await closes_between(fake, first, last)

    assert fake.counts[COINBASE_EXCHANGE_HOST] == 3
    assert fake.counts[COINBASE_HOST] == 0
    assert fake.paths_of(COINBASE_EXCHANGE_HOST) == ["/products/BTC-USD/candles"] * 3
    assert fake.queries_of(COINBASE_EXCHANGE_HOST) == [
        query(first, date(2020, 10, 26)),
        query(date(2020, 10, 27), date(2021, 8, 22)),
        query(date(2021, 8, 23), last),
    ]
    for request in fake.coinbase_exchange.requests:
        assert request.method == "GET"
        assert request.extensions[ENDPOINT_EXTENSION] == ASSET_DAILY_CLOSES
    assert [close.day for close in closes] == [first + timedelta(days=n) for n in range(700)]
    assert closes[0].close == Decimal("1.25")
    assert closes[-1].close == Decimal("700.25")


async def test_today_is_never_asked_for_and_never_returned() -> None:
    """The range is cut at yesterday (UTC) before a request, and today's candle is dropped.

    The vendor here answers with today's candle regardless of the window, which is the
    defensive half: it is outside the asked window, so it is not a close.
    """
    body = coinbase_candles_body(
        [
            coinbase_candle(str(epoch(TODAY)), "9.75"),
            coinbase_candle(str(epoch(YESTERDAY)), "8.75"),
        ]
    )
    fake = PriceFake(coinbase_exchange=ScriptedVendor(Reply(body=body)))

    closes = await closes_between(fake, YESTERDAY - timedelta(days=1), TODAY + timedelta(days=3))

    assert fake.queries_of(COINBASE_EXCHANGE_HOST) == [
        query(YESTERDAY - timedelta(days=1), YESTERDAY)
    ]
    assert closes == (DailyClose(day=YESTERDAY, close=Decimal("8.75")),)


async def test_yesterday_is_the_utc_day_before_whatever_zone_the_clock_reads_in() -> None:
    """23:00 on the 7th at UTC-5 is 04:00 on the 8th in UTC: yesterday is the 7th."""
    late = datetime(2026, 10, 7, 23, 0, tzinfo=timezone(timedelta(hours=-5)))
    fake = echoing({})

    await closes_between(fake, date(2026, 10, 1), TODAY, clock=fixed_clock(late))

    assert fake.queries_of(COINBASE_EXCHANGE_HOST) == [query(date(2026, 10, 1), YESTERDAY)]


async def test_the_default_clock_is_the_wall_clock_in_utc() -> None:
    """Built without a clock, the last day asked for is the real yesterday (UTC)."""
    fake = echoing({})
    yesterday = datetime.now(UTC).date() - timedelta(days=1)

    async with price_client(fake) as client:
        await CoinbaseDailyCloses(client).daily_closes_between(
            BTC_USD, yesterday - timedelta(days=2), yesterday + timedelta(days=10)
        )

    (sent,) = fake.queries_of(COINBASE_EXCHANGE_HOST)
    assert sent.endswith(f"&end={yesterday.isoformat()}T00:00:00Z")


async def test_a_range_starting_before_the_first_candle_starts_at_it() -> None:
    """Nothing exists before 2015-07-20, so nothing before it is asked for."""
    fake = echoing({})

    await closes_between(fake, date(2010, 1, 1), date(2015, 7, 25))

    assert fake.queries_of(COINBASE_EXCHANGE_HOST) == [
        query(EARLIEST_CANDLE_DAY, date(2015, 7, 25))
    ]


@pytest.mark.parametrize(
    ("first", "last"),
    [
        pytest.param(date(2014, 1, 1), date(2015, 7, 19), id="wholly before the first candle"),
        pytest.param(date(2020, 1, 2), date(2020, 1, 1), id="an empty range"),
        pytest.param(TODAY, TODAY + timedelta(days=5), id="today and later"),
    ],
)
async def test_a_range_with_nothing_to_ask_makes_no_request(first: date, last: date) -> None:
    fake = echoing({})

    assert await closes_between(fake, first, last) == ()
    assert fake.requests == []


@pytest.mark.parametrize(
    "pair",
    [
        pytest.param((BTC, EUR), id="BTC/EUR, which the backfill does not read"),
        pytest.param((KAS, USD), id="KAS/USD, which Coinbase Exchange does not list"),
    ],
)
async def test_a_pair_it_does_not_read_is_refused_without_a_request(pair: PricePair) -> None:
    fake = echoing({})

    with pytest.raises(ProviderResponseError) as caught:
        await closes_between(fake, date(2020, 1, 1), date(2020, 1, 5), pair=pair)

    assert fake.requests == []
    assert f"{pair[0]}/{pair[1]}" in str(caught.value)


async def test_the_name_the_pairs_and_the_first_day() -> None:
    """`coinbase` in `price_history.source`, BTC/USD only, from 2015-07-20."""
    async with price_client(echoing({})) as client:
        source = CoinbaseDailyCloses(client)

        assert source.name == COINBASE == "coinbase"
        assert source.pairs == frozenset({BTC_USD})
        assert source.earliest_day == date(2015, 7, 20)


def test_the_shipped_constants_are_the_measured_ones() -> None:
    """As literals, because they are the vendor's, not ours -- and a different host."""
    assert COINBASE_EXCHANGE_API_URL == "https://api.exchange.coinbase.com"
    assert COINBASE_EXCHANGE_HOST == "api.exchange.coinbase.com"
    assert COINBASE_EXCHANGE_HOST != COINBASE_HOST
    assert CANDLES_PATH == "/products/{product}/candles"
    assert DAILY_GRANULARITY_SECONDS == 24 * 60 * 60
    assert MAX_DAYS_PER_REQUEST == 300
    assert date(2015, 7, 20) == EARLIEST_CANDLE_DAY
    assert dict(CANDLE_PRODUCTS) == {BTC_USD: PRODUCT}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param(429, ProviderRateLimitedError, id="throttled"),
        pytest.param(503, ProviderUnavailableError, id="unavailable"),
        pytest.param(400, ProviderResponseError, id="refused"),
    ],
)
async def test_a_failing_vendor_produces_the_typed_error_its_status_means(
    status: int,
    expected: type[Exception],
) -> None:
    fake = PriceFake(coinbase_exchange=ScriptedVendor(Reply(status=status)))

    with pytest.raises(expected) as caught:
        await closes_between(fake, date(2020, 1, 1), date(2020, 1, 5))

    assert COINBASE_EXCHANGE_HOST not in f"{caught.value}{caught.value!r}"


async def test_a_failure_on_a_later_window_fails_the_whole_range() -> None:
    """All or nothing: the first window answered, the second did not, and nothing returns.

    A range with a hole in it would be stored, and the next run would ask only for the days
    before the earliest stored close -- so the hole would never be asked for again.
    """
    first = date(2020, 1, 1)
    fake = PriceFake(
        coinbase_exchange=ScriptedVendor(
            Reply(renderer=coinbase_candles_echo(every_day_from(first, first))),
            Reply(status=503),
        )
    )

    with pytest.raises(ProviderUnavailableError):
        await closes_between(fake, first, first + timedelta(days=400))

    assert fake.counts[COINBASE_EXCHANGE_HOST] == 1 + 3, "one window, then three attempts"


# --------------------------------------------------------------------------------------
# Conformance, decided by mypy rather than by isinstance
# --------------------------------------------------------------------------------------

_CONFORMS: HistoricalCloseSource = CoinbaseDailyCloses(
    httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
)
