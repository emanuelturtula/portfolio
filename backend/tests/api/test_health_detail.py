"""Spec 029 (#22), criterion 5: `GET /api/health/detail`.

`401` without a session, like every path not on the allowlist; the five fields, exactly, with
every `state` reachable through the real router and the real service; and **no configuration
value** -- not the directory, not the interval, not the retention -- anywhere in the body.

The application is the real one through its real lifespan, signed in as the owner. Its
backup service is then replaced with one built over a directory and a clock this test
controls: the router reads `app.state.backup_service` on each request, which is the seam
`create_app` publishes, so nothing in the code under test is patched.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

import pytest
from fastapi import FastAPI, Request

from portfolio.api.dependencies import get_backup_service
from portfolio.services.backup import BackupService
from tests.backup_harness import T0, fixed, plant_copies, sqlite_url

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient

DETAIL: Final = "/api/health/detail"
IN_MEMORY: Final = "sqlite+aiosqlite:///:memory:"
FIELDS: Final = {"state", "latest_at", "count", "last_attempt_at", "last_error_kind"}

#: Settings no other value in the payload could coincide with, so their absence means
#: something: an interval of 4321 minutes, 11 days, 13 weeks.
INTERVAL: Final = 4321
KEEP_DAILY: Final = 11
KEEP_WEEKLY: Final = 13


def install(
    app: FastAPI,
    directory: Path,
    *,
    database_url: str = IN_MEMORY,
    enabled: bool = True,
) -> BackupService:
    service = BackupService(
        database_url=database_url,
        directory=directory,
        enabled=enabled,
        interval_minutes=INTERVAL,
        keep_daily=KEEP_DAILY,
        keep_weekly=KEEP_WEEKLY,
        clock=fixed(T0),
    )
    app.state.backup_service = service
    return service


async def detail(client: AsyncClient) -> dict[str, Any]:
    response = await client.get(DETAIL)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert set(body) == {"backup"}
    backup: dict[str, Any] = body["backup"]
    assert set(backup) == FIELDS
    return backup


def block(directory: Path) -> None:
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.write_bytes(b"a file where the directory should be")


# --------------------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------------------


async def test_without_a_session_it_is_401(api_client: AsyncClient) -> None:
    response = await api_client.get(DETAIL)

    assert response.status_code == 401


async def test_the_public_health_check_stays_public_and_cheap(api_client: AsyncClient) -> None:
    """The container's probe calls `/api/health` before anyone signs in; it is unchanged."""
    response = await api_client.get("/api/health")

    assert response.status_code == 200
    assert set(response.json()) == {"status", "version", "environment"}


# --------------------------------------------------------------------------------------
# Every state, through the router
# --------------------------------------------------------------------------------------


async def test_ok_serves_the_newest_copy_and_the_count(
    api_app: FastAPI, signed_in_api_client: AsyncClient, backup_directory: Path
) -> None:
    plant_copies(backup_directory, [T0 - timedelta(hours=1), T0 - timedelta(days=1)])
    install(api_app, backup_directory)

    assert await detail(signed_in_api_client) == {
        "state": "ok",
        "latest_at": "2026-10-02T02:00:00.123456Z",
        "count": 2,
        "last_attempt_at": None,
        "last_error_kind": None,
    }


async def test_ok_after_the_timers_own_success_serves_the_attempt(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    backup_directory: Path,
    live_database: Path,
) -> None:
    service = install(api_app, backup_directory, database_url=sqlite_url(live_database))
    await service.take_scheduled()

    assert await detail(signed_in_api_client) == {
        "state": "ok",
        "latest_at": "2026-10-02T03:00:00.123456Z",
        "count": 1,
        "last_attempt_at": "2026-10-02T03:00:00.123456Z",
        "last_error_kind": None,
    }


async def test_pending_before_the_first_copy(
    api_app: FastAPI, signed_in_api_client: AsyncClient, backup_directory: Path
) -> None:
    install(api_app, backup_directory)

    assert await detail(signed_in_api_client) == {
        "state": "pending",
        "latest_at": None,
        "count": 0,
        "last_attempt_at": None,
        "last_error_kind": None,
    }


async def test_stale_when_the_newest_copy_is_older_than_two_intervals(
    api_app: FastAPI, signed_in_api_client: AsyncClient, backup_directory: Path
) -> None:
    plant_copies(backup_directory, [T0 - timedelta(minutes=2 * INTERVAL + 1)])
    install(api_app, backup_directory)

    served = await detail(signed_in_api_client)

    assert served["state"] == "stale"
    assert served["count"] == 1


