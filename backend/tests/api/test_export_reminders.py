"""The monthly export reminder over HTTP, the whole stack (spec 040).

Middleware, router, service, domain and SQLite all run. The service's clock is pinned
through a dependency override, so "now" is a value the test chose.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

# A runtime import: FastAPI reads the override's `request: Request` annotation when it builds
# the dependency, and a type-checking-only name would make it a query parameter.
from fastapi import Request  # noqa: TC002
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from portfolio.api.dependencies import get_export_reminder_service
from portfolio.main import create_app
from portfolio.services.export_reminders import build_export_reminder_service
from tests.auth.conftest import BASE_URL, JSON_HEADERS, sign_in

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI

    from portfolio.services.export_reminders import ExportReminderService

REMINDER: Final = "/api/exports/reminder"
EXCHANGES: Final = ["Binance", "Bitget", "BingX", "Nexo"]


def done(month: str) -> str:
    return f"/api/exports/months/{month}/done"


@asynccontextmanager
async def application(now: datetime) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    """The real application, its lifespan run, signed in, with the reminder's clock at `now`."""
    app = create_app()

    async def pinned(request: Request) -> AsyncIterator[ExportReminderService]:
        async with request.app.state.db_sessionmaker() as session:
            yield build_export_reminder_service(session, clock=lambda: now)

    app.dependency_overrides[get_export_reminder_service] = pinned
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
            await sign_in(client)
            yield app, client


async def test_both_endpoints_require_a_session(api_environment: Path) -> None:
    del api_environment
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
            assert (await client.get(REMINDER)).status_code == 401
            response = await client.post(done("2026-09"), headers=JSON_HEADERS)
            assert response.status_code == 401


async def test_nothing_is_owed_before_the_first_month_ends(api_environment: Path) -> None:
    del api_environment
    async with application(datetime(2026, 9, 30, 12, 0, tzinfo=UTC)) as (_, client):
        response = await client.get(REMINDER)

    assert response.status_code == 200
    assert response.json() == {"months": [], "exchanges": EXCHANGES}


async def test_every_closed_month_is_owed_until_it_is_marked_done(api_environment: Path) -> None:
    del api_environment
    async with application(datetime(2026, 11, 1, 3, 0, tzinfo=UTC)) as (app, client):
        before = await client.get(REMINDER)
        marked = await client.post(done("2026-09"), headers=JSON_HEADERS)
        after = await client.get(REMINDER)
        async with app.state.db_sessionmaker() as session:
            rows = (await session.execute(text("SELECT month FROM export_months"))).all()

    assert before.json()["months"] == ["2026-09", "2026-10"]
    assert marked.status_code == 200
    assert marked.json() == {"months": ["2026-10"], "exchanges": EXCHANGES}
    assert after.json()["months"] == ["2026-10"]
    assert [row.month for row in rows] == ["2026-09-01"]


async def test_marking_a_month_twice_changes_nothing(api_environment: Path) -> None:
    del api_environment
    async with application(datetime(2026, 10, 9, tzinfo=UTC)) as (app, client):
        first = await client.post(done("2026-09"), headers=JSON_HEADERS)
        second = await client.post(done("2026-09"), headers=JSON_HEADERS)
        async with app.state.db_sessionmaker() as session:
            count = await session.scalar(text("SELECT count(*) FROM export_months"))

    assert first.status_code == second.status_code == 200
    assert second.json()["months"] == []
    assert count == 1


async def test_a_month_that_has_not_ended_is_a_conflict(api_environment: Path) -> None:
    del api_environment
    async with application(datetime(2026, 10, 9, tzinfo=UTC)) as (_, client):
        current = await client.post(done("2026-10"), headers=JSON_HEADERS)
        before_first = await client.post(done("2026-08"), headers=JSON_HEADERS)

    assert current.status_code == 409
    assert "has not ended" in current.json()["detail"]
    assert before_first.status_code == 409


async def test_a_month_that_is_not_yyyy_mm_is_refused(api_environment: Path) -> None:
    del api_environment
    async with application(datetime(2026, 10, 9, tzinfo=UTC)) as (_, client):
        statuses = [
            (await client.post(done(month), headers=JSON_HEADERS)).status_code
            for month in ("2026-13", "2026-9", "september")
        ]

    assert statuses == [422, 422, 422]
