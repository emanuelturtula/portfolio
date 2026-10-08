"""Spec 029 (#22), criterion 4: the backup timer in the real lifespan.

It is not built when switched off; it follows the scheduler's first-run rule against the
newest copy's instant -- a fresh volume, or a copy older than one interval, gets a copy at
startup, and a recent copy means the timer sleeps what is left of the interval; and a tick
that fails is recorded and does not stop the loop. Ruling R4 is the last: a backup
directory that cannot be listed is attempted at once, and the attempt is recorded, rather
than waited on for a whole interval with nothing recorded.

The timer's sleep is replaced with one that parks until the test lets it go, at the
composition root where `main` looks the class up, and nothing else is: the copies are real
files from the real service over the database the lifespan migrated. What the sleep records
is the one outside trace of a decision **not** to copy at startup.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import pytest
from structlog.testing import capture_logs

from portfolio.config import get_settings
from portfolio.db.backup import BackupFile, take_copy
from portfolio.domain.backups import BackupErrorKind, instant_of
from portfolio.main import BACKUP_TASK_NAME, create_app
from portfolio.services.backup import BackupAttempt, BackupService, BackupState
from portfolio.services.scheduler import IntervalScheduler
from tests.backup_harness import copy_names, plant_copies
from tests.offline_http import use_an_offline_http_client

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi import FastAPI

PARK_TIMEOUT: Final = 10
DAY_SECONDS: Final = 86_400
SHUTDOWN_OBSERVED: Final = 0.2
"""How long shutdown is watched while a copy is held. Long enough for it to have finished
had it not waited: everything else it does takes milliseconds in this test."""


class GatedSleep:
    """The timer's sleep: it records the delay, then parks until the test releases it."""

    def __init__(self) -> None:
        self.delays: list[int] = []
        self._parked: asyncio.Queue[None] = asyncio.Queue()
        self._go = asyncio.Event()

    async def __call__(self, delay: int) -> None:
        self.delays.append(delay)
        self._parked.put_nowait(None)
        await self._go.wait()
        self._go.clear()

    async def parked(self) -> None:
        """Wait until the loop has reached its next sleep: every tick before it is done."""
        await asyncio.wait_for(self._parked.get(), timeout=PARK_TIMEOUT)

    def release(self) -> None:
        self._go.set()


@pytest.fixture
def backups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A lifespan with the backup timer on and every other timer off. Yields the directory."""
    database_path = tmp_path / "lifespan" / "portfolio.db"
    directory = tmp_path / "lifespan" / "backups"
    monkeypatch.setenv("PORTFOLIO_DATABASE_URL", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_ENABLED", "false")
    monkeypatch.setenv("PORTFOLIO_PRICE_REFRESH_ENABLED", "false")
    monkeypatch.setenv("PORTFOLIO_PRICE_BACKFILL_ENABLED", "false")
    monkeypatch.setenv("PORTFOLIO_BACKUP_ENABLED", "true")
    monkeypatch.setenv("PORTFOLIO_BACKUP_DIR", str(directory))
    monkeypatch.delenv("PORTFOLIO_BACKUP_INTERVAL_MINUTES", raising=False)
    use_an_offline_http_client(monkeypatch)
    get_settings.cache_clear()
    try:
        yield directory
    finally:
        get_settings.cache_clear()


@pytest.fixture
def sleep(monkeypatch: pytest.MonkeyPatch) -> GatedSleep:
    gated = GatedSleep()

    class GatedScheduler(IntervalScheduler):
        def __init__(self, **keywords: Any) -> None:
            keywords.setdefault("sleep", gated)
            super().__init__(**keywords)

    monkeypatch.setattr("portfolio.main.IntervalScheduler", GatedScheduler)
    return gated


def block(directory: Path) -> None:
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.write_bytes(b"a file where the directory should be")


def unblock(directory: Path) -> None:
    directory.unlink()


def service_of(app: FastAPI) -> BackupService:
    service = app.state.backup_service
    assert isinstance(service, BackupService)
    return service


def scheduler_of(app: FastAPI) -> IntervalScheduler:
    scheduler = app.state.backup_scheduler
    assert isinstance(scheduler, IntervalScheduler)
    return scheduler


def is_running(scheduler: IntervalScheduler) -> bool:
    """Read afresh through a call, so `mypy` does not narrow it across the lifespan's exit."""
    return scheduler.running