async def test_failed_serves_the_kind_and_when(
    api_app: FastAPI, signed_in_api_client: AsyncClient, backup_directory: Path
) -> None:
    plant_copies(backup_directory, [T0 - timedelta(hours=3)])
    service = install(api_app, backup_directory)
    await service.take_scheduled()

    assert await detail(signed_in_api_client) == {
        "state": "failed",
        "latest_at": "2026-10-02T00:00:00.123456Z",
        "count": 1,
        "last_attempt_at": "2026-10-02T03:00:00.123456Z",
        "last_error_kind": "database_error",
    }


async def test_failed_by_a_defect_serves_a_null_kind(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    backup_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case the frontend must word without a kind: `failed` with `last_error_kind: null`."""
    service = install(api_app, backup_directory)

    def defect(started_at: object) -> object:
        message = "a defect"
        raise RuntimeError(message)

    monkeypatch.setattr(service, "_copy_and_rotate", defect)
    with pytest.raises(RuntimeError):
        await service.take_scheduled()

    served = await detail(signed_in_api_client)

    assert served["state"] == "failed"
    assert served["last_error_kind"] is None
    assert served["last_attempt_at"] == "2026-10-02T03:00:00.123456Z"


async def test_disabled_still_serves_the_copies_there_are(
    api_app: FastAPI, signed_in_api_client: AsyncClient, backup_directory: Path
) -> None:
    plant_copies(backup_directory, [T0 - timedelta(days=40)])
    install(api_app, backup_directory, enabled=False)

    assert await detail(signed_in_api_client) == {
        "state": "disabled",
        "latest_at": "2026-08-23T03:00:00.123456Z",
        "count": 1,
        "last_attempt_at": None,
        "last_error_kind": None,
    }


async def test_unreadable_is_served_with_the_unknowns_as_null_not_a_500(
    api_app: FastAPI, signed_in_api_client: AsyncClient, backup_directory: Path
) -> None:
    """R4: the tech lead's browser check found a 500 here and a silent dashboard."""
    block(backup_directory)
    service = install(api_app, backup_directory)
    await service.take_scheduled()

    assert await detail(signed_in_api_client) == {
        "state": "unreadable",
        "latest_at": None,
        "count": None,
        "last_attempt_at": "2026-10-02T03:00:00.123456Z",
        "last_error_kind": "database_error",
    }


# --------------------------------------------------------------------------------------
# No configuration value is served
# --------------------------------------------------------------------------------------


async def test_no_configuration_value_is_in_the_body(
    api_app: FastAPI, signed_in_api_client: AsyncClient, tmp_path: Path
) -> None:
    """Not the directory, in any spelling, and not the interval or either retention."""
    directory = tmp_path / "a-directory-only-the-operator-knows"
    plant_copies(directory, [T0 - timedelta(hours=1)])
    install(api_app, directory)

    response = await signed_in_api_client.get(DETAIL)

    text = response.text
    assert response.status_code == 200
    for value in (
        str(directory),
        directory.as_posix(),
        directory.name,
        str(tmp_path),
        str(INTERVAL),
        str(KEEP_DAILY),
        str(KEEP_WEEKLY),
    ):
        assert value not in text, value
    assert text.count('"count":1') == 1


# --------------------------------------------------------------------------------------
# The dependency and the schema
# --------------------------------------------------------------------------------------


def test_an_application_with_no_backup_service_is_a_clear_failure() -> None:
    bare = FastAPI()
    request = Request({"type": "http", "app": bare, "headers": []})

    with pytest.raises(RuntimeError, match="No backup service is installed"):
        get_backup_service(request)


def test_the_application_publishes_its_service_without_starting_anything(app: FastAPI) -> None:
    """`create_app` builds the service; building it reads nothing from the file system."""
    assert isinstance(app.state.backup_service, BackupService)


def test_the_schema_names_the_six_states_and_the_three_kinds(app: FastAPI) -> None:
    """What the generated TypeScript unions are built from, so a page's wording is total."""
    document = app.openapi()
    operation = document["paths"][DETAIL]["get"]
    schemas = document["components"]["schemas"]
    backup = schemas["BackupStatusResponse"]["properties"]

    assert operation["operationId"] == "getHealthDetail"
    assert schemas["BackupState"]["enum"] == [
        "unreadable",
        "disabled",
        "failed",
        "stale",
        "pending",
        "ok",
    ]
    assert schemas["BackupErrorKind"]["enum"] == [
        "database_error",
        "integrity_failed",
        "storage_error",
    ]
    assert set(backup) == FIELDS
    assert set(schemas["BackupStatusResponse"]["required"]) == FIELDS
    assert {"type": "null"} in backup["count"]["anyOf"]
    assert {"type": "integer"} in backup["count"]["anyOf"]
    assert {"type": "null"} in backup["latest_at"]["anyOf"]
