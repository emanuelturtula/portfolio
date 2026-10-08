"""Spec 037's Kraken backfill read: the daily closes out of one OHLC response, and the call.

`parse_daily_closes` is where R1 lives -- **a candle's day is the UTC date of its open time,
and the candle still trading is never stored** -- so most of this module is that parser,
driven with bodies shaped exactly like the one measured on 2026-10-08:

```
{"error":[],"result":{"XXBTZUSD":[[1729209600,"67407.8","68980.3","67189.9","68440.3",
 "68262.2","1519.44740947",32011], ...],"last":1729296000}}
```

## Every other field in a candle is a different number from its close

A candle is `[time, open, high, low, close, vwap, volume, count]`, all prices as strings of
the same shape. A parser reaching for the open, the high or the VWAP returns a plausible
price nobody would question, so the bodies here give every field a distinct value and the
assertions name the close. The same reasoning `harness.kraken_body` applies to the ticker.

## The bodies are literals

For the reason `harness.py` gives: a body assembled with `json.dumps` would put the
expectation and the code under test through the same conversion. The candles are built from
string pieces, never from Python numbers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
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
from portfolio.providers.prices.kraken import (
    BACKFILL_PAIRS,
    DAILY_INTERVAL_MINUTES,
    KRAKEN,
    OHLC_PATH,
    PAIR_CODES,
    KrakenDailyCloses,
    parse_daily_closes,
)
from tests.providers.prices.harness import (
    KRAKEN_HOST,
    PriceFake,
    Reply,
    ScriptedVendor,
    price_client,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from portfolio.providers.prices.base import DailyCloseSource, PricePair

BTC_CODE: Final = "XXBTZUSD"
KAS_CODE: Final = "KASUSD"

#: Four consecutive UTC midnights from the measured response, as the integers Kraken sends.
#: 1729209600 is 2024-10-18T00:00:00Z, the first day `XXBTZUSD` returned on 2026-10-08.
DAY_ONE: Final = 1729209600
DAY_TWO: Final = 1729296000
DAY_THREE: Final = 1729382400
TODAY: Final = 1729468800
"""The fourth candle: the one still trading, after `last`."""

COMMITTED: Final = (DAY_ONE, DAY_TWO, DAY_THREE)

#: One measured candle, byte for byte: close `68440.3`, and every other price different.
MEASURED_CANDLE: Final = (
    '[1729209600,"67407.8","68980.3","67189.9","68440.3","68262.2","1519.44740947",32011]'
)


def candle(opened: str, close: str) -> str:
    """One OHLC entry, with the close at index 4 and every other price distinct from it.

    `opened` is the literal JSON token for the time -- `1729209600`, `"1729209600"`,
    `false` -- so a refusal of the time's type is written as the token that causes it.
    `close` is a literal token too, normally a quoted string as Kraken sends it.
    """
    return f'[{opened},"1.1","2.2","0.5",{close},"1.7","10.00000000",42]'


def ohlc_body(
    code: str,
    candles: Sequence[str],
    *,
    last: str,
    errors: Sequence[str] = (),
    extra: str = "",
) -> str:
    """Kraken's OHLC envelope: `error`, and `result` holding the pair's candles and `last`.

    `last` is a literal token for the same reason `candle`'s time is. `extra` is spliced
    into `result` verbatim, for the body that answers about a pair nobody asked for.
    """
    rendered_errors = ",".join(f'"{error}"' for error in errors)
    entries = ",".join(candles)
    return f'{{"error":[{rendered_errors}],"result":{{"{code}":[{entries}]{extra},"last":{last}}}}}'


#: Three committed days and the one still trading, as Kraken answers every call.
REALISTIC_BODY: Final = ohlc_body(
    BTC_CODE,
    [
        MEASURED_CANDLE,
        candle(str(DAY_TWO), '"68380.1"'),
        candle(str(DAY_THREE), '"69001.50000"'),
        candle(str(TODAY), '"70000.0"'),
    ],
    last=str(DAY_THREE),
)


def utc_day(timestamp: int) -> date:
    """The UTC date of an epoch second, computed apart from the parser for the expectation."""
    return datetime.fromtimestamp(timestamp, UTC).date()


# --------------------------------------------------------------------------------------
# The happy path, and R1
# --------------------------------------------------------------------------------------


def test_every_committed_close_comes_back_oldest_first_with_its_utc_day() -> None:
    """Three committed candles become three closes; the day is the UTC date of the open.

    The days are written out as dates rather than derived, so a parser that took the local
    date, or the close time a day later, disagrees with a literal.
    """
    closes = parse_daily_closes(REALISTIC_BODY, BTC_CODE)

    assert closes == (
        DailyClose(day=date(2024, 10, 18), close=Decimal("68440.3")),
        DailyClose(day=date(2024, 10, 19), close=Decimal("68380.1")),
        DailyClose(day=date(2024, 10, 20), close=Decimal("69001.50000")),
    )
    assert [close.day for close in closes] == [utc_day(day) for day in COMMITTED]


def test_the_close_is_index_four_and_not_the_open_the_high_the_low_or_the_vwap() -> None:
    """The measured candle's other prices, each of which a wrong index would have returned."""
    body = ohlc_body(BTC_CODE, [MEASURED_CANDLE], last=str(DAY_ONE))

    (only,) = parse_daily_closes(body, BTC_CODE)

    assert only.close == Decimal("68440.3")
    for wrong in ("67407.8", "68980.3", "67189.9", "68262.2"):
        assert only.close != Decimal(wrong)