async def test_the_timer_is_not_built_when_switched_off(
    backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted as "no object", and as no copy taken, not as "no tick yet"."""
    monkeypatch.setenv("PORTFOLIO_BACKUP_ENABLED", "false")
    get_settings.cache_clear()
    app = create_app()

    with capture_logs() as captured:
        async with app.router.lifespan_context(app):
            assert app.state.backup_scheduler is None
            assert isinstance(app.state.backup_service, BackupService)

    assert {"event": "scheduler_disabled", "scheduler": "backup", "log_level": "info"} in (captured)
    assert copy_names(backups) == set()


async def test_a_fresh_volume_gets_a_copy_at_startup_and_the_timer_stops_with_the_app(
    backups: Path, sleep: GatedSleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interval is the setting's: 90 minutes, 5400 seconds to the next copy."""
    monkeypatch.setenv("PORTFOLIO_BACKUP_INTERVAL_MINUTES", "90")
    get_settings.cache_clear()
    app = create_app()
    before = datetime.now(UTC)

    async with app.router.lifespan_context(app):
        scheduler = scheduler_of(app)
        assert scheduler.name == BACKUP_TASK_NAME == "backup"
        assert is_running(scheduler) is True
        await sleep.parked()
        service = service_of(app)
        attempt = service.last_attempt
        status = await service.status()

    assert is_running(scheduler) is False
    assert sleep.delays == [5_400]
    (name,) = copy_names(backups)
    started = instant_of(name)
    assert started is not None
    assert before <= started <= datetime.now(UTC)
    assert attempt == BackupAttempt(at=started, failed=False, error_kind=None)
    assert status.state is BackupState.OK


async def test_a_recent_copy_means_no_copy_at_startup(backups: Path, sleep: GatedSleep) -> None:
    """A restart an hour after the last copy sleeps the 23 hours that are left."""
    planted = plant_copies(backups, [datetime.now(UTC) - timedelta(hours=1)])
    app = create_app()

    async with app.router.lifespan_context(app):
        await sleep.parked()
        attempt = service_of(app).last_attempt

    assert attempt is None
    assert copy_names(backups) == set(planted)
    (delay,) = sleep.delays
    assert DAY_SECONDS - 3_600 - 60 < delay <= DAY_SECONDS - 3_600


async def test_a_copy_older_than_one_interval_gets_a_copy_at_startup(
    backups: Path, sleep: GatedSleep
) -> None:
    planted = plant_copies(backups, [datetime.now(UTC) - timedelta(hours=25)])
    app = create_app()

    async with app.router.lifespan_context(app):
        await sleep.parked()
        attempt = service_of(app).last_attempt

    assert sleep.delays == [DAY_SECONDS]
    assert attempt is not None
    assert attempt.failed is False
    assert len(copy_names(backups) - set(planted)) == 1


async def test_a_copy_in_flight_at_shutdown_is_finished_before_the_lifespan_ends(
    backups: Path, sleep: GatedSleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R8 and R9: stopping the timer waits for a copy under way, and the copy logs its outcome.

    The startup copy is held in its worker thread until shutdown has begun. Shutdown is seen
    not to finish while the copy is held, and once it is let go the copy is in the directory
    and `backup_completed` was logged before the lifespan ended: the arithmetic
    `drain_coordinators` documents has the copy as its first term.
    """
    del sleep  # Installed so that a loop which outlives the copy parks rather than sleeps.
    copying = threading.Event()
    go = threading.Event()

    def held_take_copy(database: Path, directory: Path, started_at: datetime) -> BackupFile:
        copying.set()
        go.wait(PARK_TIMEOUT)
        return take_copy(database, directory, started_at)

    monkeypatch.setattr("portfolio.services.backup.take_copy", held_take_copy)
    app = create_app()
    lifespan = app.router.lifespan_context(app)
    with capture_logs() as captured:
        await lifespan.__aenter__()
        try:
            assert await asyncio.to_thread(copying.wait, PARK_TIMEOUT)
            shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
            await asyncio.sleep(SHUTDOWN_OBSERVED)
            finished_while_held = shutdown.done()
        finally:
            go.set()
        await asyncio.wait_for(shutdown, timeout=PARK_TIMEOUT)

    assert finished_while_held is False
    (name,) = copy_names(backups)
    (completed,) = [entry for entry in captured if entry["event"] == "backup_completed"]
    assert completed["name"] == name


async def test_the_backup_timer_is_stopped_last_so_no_other_timer_runs_while_a_copy_finishes(
    backups: Path, sleep: GatedSleep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R15: stopping the backup timer waits for a copy in flight (R9), so it is stopped after
    every other timer. Had it been stopped first, the balance and price timers would still be
    running -- free to start a tick -- for as long as the copy took.

    The copy is held in its thread from startup; shutdown is begun and watched; and while the
    copy is still held, no other timer is running any more.
    """
    del sleep  # Installed so that every timer parks in its sleep rather than really sleeping.
    monkeypatch.setenv("PORTFOLIO_BALANCE_SYNC_ENABLED", "true")
    monkeypatch.setenv("PORTFOLIO_PRICE_REFRESH_ENABLED", "true")
    get_settings.cache_clear()
    copying = threading.Event()
    go = threading.Event()

    def held_take_copy(database: Path, directory: Path, started_at: datetime) -> BackupFile:
        copying.set()
        go.wait(PARK_TIMEOUT)
        return take_copy(database, directory, started_at)

    monkeypatch.setattr("portfolio.services.backup.take_copy", held_take_copy)
    app = create_app()
    lifespan = app.router.lifespan_context(app)
    with capture_logs() as captured:
        await lifespan.__aenter__()
        try:
            others = [app.state.balance_scheduler, app.state.price_scheduler]
            assert all(isinstance(timer, IntervalScheduler) for timer in others)
            assert await asyncio.to_thread(copying.wait, PARK_TIMEOUT)
            running_before = [is_running(timer) for timer in others]
            shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
            await asyncio.sleep(SHUTDOWN_OBSERVED)
            running_while_held = [is_running(timer) for timer in others]
            finished_while_held = shutdown.done()
        finally:
            go.set()
        await asyncio.wait_for(shutdown, timeout=PARK_TIMEOUT)

    assert running_before == [True, True]
    assert running_while_held == [False, False]
    assert finished_while_held is False
    (name,) = copy_names(backups)
    (completed,) = [entry for entry in captured if entry["event"] == "backup_completed"]
    assert completed["name"] == name


async def test_an_unreadable_directory_is_attempted_at_once_and_a_failed_tick_does_not_stop_it(
    backups: Path, sleep: GatedSleep
) -> None:
    """R4, then criterion 4's "survives a failed tick".

    Had the startup check raised, the scheduler would have slept a whole interval first and
    recorded nothing: the attempt recorded at the first park is what tells the two apart.
    The directory is then put right, and the next tick takes a copy.
    """
    block(backups)
    app = create_app()

    with capture_logs() as captured:
        async with app.router.lifespan_context(app):
            await sleep.parked()
            service = service_of(app)
            first = service.last_attempt
            while_blocked = await service.status()
            unblock(backups)
            sleep.release()
            await sleep.parked()
            second = service.last_attempt
            after = await service.status()

    assert sleep.delays == [DAY_SECONDS, DAY_SECONDS]
    assert first is not None
    assert (first.failed, first.error_kind) == (True, BackupErrorKind.STORAGE_ERROR)
    assert while_blocked.state is BackupState.UNREADABLE
    assert while_blocked.last_error_kind is BackupErrorKind.STORAGE_ERROR
    events = [entry["event"] for entry in captured]
    assert "scheduler_startup_check_failed" not in events
    assert "scheduler_tick_failed" not in events, "a BackupError is an outcome, not a defect"
    # `mkdir(exist_ok=True)` over a file raises `FileExistsError`, on POSIX and on Windows.
    assert {
        "event": "backup_failed",
        "error_kind": "storage_error",
        "error_type": "FileExistsError",
        "log_level": "error",
    } in captured
    assert second is not None
    assert (second.failed, second.error_kind) == (False, None)
    assert after.state is BackupState.OK
    assert after.count == 1
