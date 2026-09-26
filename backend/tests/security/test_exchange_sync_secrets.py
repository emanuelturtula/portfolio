"""Criteria 7 and 8 of #15: no endpoint returns a credential, and no log line carries one.

## Criterion 7, by the schema

No response model has a field whose name says it holds a credential -- `key` other than
`exchange_key`, `secret`, `passphrase`, `credential`, `token`, `signature` -- and the
OpenAPI document is what is walked, because it is what every response is serialised
against. The walker is shown to find such a field in a planted document first, so a walk
that visits nothing cannot pass.

## Criterion 8, by the bytes

The **real application**, with Bitget configured through the three real environment
variables to sentinel values, the real registry building the real `BitgetProvider`, and the
fake Bitget venue of `tests/providers/exchanges/bitget_harness.py` on the transport -- which
verifies every signature, so the sentinels really were used to sign. It is driven through a
success, a 401, a 429 then a 200, and a 5xx, and every refusal's `msg` echoes the key the
way a careless venue does. Every response body of all three endpoints and every log record
-- the JSON the production pipeline writes to stdout, and every standard-library record,
caught by a handler of this test's own -- is searched for **every five-character window** of
each sentinel, and every twelve-character window of every signature sent. Spec 014's
lesson: a whole-value search passed while a secret's tail was on the startup log.

The sentinels are built at run time from short, distinctive cycles, so no literal in this
file looks like a credential to a scanner, and no five-character window of one occurs in
ordinary output by chance. None is assigned to a name containing a venue's name.
"""

from __future__ import annotations

import logging
import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from portfolio.config import get_settings
from portfolio.main import create_app
from portfolio.providers.exchanges.base import epoch_ms
from tests.auth.conftest import BASE_URL, JSON_HEADERS, sign_in
from tests.providers.exchanges.bitget_harness import (
    HTML_BODY,
    FakeBitget,
    Reply,
    VenueFill,
    error_body,
    fills_body,
)
from tests.providers.harness import retrying_client

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from fastapi import FastAPI

    from tests.security.conftest import ProductionLoggingInstaller

EXCHANGES: Final = "/api/exchanges"
SYNC: Final = "/api/exchanges/sync"
RUNS: Final = "/api/exchanges/runs"

#: Words a response field may not contain, and the one field allowed to contain `key`.
CREDENTIAL_WORDS: Final = ("key", "secret", "passphrase", "credential", "token", "signature")
ALLOWED_EXCHANGE_FIELDS: Final = frozenset({"exchange_key"})
#: Across the whole application, `chain_key` is the balance API's venue name, not a key.
ALLOWED_ANYWHERE: Final = frozenset({"exchange_key", "chain_key"})

WINDOW: Final = 5

#: The window a signature is searched for in. A signature is random Base64, and five of its
#: characters can occur in a log by chance -- a run of timestamp digits, say -- which would
#: fail this test for no leak. Twelve cannot, and a leaked fragment shorter than that is not
#: a signature anybody can use.
SIGNATURE_WINDOW: Final = 12

#: Built from cycles no ordinary output contains, and with no English in them: a tail like
#: `-access` would put `ccess` -- which `success` contains -- among the windows searched for.
#: Letters only, so each is header-safe printable ASCII, and fifty characters long.
ACCESS_SENTINEL: Final = "QzKxV" * 10
SIGNING_SENTINEL: Final = "WjPqY" * 10
PHRASE_SENTINEL: Final = "GfXbJ" * 10
SENTINELS: Final = (ACCESS_SENTINEL, SIGNING_SENTINEL, PHRASE_SENTINEL)

#: A venue that echoes the key in its refusals, as a careless one does.
ECHO: Final = f"apiKey {ACCESS_SENTINEL} is not valid"


# --------------------------------------------------------------------------------------
# Criterion 7: no response model has a credential field
# --------------------------------------------------------------------------------------