def test_a_close_keeps_the_digits_the_vendor_sent() -> None:
    """A string close is a `Decimal` built from its characters, trailing zeros included."""
    (_, _, third) = parse_daily_closes(REALISTIC_BODY, BTC_CODE)

    assert str(third.close) == "69001.50000"


def test_the_candle_still_trading_is_never_returned() -> None:
    """R1: the entry after `last` is a price still moving, so it is skipped, not stored.

    Kraken documents it as always present, so every real answer carries one. A parser that
    kept it would write today's half-day price as today's close, and the next backfill would
    overwrite it with a different "close" for the same day.
    """
    closes = parse_daily_closes(REALISTIC_BODY, BTC_CODE)

    assert utc_day(TODAY) not in {close.day for close in closes}
    assert Decimal("70000.0") not in {close.close for close in closes}
    assert len(closes) == len(COMMITTED)


def test_every_entry_after_last_is_skipped_whatever_last_says() -> None:
    """`last` decides, not the position: with `last` on day one, days two to four are skipped.

    And the skipped entries are not order-checked against the kept ones: an entry after
    `last` never becomes a close, so it cannot make the series inconsistent.
    """
    body = ohlc_body(
        BTC_CODE,
        [
            candle(str(DAY_ONE), '"1.5"'),
            candle(str(DAY_THREE), '"3.5"'),
            candle(str(DAY_TWO), '"2.5"'),
            candle(str(TODAY), '"4.5"'),
        ],
        last=str(DAY_ONE),
    )

    assert parse_daily_closes(body, BTC_CODE) == (
        DailyClose(day=utc_day(DAY_ONE), close=Decimal("1.5")),
    )


def test_an_answer_with_no_candles_is_an_empty_series_rather_than_a_refusal() -> None:
    """A pair with nothing committed yet -- KAS before its first day on Kraken -- is `()`."""
    body = ohlc_body(KAS_CODE, [], last=str(DAY_ONE))

    assert parse_daily_closes(body, KAS_CODE) == ()


def test_only_the_moving_candle_is_an_empty_series_too() -> None:
    """A pair listed today has one entry, still trading, and nothing to store."""
    body = ohlc_body(KAS_CODE, [candle(str(TODAY), '"0.1"')], last=str(DAY_THREE))

    assert parse_daily_closes(body, KAS_CODE) == ()


def test_a_close_in_a_json_number_is_read_from_its_digits() -> None:
    """Not Kraken's shape, but `require_price` decides, once, and a number keeps its digits.

    `decode_json` builds a JSON number as a `Decimal` from its text, so a vendor switching
    to numbers would lose nothing here -- asserted so the shared boundary stays the only one.
    """
    body = ohlc_body(BTC_CODE, [candle(str(DAY_ONE), "68440.30")], last=str(DAY_ONE))

    (only,) = parse_daily_closes(body, BTC_CODE)

    assert str(only.close) == "68440.30"


