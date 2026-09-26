"""Criteria 3 and 6: `GET /api/exchanges`, `POST /api/exchanges/sync`, `GET /api/exchanges/runs`.

The whole stack runs -- middleware, router, the read service, the coordinator the lifespan
built, the write service, SQLite -- and one thing is replaced: the venue. The lifespan builds
its provider mapping once, from `exchange_providers(client, settings=...)` looked up in
`portfolio.main`, and that is the name patched here, so `configured_exchanges` on
`app.state` is derived from the very mapping the sync is handed. A venue in the mapping is
"configured"; that is the whole of what the read side learns about credentials.

The exchange schedule is off in this environment (`tests/auth/conftest.py`), so every run
here is one a test started, and the manual endpoint working with the timer off is itself
asserted.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from starlette.requests import Request

from portfolio.api.dependencies import configured_exchanges_of, get_exchange_sync_coordinator
from portfolio.config import get_settings
from portfolio.domain.exchanges import ExchangeKey
from portfolio.main import create_app
from portfolio.providers.exchanges.errors import ExchangeAuthError, ExchangeUnavailableError
from portfolio.repositories.exchange_sync_runs import SyncTrigger
from tests.auth.conftest import BASE_URL, JSON_HEADERS, sign_in
from tests.exchange_sync_harness import (
    SimulatedVenue,
    always,
    make_fill,
    sqlite_timestamp,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from pathlib import Path

    from tests.exchange_sync_harness import PageCall

EXCHANGES: Final = "/api/exchanges"
SYNC: Final = "/api/exchanges/sync"
RUNS: Final = "/api/exchanges/runs"

ACCOUNT_FIELDS: Final = {
    "exchange_key",
    "configured",
    "status",
    "syncing",
    "requested_since",
    "effective_since",
    "history_truncated",
    "last_synced_at",
    "fills_stored",
    "pending_windows",
    "last_error",
}
RUN_FIELDS: Final = {
    "run_id",
    "trigger",
    "status",
    "started_at",
    "finished_at",
    "duration_ms",
    "accounts_total",
    "accounts_succeeded",
    "accounts_failed",
    "accounts_skipped",
    "fills_seen",
    "fills_inserted",
    "accounts",
}
OUTCOME_FIELDS: Final = {
    "exchange_key",
    "status",
    "windows_completed",
    "pages",
    "fills_seen",
    "fills_inserted",
    "error_kind",
    "detail",
}
DEADLOCK_TIMEOUT: Final = 5


def recent_fills(count: int = 9) -> list[Any]:
    """Fills a few minutes old against the real clock the application runs on."""
    now = datetime.now(UTC).replace(microsecond=0)
    return [
        make_fill(1001 + index, now - timedelta(minutes=count - index)) for index in range(count)
    ]


async def until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until `condition` holds. Every caller bounds it with `wait_for`."""
    while not condition():  # noqa: ASYNC110
        await asyncio.sleep(0)


def instant(value: str) -> datetime:
    """An ISO 8601 instant off the wire, required to carry an offset."""
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None, f"{value!r} carries no offset"
    return parsed


@asynccontextmanager
async def application(
    monkeypatch: pytest.MonkeyPatch,
    providers: Mapping[ExchangeKey, SimulatedVenue],
) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    """The real application, its lifespan handed `providers` as the configured venues."""
    frozen = MappingProxyType(dict(providers))
    monkeypatch.setattr("portfolio.main.exchange_providers", lambda client, **_: frozen)
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
            await sign_in(client)
            yield app, client


async def sync(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(SYNC, headers=JSON_HEADERS)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def listed(client: AsyncClient) -> dict[str, dict[str, Any]]:
    response = await client.get(EXCHANGES)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"exchanges"}
    return {entry["exchange_key"]: entry for entry in payload["exchanges"]}


