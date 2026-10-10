"""Spec 041's Kraken hourly read: the hourly closes out of one OHLC response, and the call.

`parse_hourly_closes` shares the envelope checks with the daily parser (spec 037), which
`test_kraken_daily_closes.py` drives refusal by refusal. What is new here is R1 of spec 041:
**a candle opens on the hour, its close is the price at the end of that hour, and the candle
still trading is never returned.** The bodies are literals, for the reason the daily module
gives, shaped like the one measured on 2026-10-10:

```
{"error":[],"result":{"KASUSD":[[1788998400,"0.03650","0.03650","0.03582","0.03640",
 "0.03618","968185.84274",147], ...],"last":1791586800}}
```
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import httpx
import pytest

from portfolio.providers.errors import ProviderResponseError
from portfolio.providers.http import ASSET_HOURLY_CLOSES, ENDPOINT_EXTENSION
from portfolio.providers.prices.base import BTC, EUR, KAS, USD, HourlyClose, HourlyCloseSource
from portfolio.providers.prices.kraken import (
    HOURLY_INTERVAL_MINUTES,
    HOURLY_PAIRS,
    KRAKEN,
    OHLC_PATH,
    KrakenHourlyCloses,
    parse_hourly_closes,
)
from tests.providers.prices.harness import (
    KRAKEN_HOST,
    PriceFake,
    Reply,
    ScriptedVendor,
    price_client,
)
from tests.providers.prices.test_kraken_daily_closes import candle, ohlc_body

if TYPE_CHECKING:
    from portfolio.providers.prices.base import PricePair

KAS_CODE: Final = "KASUSD"

#: Three consecutive hours from the measured response. 1791583200 is 2026-10-09T22:00:00Z.
HOUR_ONE: Final = 1791579600
HOUR_TWO: Final = 1791583200
HOUR_THREE: Final = 1791586800
NOW: Final = 1791590400
"""The fourth candle: the hour still trading, after `last`."""

#: One measured candle, byte for byte: close `0.04140`, every other price different.
MEASURED_CANDLE: Final = (
    '[1791586800,"0.04144","0.04146","0.04136","0.04140","0.04141","274456.81350",92]'
)


def at(timestamp: int) -> datetime:
    """The aware instant of an epoch second, computed apart from the parser."""
    return datetime.fromtimestamp(timestamp, UTC)


def echo_ohlc(request: httpx.Request) -> str:
    """A Kraken answering about exactly the pair the request named, with three hours + now."""
    code = request.url.params.get("pair", "")
    return ohlc_body(
        code,
        [
            candle(str(HOUR_ONE), '"1.5"'),
            candle(str(HOUR_TWO), '"2.5"'),
            candle(str(HOUR_THREE), '"3.5"'),
            candle(str(NOW), '"4.5"'),
        ],
        last=str(HOUR_THREE),
    )


async def closes_of(fake: PriceFake, pair: PricePair) -> tuple[HourlyClose, ...]:
    """One `hourly_closes` against the scripted vendor, with the client closed afterwards."""
    async with price_client(fake) as client:
        return await KrakenHourlyCloses(client).hourly_closes(pair)


def answering() -> PriceFake:
    return PriceFake(kraken=ScriptedVendor(Reply(renderer=echo_ohlc)))


def test_every_committed_close_comes_back_oldest_first_with_its_hour() -> None:
    """The hour is the open time, aware and UTC; the close is index four; `now` is skipped."""
    body = ohlc_body(
        KAS_CODE,
        [
            candle(str(HOUR_ONE), '"0.04100"'),
            candle(str(HOUR_TWO), '"0.04120"'),
            MEASURED_CANDLE,
            candle(str(NOW), '"0.04148"'),
        ],
        last=str(HOUR_THREE),
    )

    closes = parse_hourly_closes(body, KAS_CODE)

    assert closes == (
        HourlyClose(hour=at(HOUR_ONE), close=Decimal("0.04100")),
        HourlyClose(hour=at(HOUR_TWO), close=Decimal("0.04120")),
        HourlyClose(hour=at(HOUR_THREE), close=Decimal("0.04140")),
    )
    assert all(close.hour.tzinfo is UTC for close in closes)


def test_a_candle_that_does_not_open_on_the_hour_is_refused() -> None:
    """Half past is not an hourly candle, so the series cannot be trusted as one."""
    body = ohlc_body(KAS_CODE, [candle(str(HOUR_ONE + 1800), '"1.0"')], last=str(HOUR_THREE))

    with pytest.raises(ProviderResponseError, match="does not open at the hour"):
        parse_hourly_closes(body, KAS_CODE)


def test_a_midnight_is_an_hour_like_any_other() -> None:
    """A daily boundary is also an hourly one: 2026-10-10T00:00:00Z is accepted."""
    body = ohlc_body(KAS_CODE, [candle(str(NOW), '"1.0"')], last=str(NOW))

    assert parse_hourly_closes(body, KAS_CODE) == (HourlyClose(hour=at(NOW), close=Decimal("1.0")),)


@pytest.mark.parametrize(
    "hours",
    [
        pytest.param((HOUR_TWO, HOUR_ONE), id="out of order"),
        pytest.param((HOUR_ONE, HOUR_ONE), id="one hour twice"),
    ],
)
def test_candles_not_one_per_hour_oldest_first_are_refused(hours: tuple[int, int]) -> None:
    body = ohlc_body(KAS_CODE, [candle(str(hour), '"1.0"') for hour in hours], last=str(HOUR_THREE))

    with pytest.raises(ProviderResponseError, match="not one per hour"):
        parse_hourly_closes(body, KAS_CODE)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('{"error":[],"result":{"last":1}}', id="no candles for the pair"),
        pytest.param(f'{{"error":[],"result":{{"{KAS_CODE}":[]}}}}', id="no last"),
        pytest.param(
            f'{{"error":[],"result":{{"{KAS_CODE}":[],"XXBTZUSD":[],"last":1}}}}',
            id="an unrequested pair",
        ),
    ],
)
def test_the_envelope_refusals_are_the_daily_parsers(body: str) -> None:
    with pytest.raises(ProviderResponseError):
        parse_hourly_closes(body, KAS_CODE)


@pytest.mark.parametrize("pair", [(BTC, USD), (KAS, USD)])
async def test_one_request_per_pair_with_the_documented_path_query_and_label(
    pair: PricePair,
) -> None:
    """`GET /0/public/OHLC?pair=<code>&interval=60`, labelled `asset_hourly_closes`."""
    fake = answering()

    closes = await closes_of(fake, pair)

    code = {(BTC, USD): "XXBTZUSD", (KAS, USD): KAS_CODE}[pair]
    assert fake.paths_of(KRAKEN_HOST) == [OHLC_PATH]
    assert fake.queries_of(KRAKEN_HOST) == [f"pair={code}&interval={HOURLY_INTERVAL_MINUTES}"]
    assert fake.kraken.requests[0].extensions[ENDPOINT_EXTENSION] == ASSET_HOURLY_CLOSES
    assert [close.hour for close in closes] == [at(HOUR_ONE), at(HOUR_TWO), at(HOUR_THREE)]


async def test_a_pair_outside_the_hourly_pairs_is_refused_without_a_request() -> None:
    fake = answering()

    with pytest.raises(ProviderResponseError, match="BTC/EUR"):
        await closes_of(fake, (BTC, EUR))

    assert fake.requests == []


async def test_the_name_the_pairs_and_the_interval() -> None:
    assert HOURLY_INTERVAL_MINUTES == 60
    assert frozenset({(BTC, USD), (KAS, USD)}) == HOURLY_PAIRS
    async with price_client(answering()) as client:
        source = KrakenHourlyCloses(client)

        assert source.name == KRAKEN
        assert source.pairs == HOURLY_PAIRS


_CONFORMS: HourlyCloseSource = KrakenHourlyCloses(
    httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
)