# --------------------------------------------------------------------------------------
# Every refusal in the parser's table
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        # The envelope, shared with the ticker through `_result_of`.
        pytest.param("not json", id="not JSON"),
        pytest.param("[]", id="a JSON array rather than an object"),
        pytest.param('{"result": {}}', id="no error list"),
        pytest.param('{"error": "none", "result": {}}', id="an error field that is not a list"),
        pytest.param(
            ohlc_body(BTC_CODE, [], last=str(DAY_ONE), errors=["EQuery:Unknown asset pair"]),
            id="errors reported in the envelope",
        ),
        pytest.param('{"error": []}', id="no result object"),
        pytest.param('{"error": [], "result": []}', id="a result that is not an object"),
        # The pair.
        pytest.param(
            ohlc_body(BTC_CODE, [MEASURED_CANDLE], last=str(DAY_ONE), extra=',"XXBTZEUR":[]'),
            id="a pair that was not requested",
        ),
        pytest.param(f'{{"error": [], "result": {{"last": {DAY_ONE}}}}}', id="the pair absent"),
        pytest.param(
            f'{{"error": [], "result": {{"{BTC_CODE}": {{}}, "last": {DAY_ONE}}}}}',
            id="the pair an object rather than a list",
        ),
        # `last`.
        pytest.param(
            f'{{"error": [], "result": {{"{BTC_CODE}": [{MEASURED_CANDLE}]}}}}',
            id="last absent",
        ),
        pytest.param(
            ohlc_body(BTC_CODE, [MEASURED_CANDLE], last=f'"{DAY_ONE}"'),
            id="last a string",
        ),
        pytest.param(
            ohlc_body(BTC_CODE, [MEASURED_CANDLE], last=f"{DAY_ONE}.0"),
            id="last a JSON fraction",
        ),
        pytest.param(
            ohlc_body(BTC_CODE, [MEASURED_CANDLE], last="null"),
            id="last null",
        ),
        # The candle's shape.
        pytest.param(
            ohlc_body(BTC_CODE, ['{"time": 1729209600}'], last=str(DAY_ONE)),
            id="a candle that is an object",
        ),
        pytest.param(
            ohlc_body(BTC_CODE, ['"1729209600"'], last=str(DAY_ONE)),
            id="a candle that is a string",
        ),
        pytest.param(
            ohlc_body(BTC_CODE, ['[1729209600,"1.1","2.2","0.5"]'], last=str(DAY_ONE)),
            id="a candle of four, with no close",
        ),
        # The candle's time.
        pytest.param(
            ohlc_body(BTC_CODE, [candle(f'"{DAY_ONE}"', '"1.5"')], last=str(DAY_ONE)),
            id="a time that is a string",
        ),
        pytest.param(
            ohlc_body(BTC_CODE, [candle(f"{DAY_ONE}.0", '"1.5"')], last=str(DAY_ONE)),
            id="a time that is a JSON fraction",
        ),
        pytest.param(
            ohlc_body(BTC_CODE, [candle(str(DAY_ONE + 3600), '"1.5"')], last=str(DAY_TWO)),
            id="a time an hour past midnight",
        ),
        pytest.param(
            ohlc_body(BTC_CODE, [candle(str(DAY_ONE + 1), '"1.5"')], last=str(DAY_TWO)),
            id="a time a second past midnight",
        ),
        # The close.
        pytest.param(
            ohlc_body(BTC_CODE, [candle(str(DAY_ONE), '"0"')], last=str(DAY_ONE)),
            id="a zero close",
        ),
        pytest.param(
            ohlc_body(BTC_CODE, [candle(str(DAY_ONE), '"-1"')], last=str(DAY_ONE)),
            id="a negative close",
        ),
        pytest.param(
            ohlc_body(BTC_CODE, [candle(str(DAY_ONE), '"not a number"')], last=str(DAY_ONE)),
            id="a close that is not a number",
        ),
        pytest.param(
            ohlc_body(BTC_CODE, [candle(str(DAY_ONE), '"NaN"')], last=str(DAY_ONE)),
            id="a close that is NaN",
        ),
        pytest.param(
            ohlc_body(BTC_CODE, [candle(str(DAY_ONE), "null")], last=str(DAY_ONE)),
            id="a close that is null",
        ),
        pytest.param(
            ohlc_body(BTC_CODE, [candle(str(DAY_ONE), "true")], last=str(DAY_ONE)),
            id="a close that is a bool",
        ),
        # The series.
        pytest.param(
            ohlc_body(
                BTC_CODE,
                [candle(str(DAY_ONE), '"1.5"'), candle(str(DAY_ONE), '"1.6"')],
                last=str(DAY_TWO),
            ),
            id="two candles for one day",
        ),
        pytest.param(
            ohlc_body(
                BTC_CODE,
                [candle(str(DAY_TWO), '"2.5"'), candle(str(DAY_ONE), '"1.5"')],
                last=str(DAY_TWO),
            ),
            id="newest first",
        ),
    ],
)
def test_a_body_that_cannot_be_trusted_is_a_typed_refusal(body: str) -> None:
    """Every row of the docstring's table, from the shape that causes it.

    `ProviderResponseError` in every case and never a `KeyError`, `TypeError` or
    `IndexError`: the backfill catches `ProviderError` and nothing else, so an untyped escape
    would end the whole run instead of becoming one failed pair in its report.
    """
    with pytest.raises(ProviderResponseError):
        parse_daily_closes(body, BTC_CODE)


