"""Spec 017, R6: the real application with BingX configured leaks no credential or signature.

The sibling of `test_exchange_sync_secrets.py`, which does the same for Bitget, and built
the same way: the **real application**, BingX configured through its two real environment
variables to sentinel values, the real registry building the real `BingXProvider`, and the
fake venue of `tests/providers/exchanges/bingx_harness.py` on the transport. The fake
verifies every signature, so the sentinels really were used to sign.

BingX is the venue whose signature travels **in the query string**, so this is the test that
matters most for it: any log line holding a request's URL holds a signature. The run goes
through a success, refusals on HTTP 200 and on a 401, a 429 then a 200 (a replayed request),
a 5xx, an HTML 503 and a stale replay, and every refusal's `msg` echoes the key the way a
careless venue does. Every response body of the three exchange endpoints and every log record
-- the JSON on stdout and every standard-library record, caught by a handler of this test's
own -- is searched for every five-character window of each sentinel and every
twelve-character window of every signature sent.

The sentinels are cycles no ordinary output contains, built at run time, and neither is
assigned to a name containing the venue's name.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from portfolio.config import get_settings
from portfolio.main import create_app
from portfolio.providers.exchanges.base import epoch_ms
from tests.auth.conftest import BASE_URL, JSON_HEADERS, sign_in
from tests.providers.exchanges.bingx_harness import (
    HTML_BODY,
    FakeBingX,
    Reply,
    VenueFill,
    error_body,
    fills_body,
)
from tests.providers.harness import retrying_client
from tests.security.conftest import EveryRecord

if TYPE_CHECKING:
    from pathlib import Path

    from tests.security.conftest import ProductionLoggingInstaller

EXCHANGES: Final = "/api/exchanges"
SYNC: Final = "/api/exchanges/sync"
RUNS: Final = "/api/exchanges/runs"

WINDOW: Final = 5
#: Twelve, for a hex signature: five hex characters occur in a log by chance.
SIGNATURE_WINDOW: Final = 12

#: Letters only, so the key is header-safe; cycles with no English in them.
ACCESS_SENTINEL: Final = "RtLmQ" * 10
SIGNING_SENTINEL: Final = "ZkVbN" * 10
SENTINELS: Final = (ACCESS_SENTINEL, SIGNING_SENTINEL)

#: A venue that echoes the key in its refusals, as a careless one does.
ECHO: Final = f"apiKey {ACCESS_SENTINEL} is not valid"


def windows_of(value: str, size: int) -> set[str]:
    return {value[index : index + size] for index in range(len(value) - size + 1)}


def assert_no_window(written: str, secrets: list[str], *, where: str, size: int = WINDOW) -> None:
    """Fail naming the window and the line it reached, never the whole secret."""
    for secret in secrets:
        for window in sorted(windows_of(secret, size)):
            if window in written:
                line = next((one for one in written.splitlines() if window in one), written)
                message = f"{window!r} of a credential reached {where}: {line[:300]}"
                raise AssertionError(message)


def test_the_window_search_catches_a_leaked_tail_and_ignores_ordinary_output() -> None:
    """The control: five characters of a sentinel's tail are enough to fail."""
    leaked = f"request refused for key ...{SIGNING_SENTINEL[-5:]}"

    with pytest.raises(AssertionError, match="a planted line") as caught:
        assert_no_window(leaked, [SIGNING_SENTINEL], where="a planted line")

    assert SIGNING_SENTINEL not in str(caught.value), "the failure must not print the secret"
    assert_no_window(
        "success exchange_sync_finished bingx https://open-api.bingx.com/exchange_fills",
        list(SENTINELS),
        where="text",
    )


class SwitchingVenue:
    """The transport's handler: whichever fake BingX the test has switched to."""

    def __init__(self, first: FakeBingX) -> None:
        self.current = first
        self.all: list[FakeBingX] = [first]

    def switch(self, fake: FakeBingX) -> None:
        self.current = fake
        self.all.append(fake)

    def handler(self, request: httpx.Request) -> httpx.Response:
        return self.current.handler(request)


def venue(fills: list[VenueFill] | None = None, replies: list[Reply] | None = None) -> FakeBingX:
    """A fake BingX that verifies signatures against this test's sentinels."""
    return FakeBingX(
        fills or [],
        replies=replies or [],
        signing_key=SIGNING_SENTINEL,
        access_key=ACCESS_SENTINEL,
    )


def recent_fills() -> list[VenueFill]:
    """Five fills in the last five minutes of the real clock the application runs on."""
    now_ms = epoch_ms(datetime.now(UTC))
    return [
        VenueFill(trade_id=3_000_001 + index, executed_ms=now_ms - 60_000 * (5 - index))
        for index in range(5)
    ]


