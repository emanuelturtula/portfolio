"""Spec 030 (#23), criteria 5 and 6, and rulings R5, R6, R9 and R11: the request's own context.

The real application, through its real lifespan and middleware, with the production pipeline
installed inside the test so stdout is the JSON the Raspberry Pi writes. Two routes are added
to it for the test: one that writes records -- through structlog and through a standard-library
logger, yielding to the event loop between them -- and one that raises. What is pinned:

* **`X-Request-ID` on every response**: 200, 401, 404, 422 and 500; a hyphenated UUID4,
  different for every request; an inbound `X-Request-ID` is never echoed and never logged;
* **one `request_id` on every record a request writes**, structlog's and the standard
  library's, and the same as the header; **concurrent requests never share or swap one**;
* **`request_completed`**: the method, the route template, the status and `duration_ms`, never
  the raw path or the query; at DEBUG for `GET /api/health` and for a path outside `/api`, at
  INFO otherwise; `duration_ms` in whole milliseconds from the injected clock;
* **the route label** (R6, R9): the prefixed template for every one of the API's operations,
  read off its own OpenAPI document; a documentation path's own path; `spa` outside `/api`;
  `unmatched` for a path under `/api` that routing never matched or never reached;
* **a 200 KB adversarial path from a client with no session is answered within a second**
  (R13, M1), and its `request_refused` line stays under 1 KB: at most `LOGGED_PATH_LIMIT`
  characters of the path, cut back to a `/` so no segment is written in part, and its length.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import anyio
import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from portfolio.api.middleware import LOGGED_PATH_LIMIT, is_api_path, logged_path
from portfolio.api.request_context import (
    HEALTH_CHECK_PATH,
    REQUEST_COMPLETED_EVENT,
    REQUEST_ID_HEADER,
    SPA_ROUTE,
    UNMATCHED_ROUTE,
    RequestContextMiddleware,
    current_request_id,
    documentation_paths,
)
from portfolio.config import Settings
from portfolio.domain.passwords import OWASP_MINIMUM_MEMORY_COST, OWASP_MINIMUM_TIME_COST
from portfolio.logging import REQUEST_ID_KEY, ValueRedactor, configure_logging
from portfolio.main import create_app
from tests.address_vectors import BIP173_TESTNET_P2WPKH
from tests.api.test_accounting import settled
from tests.auth.conftest import BASE_URL, JSON_HEADERS, sign_in
from tests.logging_harness import preserved_logging

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator
    from pathlib import Path

    from fastapi import FastAPI
    from starlette.types import Message, Receive, Scope, Send

PROBE: Final = "/api/test/probe/{marker}"
BOOM: Final = "/api/test/boom"
CONCURRENT_REQUESTS: Final = 40
FORGED: Final = "forged-request-id-4f1c"
QUERY_SENTINEL: Final = "query-sentinel-8a2d"
PRODUCTION_ORIGIN: Final = "https://portfolio.example"
HYPHENATED_UUID: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


class Unplanned(RuntimeError):  # noqa: N818 - the name is what the 500's log line carries
    """An exception nobody planned for."""


def is_request_id(value: str) -> bool:
    """A hyphenated UUID4 in its canonical spelling (R5)."""
    return HYPHENATED_UUID.fullmatch(value) is not None and str(UUID(value)) == value


# --------------------------------------------------------------------------------------
# The application under test, with two routes of the test's own
# --------------------------------------------------------------------------------------


async def probe(marker: str) -> dict[str, str | None]:
    """Three records across two event-loop yields, through both kinds of logger."""
    structlog.get_logger("probe").info("probe_first", marker=marker)
    await asyncio.sleep(0)
    logging.getLogger("probe.library").warning("probe_second %s", marker)
    await asyncio.sleep(0.001)
    structlog.get_logger("probe").info("probe_third", marker=marker)
    return {"marker": marker, "request_id": current_request_id()}


async def boom() -> None:
    message = "a fault nobody planned for"
    raise Unplanned(message)


def production(log_level: str = "DEBUG") -> Settings:
    return Settings(
        _env_file=None,
        environment="prod",
        allowed_origin=PRODUCTION_ORIGIN,
        log_level=log_level,
        argon2_memory_cost=OWASP_MINIMUM_MEMORY_COST,
        argon2_time_cost=OWASP_MINIMUM_TIME_COST,
    )


@pytest.fixture
def restored() -> Iterator[None]:
    with preserved_logging():
        structlog.contextvars.clear_contextvars()
        yield
        structlog.contextvars.clear_contextvars()


@asynccontextmanager
async def served(
    capsys: pytest.CaptureFixture[str] | None = None, log_level: str = "DEBUG"
) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    """The real application with the test routes, its pipeline in production form, signed in.

    What the development pipeline wrote while the application started is read and dropped,
    so every line a test reads afterwards is the production pipeline's. `raise_app_exceptions`
    is off, so the 500 comes back as a server sends it.
    """
    app = create_app()
    app.add_api_route(PROBE, probe, methods=["GET"])
    app.add_api_route(BOOM, boom, methods=["GET"])
    async with app.router.lifespan_context(app):
        await settled(app)
        configure_logging(production(log_level))
        if capsys is not None:
            capsys.readouterr()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
            await sign_in(client)
            yield app, client


def lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """Every JSON line on stdout since the last read."""
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def completed(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in entries if entry["event"] == REQUEST_COMPLETED_EVENT]


def completed_for(entries: list[dict[str, Any]], request_id: str) -> dict[str, Any]:
    [line] = [entry for entry in completed(entries) if entry.get(REQUEST_ID_KEY) == request_id]
    return line


def test_the_names_are_the_specs() -> None:
    assert REQUEST_ID_HEADER == "X-Request-ID"
    assert REQUEST_COMPLETED_EVENT == "request_completed"
    assert SPA_ROUTE == "spa"
    assert UNMATCHED_ROUTE == "unmatched"
    assert HEALTH_CHECK_PATH == "/api/health"
    assert REQUEST_ID_KEY == "request_id"
    assert LOGGED_PATH_LIMIT == 256  # docs/operations.md, section 18


# --------------------------------------------------------------------------------------
# The header, on every status
# --------------------------------------------------------------------------------------


async def test_every_status_carries_its_own_request_id(
    api_environment: Path, restored: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """200, 401, 404, 422 and 500: each a fresh hyphenated UUID4, the one its line carries."""
    del api_environment, restored
    async with served(capsys) as (_app, client):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
            responses = {
                401: await anonymous.get("/api/wallets"),
                200: await client.get("/api/health"),
                404: await client.get("/api/wallets/987654/balances"),
                422: await client.get("/api/wallets/not-a-number/balances"),
                500: await client.get(BOOM),
            }
    entries = lines(capsys)

    ids = []
    for status, response in responses.items():
        assert response.status_code == status, response.text
        request_id = response.headers[REQUEST_ID_HEADER]
        assert is_request_id(request_id), request_id
        assert completed_for(entries, request_id)["status"] == status
        ids.append(request_id)
    assert len(set(ids)) == len(ids)


async def test_the_500s_header_is_the_id_its_exception_was_logged_under(
    api_environment: Path, restored: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Set by the handler outside this middleware, from the context it left bound."""
    del api_environment, restored
    async with served(capsys) as (_app, client):
        response = await client.get(BOOM)
    entries = lines(capsys)

    assert response.status_code == 500
    request_id = response.headers[REQUEST_ID_HEADER]
    [unhandled] = [entry for entry in entries if entry["event"] == "unhandled_exception"]
    assert unhandled[REQUEST_ID_KEY] == request_id
    assert unhandled["error_type"] == "Unplanned"
    assert completed_for(entries, request_id) | {"duration_ms": 0, "timestamp": ""} == {
        "event": REQUEST_COMPLETED_EVENT,
        "method": "GET",
        "route": BOOM,
        "status": 500,
        "duration_ms": 0,
        "level": "info",
        REQUEST_ID_KEY: request_id,
        "timestamp": "",
    }