def test_a_time_of_false_is_refused_although_false_is_a_midnight() -> None:
    """`False` is an `int` equal to 0, which is 1970-01-01T00:00:00Z: a UTC midnight.

    Without the `bool` check this candle would pass both other tests on the time and be
    stored as the close of the first day of the epoch.
    """
    body = ohlc_body(BTC_CODE, [candle("false", '"1.5"')], last=str(DAY_ONE))

    with pytest.raises(ProviderResponseError, match="UTC midnight"):
        parse_daily_closes(body, BTC_CODE)


def test_a_last_of_true_is_refused_rather_than_read_as_one() -> None:
    """`True` is an `int` equal to 1, so every candle would be "after last" and skipped.

    That is an empty series for a pair Kraken answered in full: no error, no closes, and a
    backfill reporting zero days as if there were none to store.
    """
    body = ohlc_body(BTC_CODE, [MEASURED_CANDLE], last="true")

    with pytest.raises(ProviderResponseError, match="'last'"):
        parse_daily_closes(body, BTC_CODE)


def test_the_envelope_error_carries_a_count_and_none_of_the_vendors_prose() -> None:
    """The shared `_result_of` rule, seen through the OHLC parser."""
    body = ohlc_body(BTC_CODE, [], last=str(DAY_ONE), errors=["EQuery:Unknown asset pair"])

    with pytest.raises(ProviderResponseError) as caught:
        parse_daily_closes(body, BTC_CODE)

    assert "1 error" in str(caught.value)
    assert "Unknown asset pair" not in str(caught.value)


def test_an_unrequested_pair_is_counted_and_last_is_not_one_of_them() -> None:
    """`last` sits beside the candles in `result` and is not a pair; anything else is."""
    body = ohlc_body(
        BTC_CODE, [MEASURED_CANDLE], last=str(DAY_ONE), extra=',"XXBTZEUR":[],"KASUSD":[]'
    )

    with pytest.raises(ProviderResponseError) as caught:
        parse_daily_closes(body, BTC_CODE)

    assert "2 pair(s)" in str(caught.value)


def test_the_refusals_name_the_pair_code_and_never_a_value() -> None:
    """A pair code is public vendor vocabulary; a body's contents go in no message."""
    suspicious = "1729209601"
    bad_time = ohlc_body(BTC_CODE, [candle(suspicious, '"1.5"')], last=str(DAY_TWO))
    bad_close = ohlc_body(
        BTC_CODE, [candle(str(DAY_ONE), '"a-suspicious-value"')], last=str(DAY_ONE)
    )
    out_of_order = ohlc_body(
        BTC_CODE,
        [candle(str(DAY_TWO), '"2.5"'), candle(str(DAY_ONE), '"1.5"')],
        last=str(DAY_TWO),
    )

    with pytest.raises(ProviderResponseError) as time_refusal:
        parse_daily_closes(bad_time, BTC_CODE)
    with pytest.raises(ProviderResponseError) as close_refusal:
        parse_daily_closes(bad_close, BTC_CODE)
    with pytest.raises(ProviderResponseError) as order_refusal:
        parse_daily_closes(out_of_order, BTC_CODE)

    assert BTC_CODE in str(time_refusal.value)
    assert suspicious not in str(time_refusal.value)
    assert "a-suspicious-value" not in str(close_refusal.value)
    assert BTC_CODE in str(order_refusal.value)
    assert "2.5" not in str(order_refusal.value)


# --------------------------------------------------------------------------------------
# The source: one call per pair, the documented query, the backfill's own label
# --------------------------------------------------------------------------------------


def echo_ohlc(request: httpx.Request) -> str:
    """A Kraken answering about exactly the pair the request named, with three days + today.

    Echoing rather than fixed, so the source's query decides which pair comes back: a source
    that asked for the wrong code gets an answer under that code, and the parser refuses it
    as unrequested -- or finds nothing under the code it expected.
    """
    code = request.url.params.get("pair", "")
    return ohlc_body(
        code,
        [
            candle(str(DAY_ONE), '"1.5"'),
            candle(str(DAY_TWO), '"2.5"'),
            candle(str(DAY_THREE), '"3.5"'),
            candle(str(TODAY), '"4.5"'),
        ],
        last=str(DAY_THREE),
    )


