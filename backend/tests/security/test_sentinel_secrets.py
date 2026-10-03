"""Spec 030 (#23), criterion 7: every secret set to a sentinel, and none of them in any output.

**Every** `SecretStr` setting is given a distinct sentinel at once, and the set is checked
against `secret_values`, so a credential added later without a sentinel here fails the first
assertion rather than going unsearched. Then the **real application** runs in production form
at DEBUG through every path that holds one:

* a failed sign-in and a real one, with the bootstrap password the owner was created from;
* a balance sync of a registered testnet wallet, against chain indexes that fail;
* an exchange sync of both venues, each signing with its sentinels, against venues that
  refuse -- echoing back, as a careless vendor does, the full URL with its query and every
  header the request carried, keys and signatures included;
* a price refresh at startup, against price vendors that fail the same way, the CoinGecko
  key in its header;
* a request refused for want of a session whose path and query carry a sentinel;
* a 500 whose exception message carries every sentinel and a signed URL.

Searched: stdout **and stderr** at the file-descriptor level (`capfd`, spec 030 R13 S3, since
`logging`'s error fallback writes to stderr), pytest's `caplog`, a root handler of the test's
own that renders every record in full, and every response body and header. Each sentinel is
searched whole and as every window of `MIN_SUBSTRING_SECRET_LENGTH` characters, and every
signature the venues received as every twelve-character window.

The positive companions say the search was over something: each credential really reached
its vendor, each path really ran, and each capture holds the lines its path writes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qs

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from portfolio.config import Settings, get_settings
from portfolio.domain.passwords import OWASP_MINIMUM_MEMORY_COST, OWASP_MINIMUM_TIME_COST
from portfolio.logging import MIN_SUBSTRING_SECRET_LENGTH, secret_values
from portfolio.main import create_app
from tests.address_vectors import BIP173_TESTNET_P2WPKH
from tests.auth.conftest import BASE_URL, JSON_HEADERS, LOGIN_PATH, sign_in
from tests.providers.harness import retrying_client
from tests.security.conftest import EveryRecord

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

#: Letters only, so each is header-safe; cycles no ordinary output contains. Built at run
#: time, and none is assigned to a name that says which credential it is.
SENTINELS: Final[dict[str, str]] = {
    "bootstrap_password": "Owner phrase " + "HwRtK" * 6,
    "coingecko_api_key": "CG" + "PbMnQ" * 8,
    "bitget_api_key": "QzKxW" * 8,
    "bitget_api_secret": "WjPqZ" * 8,
    "bitget_api_passphrase": "GfXbL" * 8,
    "bingx_api_key": "RtLmS" * 8,
    "bingx_api_secret": "ZkVbT" * 8,
}
SIGNATURE_WINDOW: Final = 12
BOOM: Final = "/api/test/boom"
DEADLINE: Final = 10


def environment_name(field: str) -> str:
    return f"PORTFOLIO_{field.upper()}"


def windows_of(value: str, size: int) -> set[str]:
    return {value[start : start + size] for start in range(len(value) - size + 1)}


def assert_nothing_of(text: str, secrets: Iterable[str], *, where: str, size: int) -> None:
    """Fail naming the window and the line it reached, never printing a whole secret."""
    for secret in secrets:
        for window in windows_of(secret, size):
            if window in text:
                line = next(one for one in text.splitlines() if window in one)
                message = (
                    f"a {size}-character window of a secret reached {where}, on a line "
                    f"starting {line[:30]!r} at column {line.index(window)}"
                )
                raise AssertionError(message)


class CarelessVendors:
    """Every vendor fails, and echoes back the whole request: its URL and every header.

    Records each request, so the test can say which credentials were really sent.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        echo = f"rejected {request.url} with headers {dict(request.headers)}"
        host = request.url.host
        if "bitget" in host:
            return httpx.Response(401, json={"code": "40006", "msg": echo, "data": None})
        if "bingx" in host:
            return httpx.Response(200, json={"code": 100001, "msg": echo, "data": {}})
        if "coingecko" in host:
            return httpx.Response(401, json={"status": {"error_message": echo}})
        return httpx.Response(503, text=f"<html><body>{echo}</body></html>")

    def headers_named(self, name: str) -> list[str]:
        return [request.headers[name] for request in self.requests if name in request.headers]

    def signatures(self) -> list[str]:
        signed = self.headers_named("ACCESS-SIGN")
        for request in self.requests:
            signed.extend(parse_qs(request.url.query.decode()).get("signature", []))
        return signed