async def plant_account(app: FastAPI, exchange_key: str) -> None:
    """An account row for the owner, as a venue configured in some earlier deployment left it."""
    async with app.state.db_sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO exchange_accounts (user_id, exchange_key, created_at) "
                "VALUES ((SELECT id FROM users WHERE username = 'owner'), :key, :at)"
            ),
            {"key": exchange_key, "at": sqlite_timestamp(datetime(2026, 1, 1, tzinfo=UTC))},
        )
        await session.commit()


# --------------------------------------------------------------------------------------
# GET /api/exchanges
# --------------------------------------------------------------------------------------


async def test_nothing_configured_and_no_account_is_an_empty_list(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#16's empty state."""
    del api_environment
    async with application(monkeypatch, {}) as (_app, client):
        response = await client.get(EXCHANGES)

    assert response.status_code == 200
    assert response.json() == {"exchanges": []}


async def test_a_configured_venue_that_never_synced_is_listed(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch, {ExchangeKey.BITGET: SimulatedVenue()}) as (_app, client):
        entries = await listed(client)

    assert list(entries) == ["bitget"]
    bitget = entries["bitget"]
    assert set(bitget) == ACCOUNT_FIELDS
    assert bitget == {
        "exchange_key": "bitget",
        "configured": True,
        "status": "never_synced",
        "syncing": False,
        "requested_since": None,
        "effective_since": None,
        "history_truncated": False,
        "last_synced_at": None,
        "fills_stored": 0,
        "pending_windows": 0,
        "last_error": None,
    }


async def test_the_account_list_reports_a_truncated_history(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 3 on the wire: 2009 asked for, ninety days less a margin held, and a flag."""
    del api_environment
    venue = SimulatedVenue(recent_fills())
    async with application(monkeypatch, {ExchangeKey.BITGET: venue}) as (_app, client):
        before = datetime.now(UTC)
        await sync(client)
        bitget = (await listed(client))["bitget"]

    assert bitget["status"] == "ok"
    assert bitget["configured"] is True
    assert instant(bitget["requested_since"]) == datetime(2009, 1, 3, tzinfo=UTC)
    effective = instant(bitget["effective_since"])
    ninety_less_margin = timedelta(days=90) - timedelta(minutes=5)
    assert before - ninety_less_margin - timedelta(seconds=5) <= effective
    assert effective <= datetime.now(UTC) - ninety_less_margin
    assert bitget["history_truncated"] is True
    assert bitget["fills_stored"] == 9
    assert bitget["pending_windows"] == 0
    assert bitget["last_error"] is None
    assert instant(bitget["last_synced_at"]) >= before - timedelta(seconds=1)


async def test_a_history_inside_retention_is_not_truncated(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start asked for is the start held: `effective_since == requested_since`."""
    del api_environment
    start = (datetime.now(UTC) - timedelta(days=3)).date()
    monkeypatch.setenv("PORTFOLIO_EXCHANGE_HISTORY_START", start.isoformat())
    get_settings.cache_clear()
    async with application(monkeypatch, {ExchangeKey.BITGET: SimulatedVenue()}) as (
        _app,
        client,
    ):
        await sync(client)
        bitget = (await listed(client))["bitget"]

    assert bitget["requested_since"] == bitget["effective_since"]
    assert bitget["history_truncated"] is False


async def test_an_account_without_credentials_is_listed_as_not_configured(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its fills belong to it, so it is shown; nothing is claimed about a key it lacks."""
    del api_environment
    async with application(monkeypatch, {ExchangeKey.BITGET: SimulatedVenue()}) as (app, client):
        await plant_account(app, "bingx")
        entries = await listed(client)

    assert list(entries) == ["bingx", "bitget"], "sorted by exchange_key"
    assert entries["bingx"]["configured"] is False
    assert entries["bingx"]["status"] == "never_synced"
    assert entries["bingx"]["syncing"] is False
    assert entries["bitget"]["configured"] is True


async def test_syncing_is_true_only_for_configured_venues_while_a_run_is_in_flight(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    release = asyncio.Event()
    started = asyncio.Event()
    venue = SimulatedVenue(recent_fills())

    async def hold(call: PageCall) -> None:
        del call
        started.set()
        await release.wait()

    venue.on_call = hold
    async with application(monkeypatch, {ExchangeKey.BITGET: venue}) as (app, client):
        await plant_account(app, "bingx")
        idle = await listed(client)
        running = asyncio.create_task(client.post(SYNC, headers=JSON_HEADERS))
        await asyncio.wait_for(started.wait(), DEADLOCK_TIMEOUT)
        during = await listed(client)
        release.set()
        response = await asyncio.wait_for(running, DEADLOCK_TIMEOUT)
        after = await listed(client)

    assert response.status_code == 200
    assert (idle["bitget"]["syncing"], idle["bingx"]["syncing"]) == (False, False)
    assert (during["bitget"]["syncing"], during["bingx"]["syncing"]) == (True, False)
    assert after["bitget"]["syncing"] is False


async def test_last_error_is_the_latest_attempt_and_ignores_skipped_runs(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused key, then a scheduled run that skipped it, then a manual run that worked.

    The skipped run says nothing about the key, so `last_error` still names the refusal
    after it. A manual run that succeeds clears it.
    """
    del api_environment
    refusal = ExchangeAuthError(status=401, venue_code="40006")
    venue = SimulatedVenue(recent_fills(), fault=always(refusal))
    async with application(monkeypatch, {ExchangeKey.BITGET: venue}) as (app, client):
        failed = await sync(client)
        after_failure = (await listed(client))["bitget"]
        await app.state.exchange_sync_coordinator.sync(SyncTrigger.SCHEDULED)
        after_skip = (await listed(client))["bitget"]
        venue.fault = None
        await sync(client)
        after_success = (await listed(client))["bitget"]

    assert failed["status"] == "failed"
    expected = {"error_kind": "auth", "detail": str(refusal)}
    assert after_failure["status"] == "auth_failed"
    assert after_failure["last_error"] == expected
    assert after_skip["status"] == "auth_failed"
    assert after_skip["last_error"] == expected, "a skipped outcome must not hide the refusal"
    assert after_success["status"] == "ok"
    assert after_success["last_error"] is None


# --------------------------------------------------------------------------------------
# POST /api/exchanges/sync
# --------------------------------------------------------------------------------------


async def test_a_manual_sync_returns_the_run_summary(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    assert get_settings().exchange_sync_enabled is False, "the timer is off; the button is not"
    async with application(monkeypatch, {ExchangeKey.BITGET: SimulatedVenue(recent_fills())}) as (
        _app,
        client,
    ):
        summary = await sync(client)

    assert set(summary) == RUN_FIELDS | {"joined"}
    assert summary["joined"] is False
    assert summary["trigger"] == "manual"
    assert summary["status"] == "success"
    assert (summary["accounts_total"], summary["accounts_succeeded"]) == (1, 1)
    assert (summary["accounts_failed"], summary["accounts_skipped"]) == (0, 0)
    assert (summary["fills_seen"], summary["fills_inserted"]) == (9, 9)
    assert isinstance(summary["duration_ms"], int)
    instant(summary["started_at"])
    instant(summary["finished_at"])
    (account,) = summary["accounts"]
    assert set(account) == OUTCOME_FIELDS
    assert account["exchange_key"] == "bitget"
    assert account["status"] == "success"
    assert (account["fills_seen"], account["fills_inserted"]) == (9, 9)
    assert (account["error_kind"], account["detail"]) == (None, None)


async def test_a_second_click_joins_the_run_in_flight(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run is held open at the venue until the second request has reached the coordinator.

    Waiting a single loop turn was a race: the second request's middleware and session check
    take many turns, so the first run could finish before it arrived and the second would
    start a run of its own. A spy on the coordinator's `sync` says when the second call has
    been made; the join itself then happens in that same step, under an uncontended lock.
    """
    del api_environment
    release = asyncio.Event()
    started = asyncio.Event()
    venue = SimulatedVenue(recent_fills())

    async def hold(call: PageCall) -> None:
        del call
        started.set()
        await release.wait()

    venue.on_call = hold
    async with application(monkeypatch, {ExchangeKey.BITGET: venue}) as (app, client):
        coordinator = app.state.exchange_sync_coordinator
        arrived: list[SyncTrigger] = []
        delegate = coordinator.sync

        async def spy(trigger: SyncTrigger) -> Any:
            arrived.append(trigger)
            return await delegate(trigger)

        monkeypatch.setattr(coordinator, "sync", spy)
        first = asyncio.create_task(client.post(SYNC, headers=JSON_HEADERS))
        await asyncio.wait_for(started.wait(), DEADLOCK_TIMEOUT)
        second = asyncio.create_task(client.post(SYNC, headers=JSON_HEADERS))
        await asyncio.wait_for(until(lambda: len(arrived) == 2), DEADLOCK_TIMEOUT)
        for _ in range(5):
            await asyncio.sleep(0)
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), DEADLOCK_TIMEOUT)

    bodies = [response.json() for response in responses]
    assert [response.status_code for response in responses] == [200, 200]
    assert sorted(body["joined"] for body in bodies) == [False, True]
    assert bodies[0]["run_id"] == bodies[1]["run_id"]


async def test_a_failed_run_is_still_a_200(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's own status is in the body, for the reason `POST /api/balances/sync` gives."""
    del api_environment
    venue = SimulatedVenue(fault=always(ExchangeUnavailableError(status=503)))
    async with application(monkeypatch, {ExchangeKey.BITGET: venue}) as (_app, client):
        summary = await sync(client)

    assert summary["status"] == "failed"
    (account,) = summary["accounts"]
    assert account["error_kind"] == "unavailable"
    assert account["detail"] == str(ExchangeUnavailableError(status=503))


async def test_a_manual_sync_retries_an_auth_failed_account(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    venue = SimulatedVenue(recent_fills(), fault=always(ExchangeAuthError(status=401)))
    async with application(monkeypatch, {ExchangeKey.BITGET: venue}) as (_app, client):
        await sync(client)
        venue.fault = None
        calls_before = len(venue.calls)
        summary = await sync(client)

    assert len(venue.calls) > calls_before
    assert summary["accounts"][0]["status"] == "success"


# --------------------------------------------------------------------------------------
# GET /api/exchanges/runs
# --------------------------------------------------------------------------------------


async def test_runs_are_newest_first_with_their_accounts_sorted(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    providers = {
        ExchangeKey.BITGET: SimulatedVenue(recent_fills()),
        ExchangeKey.BINGX: SimulatedVenue(
            exchange_key=ExchangeKey.BINGX, fault=always(ExchangeUnavailableError(status=503))
        ),
    }
    async with application(monkeypatch, providers) as (_app, client):
        first = await sync(client)
        second = await sync(client)
        response = await client.get(RUNS)
        limited = await client.get(RUNS, params={"limit": 1})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"runs"}
    runs = payload["runs"]
    assert [run["run_id"] for run in runs] == [second["run_id"], first["run_id"]]
    assert all(set(run) == RUN_FIELDS for run in runs)
    newest = runs[0]
    assert newest["status"] == "partial"
    assert [account["exchange_key"] for account in newest["accounts"]] == ["bingx", "bitget"]
    assert newest["fills_seen"] == sum(account["fills_seen"] for account in newest["accounts"])
    assert newest["fills_inserted"] == sum(
        account["fills_inserted"] for account in newest["accounts"]
    )
    assert [run["run_id"] for run in limited.json()["runs"]] == [second["run_id"]]


@pytest.mark.parametrize(("limit", "status"), [(0, 422), (1, 200), (100, 200), (101, 422)])
async def test_the_runs_limit_is_bounded_at_both_ends(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch, limit: int, status: int
) -> None:
    del api_environment
    async with application(monkeypatch, {}) as (_app, client):
        response = await client.get(RUNS, params={"limit": limit})

    assert response.status_code == status


async def test_the_runs_limit_defaults_to_twenty(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch, {ExchangeKey.BITGET: SimulatedVenue()}) as (_app, client):
        for _ in range(21):
            await sync(client)
        response = await client.get(RUNS)

    assert len(response.json()["runs"]) == 20


# --------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), [("GET", EXCHANGES), ("POST", SYNC), ("GET", RUNS)])
async def test_every_exchange_endpoint_requires_a_session(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    del api_environment
    venue = SimulatedVenue(recent_fills())
    frozen = MappingProxyType({ExchangeKey.BITGET: venue})
    monkeypatch.setattr("portfolio.main.exchange_providers", lambda client, **_: frozen)
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as anonymous,
    ):
        response = await anonymous.request(method, path, headers=JSON_HEADERS)

    assert response.status_code == 401
    assert venue.calls == [], "an anonymous request reached the venue"


# --------------------------------------------------------------------------------------
# The dependencies
# --------------------------------------------------------------------------------------


def test_the_exchange_coordinator_dependency_refuses_an_application_whose_lifespan_never_ran() -> (
    None
):
    """A clear failure naming the lifespan, rather than an `AttributeError` elsewhere."""
    bare = FastAPI()
    request = Request({"type": "http", "app": bare, "method": "POST", "path": SYNC, "headers": []})

    with pytest.raises(RuntimeError, match="lifespan"):
        get_exchange_sync_coordinator(request)


def test_the_exchange_coordinator_dependency_refuses_the_balance_coordinator_in_its_place() -> None:
    """Only a `SyncCoordinator` on the exchange attribute counts; anything else is refused."""
    bare = FastAPI()
    bare.state.exchange_sync_coordinator = object()
    request = Request({"type": "http", "app": bare, "method": "POST", "path": SYNC, "headers": []})

    with pytest.raises(RuntimeError, match="lifespan"):
        get_exchange_sync_coordinator(request)


def test_an_application_whose_lifespan_never_ran_has_nothing_configured() -> None:
    assert configured_exchanges_of(FastAPI()) == frozenset()


async def test_an_interrupted_window_is_counted_as_pending_with_its_error(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that failed mid-window leaves one pending window, and `last_error` says why."""
    del api_environment

    def fail_page_two(call_number: int, window: object, cursor: str | None) -> Exception | None:
        del window, cursor
        return ExchangeUnavailableError(status=503) if call_number == 2 else None

    venue = SimulatedVenue(
        recent_fills(), max_query_window=timedelta(days=100), fault=fail_page_two
    )
    async with application(monkeypatch, {ExchangeKey.BITGET: venue}) as (_app, client):
        await sync(client)
        bitget = (await listed(client))["bitget"]

    assert bitget["status"] == "error"
    assert bitget["pending_windows"] == 1
    assert bitget["fills_stored"] == 3, "page 1 is kept"
    assert bitget["last_error"] == {
        "error_kind": "unavailable",
        "detail": str(ExchangeUnavailableError(status=503)),
    }
    assert bitget["last_synced_at"] is None


async def test_fills_stored_counts_each_accounts_own_fills(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    providers = {
        ExchangeKey.BITGET: SimulatedVenue(recent_fills(9)),
        ExchangeKey.BINGX: SimulatedVenue(recent_fills(4), exchange_key=ExchangeKey.BINGX),
    }
    async with application(monkeypatch, providers) as (_app, client):
        await sync(client)
        entries = await listed(client)

    assert (entries["bitget"]["fills_stored"], entries["bingx"]["fills_stored"]) == (9, 4)