def answering() -> PriceFake:
    return PriceFake(kraken=ScriptedVendor(Reply(renderer=echo_ohlc)))


async def closes_of(fake: PriceFake, pair: PricePair) -> tuple[DailyClose, ...]:
    """One `daily_closes` against the scripted vendor, with the client closed afterwards."""
    async with price_client(fake) as client:
        return await KrakenDailyCloses(client).daily_closes(pair)


@pytest.mark.parametrize(
    ("pair", "code"),
    [
        pytest.param((BTC, USD), BTC_CODE, id="BTC/USD"),
        pytest.param((KAS, USD), KAS_CODE, id="KAS/USD"),
    ],
)
async def test_one_request_per_pair_with_the_documented_path_query_and_label(
    pair: PricePair,
    code: str,
) -> None:
    """`GET /0/public/OHLC?pair=<code>&interval=1440`, labelled `asset_daily_closes`.

    The query is asserted as text: one `pair` and one `interval`, in that order, which is the
    request the 2026-10-08 measurement made. The label is what makes a backfill visible in a
    log apart from the hourly ticker read.
    """
    fake = answering()

    closes = await closes_of(fake, pair)

    assert fake.counts[KRAKEN_HOST] == 1
    assert fake.paths_of(KRAKEN_HOST) == [OHLC_PATH]
    assert fake.queries_of(KRAKEN_HOST) == [f"pair={code}&interval={DAILY_INTERVAL_MINUTES}"]
    request = fake.kraken.requests[0]
    assert request.method == "GET"
    assert request.extensions[ENDPOINT_EXTENSION] == ASSET_DAILY_CLOSES
    assert [close.close for close in closes] == [Decimal("1.5"), Decimal("2.5"), Decimal("3.5")]
    assert [close.day for close in closes] == [utc_day(day) for day in COMMITTED]


def test_the_shipped_constants_are_the_documented_ones() -> None:
    """The path and the interval as literals, because they are the vendor's, not ours."""
    assert OHLC_PATH == "/0/public/OHLC"
    assert DAILY_INTERVAL_MINUTES == 24 * 60
    assert frozenset({(BTC, USD), (KAS, USD)}) == BACKFILL_PAIRS
    assert {PAIR_CODES[pair] for pair in BACKFILL_PAIRS} == {BTC_CODE, KAS_CODE}


@pytest.mark.parametrize(
    "pair",
    [
        pytest.param((BTC, EUR), id="a pair Kraken lists but the backfill does not read"),
        pytest.param((KAS, EUR), id="KAS/EUR"),
        pytest.param(("XRP", USD), id="a pair Kraken is not asked about at all"),
    ],
)
async def test_a_pair_outside_the_backfill_is_refused_without_a_request(pair: PricePair) -> None:
    """Typed, before a URL is built, and naming the pair rather than raising a `KeyError`.

    EUR is refused although `PAIR_CODES` has a code for it: the dashboard values in USD, and
    an EUR history nobody draws would double the calls for nothing (spec 037).
    """
    fake = answering()

    with pytest.raises(ProviderResponseError) as caught:
        await closes_of(fake, pair)

    assert fake.requests == []
    assert f"{pair[0]}/{pair[1]}" in str(caught.value)


async def test_the_name_and_the_pairs_are_the_backfills() -> None:
    """`kraken` in `price_history.source`, and exactly the two USD pairs."""
    async with price_client(answering()) as client:
        source = KrakenDailyCloses(client)

        assert source.name == KRAKEN
        assert source.pairs == BACKFILL_PAIRS


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
    """The shared endpoint's three-way split, so the backfill records one failed pair."""
    fake = PriceFake(kraken=ScriptedVendor(Reply(status=status)))

    with pytest.raises(expected) as caught:
        await closes_of(fake, (BTC, USD))

    assert KRAKEN_HOST not in f"{caught.value}{caught.value!r}"


async def test_an_untrustworthy_answer_from_the_vendor_is_a_typed_refusal() -> None:
    """A 200 carrying Kraken's own error list reaches the caller as `ProviderResponseError`."""
    body = ohlc_body(BTC_CODE, [], last=str(DAY_ONE), errors=["EGeneral:Too many requests"])
    fake = PriceFake(kraken=ScriptedVendor(Reply(body=body)))

    with pytest.raises(ProviderResponseError):
        await closes_of(fake, (BTC, USD))


# --------------------------------------------------------------------------------------
# Conformance, decided by mypy rather than by isinstance
# --------------------------------------------------------------------------------------

_CONFORMS: DailyCloseSource = KrakenDailyCloses(
    httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
)