def response_field_names(
    document: Mapping[str, Any], paths: Iterator[str] | None = None
) -> set[str]:
    """Every property name reachable from the success responses of the given paths.

    Follows `$ref`, `items`, `anyOf`/`oneOf`/`allOf` and `additionalProperties`, each schema
    once. `paths` defaults to every path in the document.
    """
    schemas = document.get("components", {}).get("schemas", {})
    found: set[str] = set()
    seen: set[str] = set()

    def visit(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        reference = node.get("$ref")
        if isinstance(reference, str):
            name = reference.rsplit("/", 1)[-1]
            if name not in seen:
                seen.add(name)
                visit(schemas.get(name, {}))
        properties = node.get("properties")
        if isinstance(properties, dict):
            found.update(properties)
            visit(list(properties.values()))
        for keyword in ("items", "anyOf", "oneOf", "allOf", "additionalProperties"):
            visit(node.get(keyword))

    selected = list(paths) if paths is not None else list(document.get("paths", {}))
    for path in selected:
        for operation in document["paths"][path].values():
            if not isinstance(operation, dict):
                continue
            for status, response in operation.get("responses", {}).items():
                if not str(status).startswith("2"):
                    continue
                for media in response.get("content", {}).values():
                    visit(media.get("schema"))
    return found


def credential_like(names: set[str], allowed: frozenset[str]) -> set[str]:
    return {
        name
        for name in names
        if name not in allowed and any(word in name.lower() for word in CREDENTIAL_WORDS)
    }


def test_the_field_walk_finds_a_credential_field_in_a_planted_document() -> None:
    """The control: nested behind a `$ref`, an array and an `anyOf`, it is still found."""
    planted = {
        "paths": {
            "/api/exchanges": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/List"}
                                }
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "List": {
                    "properties": {"exchanges": {"items": {"$ref": "#/components/schemas/Account"}}}
                },
                "Account": {
                    "properties": {
                        "exchange_key": {"type": "string"},
                        "extra": {"anyOf": [{"$ref": "#/components/schemas/Leak"}, {}]},
                    }
                },
                "Leak": {"properties": {"api_key": {"type": "string"}}},
            }
        },
    }

    names = response_field_names(planted)

    assert {"exchanges", "exchange_key", "extra", "api_key"} <= names
    assert credential_like(names, ALLOWED_EXCHANGE_FIELDS) == {"api_key"}


def test_no_response_model_has_a_credential_field(app: FastAPI) -> None:
    """The three exchange endpoints: `configured` and a status, never a key."""
    document = app.openapi()
    exchange_paths = [path for path in document["paths"] if path.startswith(EXCHANGES)]
    assert sorted(exchange_paths) == [EXCHANGES, RUNS, SYNC]

    names = response_field_names(document, iter(exchange_paths))

    assert {"exchange_key", "configured", "status", "last_error", "detail", "joined"} <= names
    assert credential_like(names, ALLOWED_EXCHANGE_FIELDS) == set()


def test_no_response_anywhere_has_a_credential_field(app: FastAPI) -> None:
    """The same rule over every endpoint, so the next one is covered when it is added."""
    names = response_field_names(app.openapi())

    assert "chain_key" in names, "the walk reached the balance API"
    assert credential_like(names, ALLOWED_ANYWHERE) == set()


# --------------------------------------------------------------------------------------
# Criterion 8: the sentinel credentials reach no response and no log line
# --------------------------------------------------------------------------------------


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
    """The control: the last five characters of a sentinel are enough to fail."""
    leaked = f"request refused for key ...{ACCESS_SENTINEL[-5:]}"

    with pytest.raises(AssertionError, match="a planted line") as caught:
        assert_no_window(leaked, [ACCESS_SENTINEL], where="a planted line")

    assert ACCESS_SENTINEL not in str(caught.value), "the failure must not print the secret"
    assert_no_window("success exchange_sync_finished bitget", list(SENTINELS), where="text")