async def test_sentinel_bingx_credentials_reach_no_response_and_no_log_line(
    api_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del api_environment
    monkeypatch.setenv("PORTFOLIO_BINGX_API_KEY", ACCESS_SENTINEL)
    monkeypatch.setenv("PORTFOLIO_BINGX_API_SECRET", SIGNING_SENTINEL)
    monkeypatch.setenv("PORTFOLIO_LOG_LEVEL", "DEBUG")
    get_settings.cache_clear()
    switching = SwitchingVenue(venue(fills=recent_fills()))
    monkeypatch.setattr(
        "portfolio.main.build_http_client",
        lambda: retrying_client(httpx.MockTransport(switching.handler)),
    )
    scenarios: list[tuple[str, FakeBingX | None]] = [
        ("a success", None),
        ("a refusal on a 200", venue(replies=[Reply(body=error_body(100001, ECHO))])),
        ("a refusal on a 401", venue(replies=[Reply(status=401, body=error_body(100413, ECHO))])),
        (
            "a replayed request, a 429 then a 200",
            venue(
                replies=[
                    Reply(status=429, body=error_body(100410, ECHO), headers={"Retry-After": "1"}),
                    Reply(body=fills_body([])),
                ]
            ),
        ),
        ("a 5xx", venue(replies=[Reply(status=502, body=error_body(100500, ECHO))])),
        ("a 5xx page of HTML", venue(replies=[Reply(status=503, body=HTML_BODY)])),
        (
            "a stale replay",
            venue(
                replies=[Reply(status=503, body=HTML_BODY), Reply(body=error_body(100421, ECHO))]
            ),
        ),
    ]

    app = create_app()
    production_logging("DEBUG")
    records = EveryRecord()
    logging.getLogger().addHandler(records)
    bodies: list[str] = []
    summaries: list[dict[str, Any]] = []
    try:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client,
        ):
            assert app.state.configured_exchanges == frozenset({"bingx"})
            await sign_in(client)
            for _label, fake in scenarios:
                if fake is not None:
                    switching.switch(fake)
                synced = await client.post(SYNC, headers=JSON_HEADERS)
                listed = await client.get(EXCHANGES)
                runs = await client.get(RUNS)
                for response in (synced, listed, runs):
                    assert response.status_code == 200, response.text
                    bodies.append(response.text)
                    bodies.append(repr(dict(response.headers)))
                summaries.append(synced.json())
    finally:
        logging.getLogger().removeHandler(records)

    written = capsys.readouterr().out
    signatures = [
        query.rpartition("&signature=")[2] for fake in switching.all for query in fake.queries()
    ]

    # The positive companions: the credentials were really used, the scenarios really
    # happened, and the logs under search are the logs the application wrote.
    assert signatures, "no signed request reached the venue"
    assert all(len(signature) == 64 for signature in signatures)
    for fake in switching.all:
        assert fake.requests, "a scenario's venue was never asked"
        assert fake.signature_failures == [], "a request did not verify against the sentinels"
        assert all(request.headers["X-BX-APIKEY"] == ACCESS_SENTINEL for request in fake.requests)
    outcomes = [summary["accounts"][0] for summary in summaries]
    assert [outcome["exchange_key"] for outcome in outcomes] == ["bingx"] * len(scenarios)
    assert [outcome["status"] for outcome in outcomes] == [
        "success",
        "failed",
        "failed",
        "success",
        "failed",
        "failed",
        "failed",
    ]
    assert [outcome["error_kind"] for outcome in outcomes] == [
        None,
        "auth",
        "auth",
        None,
        "unavailable",
        "unavailable",
        "unavailable",
    ]
    assert outcomes[0]["fills_inserted"] == 5
    for marker in ("exchange_sync_finished", "exchange_sync_account_failed", "exchange_fills"):
        assert marker in written, f"stdout carried no {marker!r}: the log under test never ran"
    assert "https://open-api.bingx.com/exchange_fills" in written
    assert any("exchange_sync_finished" in line for line in records.rendered)

    everything = {
        "a response": "\n".join(bodies),
        "stdout": written,
        "a log record": "\n".join(records.rendered),
    }
    for where, text in everything.items():
        assert_no_window(text, list(SENTINELS), where=where)
        assert_no_window(text, signatures, where=where, size=SIGNATURE_WINDOW)
        assert ECHO not in text
        for fragment in ("myTrades", "signature=", "startTime="):
            assert fragment not in text, f"{fragment!r} of a request reached {where}"