async def boom() -> None:
    message = (
        "a fault carrying every credential: "
        + " ".join(SENTINELS.values())
        + f" and https://open-api.example.test/x?signature={SENTINELS['bingx_api_secret']}"
    )
    raise RuntimeError(message)


def test_every_secret_setting_has_a_sentinel_here() -> None:
    """By introspection, so a credential added later fails here before it goes unsearched."""
    names = {
        name
        for name, field in Settings.model_fields.items()
        if "SecretStr" in str(field.annotation)
    }
    settings = Settings(
        _env_file=None,
        environment="dev",
        **{name: SecretStr(value) for name, value in SENTINELS.items()},  # type: ignore[arg-type]
    )

    assert names == set(SENTINELS)
    assert secret_values(settings) == frozenset(SENTINELS.values())
    assert len(set(SENTINELS.values())) == len(SENTINELS)
    assert all(len(value) >= MIN_SUBSTRING_SECRET_LENGTH for value in SENTINELS.values())


async def test_no_sentinel_reaches_any_output(
    api_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    restored_logging: None,
) -> None:
    del api_environment, restored_logging
    for field, value in SENTINELS.items():
        monkeypatch.setenv(environment_name(field), value)
    monkeypatch.setenv("PORTFOLIO_ENVIRONMENT", "prod")
    monkeypatch.setenv("PORTFOLIO_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("PORTFOLIO_ARGON2_MEMORY_COST", str(OWASP_MINIMUM_MEMORY_COST))
    monkeypatch.setenv("PORTFOLIO_ARGON2_TIME_COST", str(OWASP_MINIMUM_TIME_COST))
    monkeypatch.setenv("PORTFOLIO_PRICE_REFRESH_ENABLED", "true")
    monkeypatch.setenv("PORTFOLIO_BITCOIN_NETWORK", "testnet")
    monkeypatch.setenv("PORTFOLIO_BITCOIN_ESPLORA_URL", "https://esplora-one.example.test/api")
    monkeypatch.setenv(
        "PORTFOLIO_BITCOIN_ESPLORA_FALLBACK_URL", "https://esplora-two.example.test/api"
    )
    get_settings.cache_clear()
    vendors = CarelessVendors()
    monkeypatch.setattr(
        "portfolio.main.build_http_client",
        lambda: retrying_client(httpx.MockTransport(vendors.handler)),
    )

    assert secret_values(get_settings()) == frozenset(SENTINELS.values())
    app = create_app()
    app.add_api_route(BOOM, boom, methods=["GET"])
    records = EveryRecord()
    logging.getLogger().addHandler(records)
    logging.getLogger().addHandler(caplog.handler)
    bodies: list[str] = []
    try:
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
                async with asyncio.timeout(DEADLINE):
                    while app.state.price_scheduler.last_tick_finished_at is None:  # noqa: ASYNC110
                        await asyncio.sleep(0.01)
                refused = await client.get(
                    f"/api/{SENTINELS['bitget_api_key']}?apiKey={SENTINELS['bingx_api_key']}"
                )
                failed_login = await client.post(
                    LOGIN_PATH,
                    json={"username": "owner", "password": SENTINELS["bitget_api_secret"]},
                    headers=JSON_HEADERS,
                )
                await sign_in(client, phrase=SENTINELS["bootstrap_password"])
                created = await client.post(
                    "/api/wallets",
                    json={"chain_key": "bitcoin", "address": BIP173_TESTNET_P2WPKH},
                    headers=JSON_HEADERS,
                )
                balances = await client.post("/api/balances/sync", headers=JSON_HEADERS)
                exchanges = await client.post("/api/exchanges/sync", headers=JSON_HEADERS)
                detail = await client.get("/api/health/detail")
                crashed = await client.get(BOOM)
                # Not `refused`: its problem document's `instance` is the path the client
                # itself sent, sentinel and all, which discloses nothing to anybody.
                for response in (
                    failed_login,
                    created,
                    balances,
                    exchanges,
                    detail,
                    crashed,
                ):
                    bodies.append(response.text)
                    bodies.append(repr(dict(response.headers)))
    finally:
        logging.getLogger().removeHandler(records)
        logging.getLogger().removeHandler(caplog.handler)
        get_settings.cache_clear()
    captured = capfd.readouterr()

    # The positive companions: every path ran, and every credential reached its vendor.
    assert refused.status_code == 401
    assert SENTINELS["bingx_api_key"] not in refused.text, "the query is never echoed"
    assert failed_login.status_code == 401
    assert created.status_code == 201, created.text
    assert balances.status_code == 200, balances.text
    assert exchanges.status_code == 200, exchanges.text
    assert crashed.status_code == 500
    assert {account["status"] for account in exchanges.json()["accounts"]} == {"failed"}
    assert vendors.headers_named("ACCESS-KEY")
    assert set(vendors.headers_named("ACCESS-KEY")) == {SENTINELS["bitget_api_key"]}
    assert set(vendors.headers_named("ACCESS-PASSPHRASE")) == {SENTINELS["bitget_api_passphrase"]}
    assert set(vendors.headers_named("X-BX-APIKEY")) == {SENTINELS["bingx_api_key"]}
    assert set(vendors.headers_named("x-cg-demo-api-key")) == {SENTINELS["coingecko_api_key"]}
    assert any("esplora-one.example.test" in str(request.url) for request in vendors.requests)
    signatures = vendors.signatures()
    assert any(len(signature) == 64 for signature in signatures), "BingX signed nothing"
    stdout_events = {json.loads(line)["event"] for line in captured.out.splitlines() if line}
    for event in (
        "request_refused",
        "request_completed",
        "unhandled_exception",
        "exchange_sync_account_failed",
        "price_refresh_incomplete",
    ):
        assert event in stdout_events, f"stdout carried no {event!r}: that path never ran"
    assert caplog.records, "caplog saw nothing, so its absence below proves nothing"
    assert records.rendered, "the witness saw nothing, so its absence below proves nothing"

    everything = {
        "stdout": captured.out,
        "stderr": captured.err,
        "caplog": "\n".join(repr(record.__dict__) for record in caplog.records),
        "a log record": "\n".join(records.rendered),
        "a response": "\n".join(bodies),
    }
    for where, text in everything.items():
        assert_nothing_of(text, SENTINELS.values(), where=where, size=MIN_SUBSTRING_SECRET_LENGTH)
        assert_nothing_of(text, signatures, where=where, size=SIGNATURE_WINDOW)


def test_the_window_search_catches_a_leaked_fragment_and_ignores_the_rest() -> None:
    """The search above is only as good as this: a tail of a secret is a leak."""
    secret = SENTINELS["bingx_api_secret"]

    with pytest.raises(AssertionError, match="window of a secret reached stdout"):
        assert_nothing_of(
            f"prefix {secret[-MIN_SUBSTRING_SECRET_LENGTH:]} suffix",
            [secret],
            where="stdout",
            size=MIN_SUBSTRING_SECRET_LENGTH,
        )
    assert_nothing_of(
        '{"event": "request_completed", "status": 500}',
        SENTINELS.values(),
        where="stdout",
        size=MIN_SUBSTRING_SECRET_LENGTH,
    )