class EveryRecord(logging.Handler):
    """A root handler of this test's own: every standard-library record, rendered in full."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.rendered: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        parts = [record.name, record.getMessage(), repr(record.args), repr(record.__dict__)]
        if record.exc_info:
            parts.append("".join(traceback.format_exception(*record.exc_info)))
        if record.exc_text:
            parts.append(record.exc_text)
        self.rendered.append(" ".join(parts))


class SwitchingVenue:
    """The transport's handler: whichever fake Bitget the test has switched to."""

    def __init__(self, first: FakeBitget) -> None:
        self.current = first
        self.all: list[FakeBitget] = [first]

    def switch(self, fake: FakeBitget) -> None:
        self.current = fake
        self.all.append(fake)

    def handler(self, request: httpx.Request) -> httpx.Response:
        return self.current.handler(request)


def venue(**keywords: Any) -> FakeBitget:
    """A fake Bitget that verifies signatures against this test's sentinels."""
    return FakeBitget(
        signing_key=SIGNING_SENTINEL,
        access_key=ACCESS_SENTINEL,
        passphrase=PHRASE_SENTINEL,
        **keywords,
    )


def recent_fills() -> list[VenueFill]:
    """Five fills in the last five minutes of the real clock the application runs on."""
    now_ms = epoch_ms(datetime.now(UTC))
    return [
        VenueFill(trade_id=2_000_001 + index, executed_ms=now_ms - 60_000 * (5 - index))
        for index in range(5)
    ]


async def test_sentinel_credentials_reach_no_response_and_no_log_line(
    api_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del api_environment
    monkeypatch.setenv("PORTFOLIO_BITGET_API_KEY", ACCESS_SENTINEL)
    monkeypatch.setenv("PORTFOLIO_BITGET_API_SECRET", SIGNING_SENTINEL)
    monkeypatch.setenv("PORTFOLIO_BITGET_API_PASSPHRASE", PHRASE_SENTINEL)
    monkeypatch.setenv("PORTFOLIO_LOG_LEVEL", "DEBUG")
    get_settings.cache_clear()
    switching = SwitchingVenue(venue(fills=recent_fills()))
    monkeypatch.setattr(
        "portfolio.main.build_http_client",
        lambda: retrying_client(httpx.MockTransport(switching.handler)),
    )
    scenarios: list[tuple[str, FakeBitget | None]] = [
        ("success", None),
        ("a 401", venue(fill_replies=[Reply(status=401, body=error_body("40006", ECHO))])),
        (
            "a 429 then a 200",
            venue(
                fill_replies=[
                    Reply(status=429, body=error_body("429", ECHO), headers={"Retry-After": "1"}),
                    Reply(body=fills_body([])),
                ]
            ),
        ),
        ("a 5xx", venue(fill_replies=[Reply(status=502, body=error_body("45001", ECHO))])),
        ("a 5xx page of HTML", venue(fill_replies=[Reply(status=503, body=HTML_BODY)])),
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
            assert app.state.configured_exchanges == frozenset({"bitget"})
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
        request.headers["ACCESS-SIGN"] for fake in switching.all for request in fake.fill_requests
    ]

    # The positive companions: the credentials were really used, the scenarios really
    # happened, and the logs under search are the logs the application wrote.
    assert signatures, "no signed request reached the venue"
    for fake in switching.all:
        assert fake.signature_failures == [], "a request did not verify against the sentinels"
        assert all(
            request.headers["ACCESS-KEY"] == ACCESS_SENTINEL for request in fake.fill_requests
        )
    outcomes = [summary["accounts"][0] for summary in summaries]
    assert [outcome["status"] for outcome in outcomes] == [
        "success",
        "failed",
        "success",
        "failed",
        "failed",
    ]
    assert [outcome["error_kind"] for outcome in outcomes] == [
        None,
        "auth",
        None,
        "unavailable",
        "unavailable",
    ]
    assert outcomes[0]["fills_inserted"] == 5
    for marker in ("exchange_sync_finished", "exchange_sync_account_failed", "exchange_fills"):
        assert marker in written, f"stdout carried no {marker!r}: the log under test never ran"
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