@pytest.mark.parametrize("header", ["X-Request-ID", "x-request-id"])
async def test_an_inbound_request_id_is_never_echoed_nor_logged(
    header: str, api_environment: Path, restored: None, capsys: pytest.CaptureFixture[str]
) -> None:
    del api_environment, restored
    async with served(capsys) as (_app, client):
        ok = await client.get("/api/health/detail", headers={header: FORGED})
        refused = await client.get(BOOM, headers={header: FORGED})
    written = capsys.readouterr().out

    for response in (ok, refused):
        assert response.headers[REQUEST_ID_HEADER] != FORGED
        assert is_request_id(response.headers[REQUEST_ID_HEADER])
        assert response.headers.get_list(REQUEST_ID_HEADER) == [response.headers[REQUEST_ID_HEADER]]
    assert FORGED not in written
    assert REQUEST_COMPLETED_EVENT in written


# --------------------------------------------------------------------------------------
# One id on every record of a request, and never another request's
# --------------------------------------------------------------------------------------


def records_of(entries: list[dict[str, Any]], marker: str) -> list[dict[str, Any]]:
    return [
        entry
        for entry in entries
        if entry.get("marker") == marker or entry["event"] == f"probe_second {marker}"
    ]


async def test_every_record_of_a_request_carries_its_id(
    api_environment: Path, restored: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Structlog's and the standard library's, before and after the route yields."""
    del api_environment, restored
    async with served(capsys) as (_app, client):
        response = await client.get(PROBE.format(marker="solo"))
    entries = lines(capsys)

    request_id = response.headers[REQUEST_ID_HEADER]
    assert response.json() == {"marker": "solo", "request_id": request_id}
    probes = records_of(entries, "solo")
    assert [entry["event"] for entry in probes] == [
        "probe_first",
        "probe_second solo",
        "probe_third",
    ]
    assert probes[1]["logger"] == "probe.library"
    assert {entry[REQUEST_ID_KEY] for entry in probes} == {request_id}
    assert completed_for(entries, request_id)["route"] == PROBE


async def test_concurrent_requests_never_share_or_swap_an_id(
    api_environment: Path, restored: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Forty requests in flight at once, each yielding between its records."""
    del api_environment, restored
    headers: dict[str, str] = {}
    async with served(capsys) as (_app, client):

        async def one(marker: str) -> None:
            response = await client.get(PROBE.format(marker=marker))
            assert response.status_code == 200
            assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]
            headers[marker] = response.headers[REQUEST_ID_HEADER]

        async with anyio.create_task_group() as group:
            for index in range(CONCURRENT_REQUESTS):
                group.start_soon(one, f"m{index:03d}")
    entries = lines(capsys)

    assert len(headers) == CONCURRENT_REQUESTS
    assert len(set(headers.values())) == CONCURRENT_REQUESTS
    for marker, request_id in headers.items():
        probes = records_of(entries, marker)
        assert len(probes) == 3, marker
        assert {entry[REQUEST_ID_KEY] for entry in probes} == {request_id}, marker
        assert completed_for(entries, request_id)["status"] == 200
    interleaved = [entry["event"] for entry in entries if entry["event"].startswith("probe_")]
    assert interleaved != sorted(interleaved, key=lambda event: event.split()[0]), (
        "the requests ran one after another, so this proves nothing about concurrency"
    )


async def test_a_request_starts_from_a_clean_context(
    api_environment: Path, restored: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whatever was bound before is cleared, so no value leaks from one request to the next."""
    del api_environment, restored
    async with served(capsys) as (_app, client):
        structlog.contextvars.bind_contextvars(stale_value="left-over-from-before")
        response = await client.get(PROBE.format(marker="clean"))
    entries = lines(capsys)

    probes = records_of(entries, "clean")
    assert probes
    assert all("stale_value" not in entry for entry in probes)
    assert response.headers[REQUEST_ID_HEADER] == probes[0][REQUEST_ID_KEY]


async def test_the_context_is_left_bound_when_the_request_ends(
    api_environment: Path, restored: None
) -> None:
    """The 500 handler runs after the middleware returns and still needs the id.

    The in-process transport serves the request in the caller's task, so the caller sees what
    the middleware left behind.
    """
    del api_environment, restored
    assert current_request_id() is None
    async with served() as (_app, client):
        response = await client.get("/api/health/detail")

        assert current_request_id() == response.headers[REQUEST_ID_HEADER]


def test_outside_a_request_there_is_no_id() -> None:
    structlog.contextvars.clear_contextvars()
    assert current_request_id() is None
    structlog.contextvars.bind_contextvars(**{REQUEST_ID_KEY: 42})
    try:
        assert current_request_id() is None, "only a string is an id"
    finally:
        structlog.contextvars.clear_contextvars()


# --------------------------------------------------------------------------------------
# request_completed: its fields, its level, and never the raw path
# --------------------------------------------------------------------------------------


async def test_request_completed_has_the_template_and_never_the_path_or_query(
    api_environment: Path, restored: None, capsys: pytest.CaptureFixture[str]
) -> None:
    del api_environment, restored
    async with served(capsys) as (_app, client):
        response = await client.get(f"/api/wallets/987654/balances?cursor={QUERY_SENTINEL}&limit=5")
    entries = lines(capsys)

    line = completed_for(entries, response.headers[REQUEST_ID_HEADER])
    assert set(line) == {
        "event",
        "method",
        "route",
        "status",
        "duration_ms",
        "level",
        REQUEST_ID_KEY,
        "timestamp",
    }
    assert line["route"] == "/api/wallets/{wallet_id}/balances"
    assert line["method"] == "GET"
    assert line["status"] == response.status_code
    assert isinstance(line["duration_ms"], int)
    assert line["duration_ms"] >= 0
    rendered = json.dumps(line)
    assert "987654" not in rendered
    assert QUERY_SENTINEL not in rendered
    assert "cursor" not in rendered


@pytest.mark.parametrize(
    ("method", "path", "level", "route"),
    [
        ("GET", "/api/health", "debug", "/api/health"),
        ("GET", "/api/health/detail", "info", "/api/health/detail"),
        ("POST", "/api/health", "info", None),
        ("GET", "/", "debug", SPA_ROUTE),
        ("GET", "/assets/index-3f2a.js", "debug", SPA_ROUTE),
        ("GET", "/wallets", "debug", SPA_ROUTE),
        ("GET", "/api/openapi.json", "info", "/api/openapi.json"),
        ("GET", "/api/docs", "info", "/api/docs"),
        ("GET", "/api/docs/oauth2-redirect", "info", "/api/docs/oauth2-redirect"),
        ("GET", "/api/wallets", "info", "/api/wallets"),
    ],
)
async def test_each_request_is_logged_at_its_level_under_its_route(
    method: str,
    path: str,
    level: str,
    route: str | None,
    api_environment: Path,
    restored: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DEBUG for the container's health check and the SPA; INFO for everything else."""
    del api_environment, restored
    async with served(capsys) as (_app, client):
        response = await client.request(method, path)
    line = completed_for(lines(capsys), response.headers[REQUEST_ID_HEADER])

    assert line["level"] == level
    assert line["method"] == method
    if route is not None:
        assert line["route"] == route


async def test_the_health_check_is_not_written_at_info(
    api_environment: Path, restored: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every thirty seconds at INFO would bury everything else."""
    del api_environment, restored
    async with served(capsys, log_level="INFO") as (_app, client):
        quiet = await client.get("/api/health")
        loud = await client.get("/api/health/detail")
    written = [entry[REQUEST_ID_KEY] for entry in completed(lines(capsys))]

    assert quiet.status_code == 200
    assert quiet.headers[REQUEST_ID_HEADER] not in written
    assert loud.headers[REQUEST_ID_HEADER] in written


@pytest.mark.parametrize(
    ("path", "route"),
    [
        ("/api/wallets", "unmatched"),
        ("/api/nope", "unmatched"),
        ("/api/openapi.json", "/api/openapi.json"),
        ("/api/docs", "/api/docs"),
        ("/api/docs/oauth2-redirect", "/api/docs/oauth2-redirect"),
        ("/", SPA_ROUTE),
    ],
)
async def test_a_request_refused_before_routing_keeps_a_label_without_its_path(
    path: str,
    route: str,
    api_environment: Path,
    restored: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R9: no session, so the guard answers before routing; a documentation path keeps its own."""
    del api_environment, restored
    async with served(capsys) as (app, _client):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
            response = await anonymous.get(path)
    line = completed_for(lines(capsys), response.headers[REQUEST_ID_HEADER])

    assert line["route"] == route
    if path.startswith("/api"):
        assert response.status_code == 401
        assert line["status"] == 401
        assert line["level"] == "info"


async def test_an_unmatched_api_path_never_logs_its_path(
    api_environment: Path, restored: None, capsys: pytest.CaptureFixture[str]
) -> None:
    del api_environment, restored
    async with served(capsys) as (app, _client):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
            response = await anonymous.get(f"/api/{QUERY_SENTINEL}?x={QUERY_SENTINEL}")
    line = completed_for(lines(capsys), response.headers[REQUEST_ID_HEADER])

    assert line["route"] == UNMATCHED_ROUTE
    assert QUERY_SENTINEL not in json.dumps(line)


#: 200 KB of what made the old URL rule rescan a run: 12 s for `a.`, 27 s for `a://` (R13).
ADVERSARIAL_PATH_SIZE: Final = 200_000
ANSWERED_WITHIN_SECONDS: Final = 1.0
#: The most one `request_refused` line may take, whatever the path: at most
#: `LOGGED_PATH_LIMIT` characters of it, the fields beside it and the JSON around them, with
#: room to spare. With the path written whole it was over 200 KB.
REFUSED_LINE_MAX_BYTES: Final = 1024


async def anonymous_call(app: FastAPI, method: str, path: str) -> tuple[int, dict[str, str]]:
    """`method path` with no cookie, no `Origin` and no body, as a raw ASGI call: the status
    and the headers.

    Not through `httpx`, which refuses a URL over 64 KB before sending it. A server does not:
    uvicorn's `httptools` parser hands the application a path of any length.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 443),
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    [start] = [message for message in sent if message["type"] == "http.response.start"]
    headers = {name.decode().lower(): value.decode() for name, value in start["headers"]}
    return start["status"], headers


@pytest.mark.parametrize(
    ("method", "status", "reason"),
    [("GET", 401, "no_session"), ("POST", 403, "origin")],
    ids=["no session", "no origin"],
)
@pytest.mark.parametrize("unit", ["a.", "a://", "tb1", "kaspatest:"])
async def test_an_anonymous_200_kb_path_is_answered_quickly_and_logged_in_a_bounded_line(
    unit: str,
    method: str,
    status: int,
    reason: str,
    api_environment: Path,
    restored: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R13 (M1), end to end, and the line it writes: a client with no session chooses the
    path `request_refused` logs. Written whole, each such request made the application write
    a 200 KB line; now it writes at most `LOGGED_PATH_LIMIT` characters of it, and its length.

    Both refusals: the session check's, and the write guard's, which comes first and covers
    every path, the API's or not. One second is generous; a redaction rule that rescanned took
    tens. The rules' own time on 200 KB is pinned in `tests/test_logging_values.py`, since
    the path they see here is no longer that long."""
    del api_environment, restored
    path = "/api/" + unit * (ADVERSARIAL_PATH_SIZE // len(unit))
    async with served(capsys) as (app, _client):
        started = time.perf_counter()
        answered, headers = await anonymous_call(app, method, path)
        elapsed = time.perf_counter() - started
    written = capsys.readouterr().out.splitlines()
    request_id = headers[REQUEST_ID_HEADER.lower()]
    mine = [
        (raw, entry)
        for raw in written
        if raw.strip() and (entry := json.loads(raw)).get(REQUEST_ID_KEY) == request_id
    ]

    assert answered == status
    assert elapsed < ANSWERED_WITHIN_SECONDS
    assert [entry["event"] for _raw, entry in mine] == [
        "request_refused",
        REQUEST_COMPLETED_EVENT,
    ]
    [(raw, refused), _completed] = mine
    assert len(raw.encode()) <= REFUSED_LINE_MAX_BYTES, len(raw.encode())
    assert refused["reason"] == reason
    assert refused["path_truncated"] is True
    assert refused["path_length"] == len(path)
    assert path.startswith(refused["path"])
    assert refused["path"].endswith("/")
    assert len(refused["path"]) <= LOGGED_PATH_LIMIT


# --------------------------------------------------------------------------------------
# What `request_refused` writes of a path
# --------------------------------------------------------------------------------------


def test_a_path_that_fits_is_written_whole_and_alone() -> None:
    at_the_limit = "/api/" + "a" * (LOGGED_PATH_LIMIT - len("/api/"))

    assert len(at_the_limit) == LOGGED_PATH_LIMIT
    assert logged_path(at_the_limit) == {"path": at_the_limit}
    assert logged_path("/api/wallets") == {"path": "/api/wallets"}


def test_a_longer_path_is_cut_back_to_the_last_slash_within_the_limit() -> None:
    one_over = "/api/" + "a" * (LOGGED_PATH_LIMIT - len("/api/") + 1)
    segments = "/api/wallets/" + "b" * LOGGED_PATH_LIMIT + "/balances"

    assert logged_path(one_over) == {
        "path": "/api/",
        "path_truncated": True,
        "path_length": LOGGED_PATH_LIMIT + 1,
    }
    assert logged_path(segments) == {
        "path": "/api/wallets/",
        "path_truncated": True,
        "path_length": len(segments),
    }


def test_an_address_the_limit_falls_inside_is_left_out_whole() -> None:
    """Why the cut goes back to a `/`. Ten characters of an address are fewer than the value
    redaction recognises, so a cut at the limit would write them as they are."""
    head = "/api/" + "a" * (LOGGED_PATH_LIMIT - len("/api/") - 11) + "/"
    path = head + BIP173_TESTNET_P2WPKH + "/balances"
    assert len(head) == LOGGED_PATH_LIMIT - 10

    # The control: cut at the limit and redacted, it ends in the address's first ten.
    cut_at_the_limit = ValueRedactor([]).redact_text(path[:LOGGED_PATH_LIMIT])
    assert cut_at_the_limit.endswith(BIP173_TESTNET_P2WPKH[:10])
    assert logged_path(path)["path"] == head


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet="/a.:é", max_size=3 * LOGGED_PATH_LIMIT).map(lambda rest: "/" + rest))
def test_what_is_written_is_the_longest_run_of_whole_segments_within_the_limit(
    path: str,
) -> None:
    fields = logged_path(path)
    written = fields["path"]
    assert isinstance(written, str)

    assert len(written) <= LOGGED_PATH_LIMIT
    assert path.startswith(written)
    if len(path) <= LOGGED_PATH_LIMIT:
        assert fields == {"path": path}
    else:
        assert fields == {"path": written, "path_truncated": True, "path_length": len(path)}
        assert written.endswith("/")
        assert "/" not in path[len(written) : LOGGED_PATH_LIMIT]


# --------------------------------------------------------------------------------------
# R6: the prefixed template, for every operation the API documents
# --------------------------------------------------------------------------------------

#: A path parameter's value that no integer converter accepts: the route matches, and the
#: request is refused by validation before its endpoint runs.
NOT_AN_INTEGER: Final = "not-an-integer"


def operations(app: FastAPI) -> list[tuple[str, str, dict[str, Any]]]:
    """Every operation in the OpenAPI document but the test's own; `logout` last."""
    found = [
        (method.upper(), path, operation)
        for path, methods in app.openapi()["paths"].items()
        for method, operation in methods.items()
        if not path.startswith("/api/test/")
    ]
    return sorted(found, key=lambda item: item[2]["operationId"] == "logout")


async def test_every_operation_is_logged_under_its_prefixed_template(
    api_environment: Path, restored: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Read from `scope["fastapi"]["effective_route_context"]`, measured on FastAPI 0.141.1.

    An upgrade that drops that entry falls back to the template without `/api`, and this
    fails on all twenty-five. A body is sent malformed and a path parameter unparsable, so the
    route is matched and no endpoint with a side effect runs; the bodiless ones that remain
    are reads, the two manual syncs with nothing configured, and `logout`, sent last.
    """
    del api_environment, restored
    seen: dict[str, tuple[str, int]] = {}
    async with served(capsys) as (app, client):
        documented = operations(app)
        for method, template, operation in documented:
            path = re.sub(r"\{[^}]+\}", NOT_AN_INTEGER, template)
            refusable = "requestBody" in operation or path != template
            response = await client.request(
                method,
                path,
                content=b"{" if "requestBody" in operation else None,
                headers=JSON_HEADERS,
            )
            seen[f"{method} {template}"] = (
                response.headers[REQUEST_ID_HEADER],
                422 if refusable else response.status_code,
            )
            assert response.status_code == seen[f"{method} {template}"][1], response.text
    entries = lines(capsys)

    assert len(documented) == 25
    assert sum(1 for _id, status in seen.values() if status == 422) == 9
    for name, (request_id, _status) in seen.items():
        template = name.split(" ", 1)[1]
        assert template.startswith("/api/")
        assert completed_for(entries, request_id)["route"] == template, name


def test_the_documentation_paths_are_the_apps_configured_urls(app: FastAPI) -> None:
    assert documentation_paths(app) == frozenset(
        {"/api/openapi.json", "/api/docs", "/api/docs/oauth2-redirect"}
    )


def test_a_documentation_url_switched_off_is_not_a_documentation_path() -> None:
    from fastapi import FastAPI as Bare

    bare = Bare(
        openapi_url="/o.json", docs_url="/d", redoc_url=None, swagger_ui_oauth2_redirect_url=None
    )

    assert documentation_paths(bare) == frozenset({"/o.json", "/d"})
    assert (
        documentation_paths(
            Bare(
                openapi_url=None, docs_url=None, redoc_url=None, swagger_ui_oauth2_redirect_url=None
            )
        )
        == frozenset()
    )


def test_the_middleware_is_the_outermost_and_measures_with_perf_counter(app: FastAPI) -> None:
    """R11: `perf_counter_ns`, not `monotonic_ns`, which steps by 15.6 ms on Windows."""
    outermost = app.user_middleware[0]
    middleware_class: object = outermost.cls

    assert middleware_class is RequestContextMiddleware
    assert outermost.kwargs["is_api_path"] is is_api_path
    assert outermost.kwargs["documentation_paths"] == documentation_paths(app)
    assert "clock_ns" not in outermost.kwargs
    default = inspect.signature(RequestContextMiddleware).parameters["clock_ns"].default
    assert default is time.perf_counter_ns


# --------------------------------------------------------------------------------------
# The middleware on its own: the clock, and anything that is not HTTP
# --------------------------------------------------------------------------------------


def answering(status: int) -> Callable[[Scope, Receive, Send], Any]:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app


def clock(*readings: int) -> Callable[[], int]:
    remaining = list(readings)

    def read() -> int:
        return remaining.pop(0)

    return read


@pytest.mark.parametrize(
    ("elapsed_ns", "expected_ms"),
    [(0, 0), (999_999, 0), (1_000_000, 1), (1_234_999_999, 1234), (15_625_000, 15)],
)
async def test_duration_ms_is_whole_milliseconds_of_the_injected_clock(
    elapsed_ns: int, expected_ms: int
) -> None:
    started = 7_000_000_000_123
    middleware = RequestContextMiddleware(
        answering(204), is_api_path=is_api_path, clock_ns=clock(started, started + elapsed_ns)
    )
    async with AsyncClient(transport=ASGITransport(app=middleware), base_url=BASE_URL) as client:
        with capture_logs() as captured:
            response = await client.get("/api/anything?secret=1")

    assert response.status_code == 204
    [line] = [entry for entry in captured if entry["event"] == REQUEST_COMPLETED_EVENT]
    assert line == {
        "event": REQUEST_COMPLETED_EVENT,
        "method": "GET",
        "route": UNMATCHED_ROUTE,
        "status": 204,
        "duration_ms": expected_ms,
        "log_level": "info",
    }


async def test_a_request_that_ends_without_a_response_is_logged_as_a_500() -> None:
    async def raising(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive, send
        message = "no response at all"
        raise Unplanned(message)

    middleware = RequestContextMiddleware(raising, is_api_path=is_api_path, clock_ns=clock(0, 0))
    transport = ASGITransport(app=middleware, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        with capture_logs() as captured:
            await client.get("/api/anything")

    [line] = [entry for entry in captured if entry["event"] == REQUEST_COMPLETED_EVENT]
    assert line["status"] == 500


async def test_anything_but_http_passes_through_untouched() -> None:
    """The lifespan scope: no id, no header, no line, and the very `send` it was given."""
    seen: list[tuple[Scope, Send]] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        seen.append((scope, send))

    middleware = RequestContextMiddleware(inner, is_api_path=is_api_path, clock_ns=clock())
    scope: Scope = {"type": "lifespan"}

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        del message

    structlog.contextvars.clear_contextvars()
    with capture_logs() as captured:
        await middleware(scope, receive, send)

    assert seen == [(scope, send)]
    assert captured == []
    assert current_request_id() is None
