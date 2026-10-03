"""Spec 029 (#22): the backup service -- the state rule, what a copy logs, and rotation's limits.

Criterion 2's second half, "nothing is rotated after a failure", and criterion 3's "never a
file that does not match the name pattern" are here, against a real migrated database and a
real directory, because both are statements about which files exist afterwards. Criterion 8's
"failures are logged with their `error_kind`" is the logging block, read with
`capture_logs`; the security suite reads the same lines off stdout for the absence of rows.

**The state rule** is driven through `status(now)` with every input set by hand: the
directory's copies (planted by name -- `status` reads names and sizes only), the timer's last
attempt (made by calling `take_scheduled` on a service whose copy succeeds, fails, or hits a
defect), and `now`. Each row of the spec's table is reached, each in the presence of what the
rows below it would answer, so that the order of the rules is what is pinned and not only
their conditions.
"""

from __future__ import annotations

import asyncio
import copy
import gc
import threading
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import anyio
import anyio.lowlevel
import pytest
from structlog.testing import capture_logs

from portfolio.db import backup as db_backup
from portfolio.db.backup import BackupError, BackupFile, list_copies
from portfolio.domain.backups import BackupErrorKind, backup_name, instant_of
from portfolio.services import backup as service_module
from portfolio.services.backup import (
    STALE_AFTER_INTERVALS,
    BackupAttempt,
    BackupService,
    BackupState,
    BackupStatus,
    RotationFailedError,
)
from tests.backup_harness import (
    T0,
    add_notes,
    copy_names,
    corrupt_with_orphan_pages,
    fixed,
    plant_copies,
    sidecars_of,
    sqlite_url,
    table_contents,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, MutableMapping, Sequence

DAY: Final = 1440
NAME: Final = backup_name(T0)
IN_MEMORY: Final = "sqlite+aiosqlite:///:memory:"


def service_for(
    database: Path | str,
    directory: Path,
    *,
    enabled: bool = True,
    interval_minutes: int = DAY,
    keep_daily: int = 7,
    keep_weekly: int = 4,
    clock: Callable[[], datetime] | None = None,
) -> BackupService:
    return BackupService(
        database_url=database if isinstance(database, str) else sqlite_url(database),
        directory=directory,
        enabled=enabled,
        interval_minutes=interval_minutes,
        keep_daily=keep_daily,
        keep_weekly=keep_weekly,
        clock=fixed(T0) if clock is None else clock,
    )


@contextmanager
def moved_away(database: Path) -> Iterator[None]:
    """The live database is somewhere else for the length of the block."""
    away = database.with_name("away.db")
    database.rename(away)
    try:
        yield
    finally:
        away.rename(database)


async def fail_once(service: BackupService, database: Path) -> None:
    """Give `service` a failed last attempt: its database is moved away for one tick."""
    with moved_away(database):
        await service.take_scheduled()
    assert service.last_attempt == BackupAttempt(
        at=T0, failed=True, error_kind=BackupErrorKind.DATABASE_ERROR
    )


def block(directory: Path) -> None:
    """Put a file where `directory` should be, so that listing it fails."""
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.write_bytes(b"a file where the directory should be")


# --------------------------------------------------------------------------------------
# The state, first rule that applies
# --------------------------------------------------------------------------------------


async def test_a_fresh_installation_is_pending(backup_directory: Path) -> None:
    """No copy and no attempt: the seconds before the first copy, and not a warning."""
    status = await service_for(IN_MEMORY, backup_directory).status(T0)

    assert status == BackupStatus(
        state=BackupState.PENDING,
        latest_at=None,
        count=0,
        last_attempt_at=None,
        last_error_kind=None,
    )


async def test_a_recent_copy_is_ok_before_any_attempt(backup_directory: Path) -> None:
    """R7: `ok` is also served before this process's first attempt, as after a restart."""
    plant_copies(backup_directory, [T0 - timedelta(hours=1), T0 - timedelta(days=1)], b"12")
    (backup_directory / "notes.txt").write_bytes(b"not a copy")

    status = await service_for(IN_MEMORY, backup_directory).status(T0)

    assert status == BackupStatus(
        state=BackupState.OK,
        latest_at=T0 - timedelta(hours=1),
        count=2,
        last_attempt_at=None,
        last_error_kind=None,
    )


async def test_a_successful_attempt_is_ok_with_no_error_kind(
    live_database: Path, backup_directory: Path
) -> None:
    service = service_for(live_database, backup_directory)

    await service.take_scheduled()
    status = await service.status(T0 + timedelta(minutes=1))

    assert status == BackupStatus(
        state=BackupState.OK,
        latest_at=T0,
        count=1,
        last_attempt_at=T0,
        last_error_kind=None,
    )


@pytest.mark.parametrize(
    ("interval_minutes", "age", "state"),
    [
        (DAY, timedelta(days=2), BackupState.OK),
        (DAY, timedelta(days=2, microseconds=1), BackupState.STALE),
        (90, timedelta(minutes=180), BackupState.OK),
        (90, timedelta(minutes=180, microseconds=1), BackupState.STALE),
    ],
    ids=["two days", "two days and 1 us", "three hours of 90", "three hours and 1 us of 90"],
)
async def test_a_copy_older_than_two_intervals_is_stale(
    backup_directory: Path, interval_minutes: int, age: timedelta, state: BackupState
) -> None:
    """Older than, not as old as: the copy one interval old is the one about to be replaced."""
    plant_copies(backup_directory, [T0 - age])
    service = service_for(IN_MEMORY, backup_directory, interval_minutes=interval_minutes)

    assert (await service.status(T0)).state is state
    assert STALE_AFTER_INTERVALS == 2


async def test_a_copy_named_in_the_future_is_the_newest_until_its_date_passes(
    backup_directory: Path,
) -> None:
    """R8, accepted as it is: after the clock was once ahead, that copy is `latest_at`.

    And the state stays `ok` while it is: a copy dated after `now` is not older than two
    intervals. Once the date has passed it ages like any other copy.
    """
    future = T0 + timedelta(days=3)
    plant_copies(backup_directory, [T0 - timedelta(hours=1), future])
    service = service_for(IN_MEMORY, backup_directory)

    now = await service.status(T0)
    long_after = await service.status(future + timedelta(days=2, microseconds=1))

    assert (now.state, now.latest_at, now.count) == (BackupState.OK, future, 2)
    assert (long_after.state, long_after.latest_at) == (BackupState.STALE, future)
    assert await service.last_run_at() == future


async def test_no_copy_after_an_attempt_finished_is_stale(
    live_database: Path, backup_directory: Path
) -> None:
    """In practice: a copy succeeded, and its file was then removed by hand."""
    service = service_for(live_database, backup_directory)
    await service.take_scheduled()
    (backup_directory / NAME).unlink()

    status = await service.status(T0)

    assert status.state is BackupState.STALE
    assert (status.latest_at, status.count) == (None, 0)
    assert status.last_attempt_at == T0


async def test_a_failed_attempt_is_failed_even_beside_a_recent_copy(
    backup_directory: Path,
) -> None:
    plant_copies(backup_directory, [T0 - timedelta(minutes=30)])
    service = service_for(IN_MEMORY, backup_directory)

    await service.take_scheduled()
    status = await service.status(T0)

    assert status == BackupStatus(
        state=BackupState.FAILED,
        latest_at=T0 - timedelta(minutes=30),
        count=1,
        last_attempt_at=T0,
        last_error_kind=BackupErrorKind.DATABASE_ERROR,
    )


async def test_a_failed_attempt_is_failed_even_with_a_stale_copy_or_none(
    backup_directory: Path,
) -> None:
    """`failed` comes before both halves of `stale`."""
    service = service_for(IN_MEMORY, backup_directory)
    await service.take_scheduled()

    assert (await service.status(T0)).state is BackupState.FAILED
    plant_copies(backup_directory, [T0 - timedelta(days=30)])
    assert (await service.status(T0)).state is BackupState.FAILED


async def test_a_defect_is_failed_with_no_kind(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a backup outcome: re-raised for the scheduler's traceback, and recorded as failed."""
    service = service_for(live_database, backup_directory)

    def defect(started_at: datetime) -> Any:
        message = "a bug, not a backup outcome"
        raise RuntimeError(message)

    monkeypatch.setattr(service, "_copy_and_rotate", defect)

    with pytest.raises(RuntimeError, match="a bug"):
        await service.take_scheduled()
    status = await service.status(T0)

    assert service.last_attempt == BackupAttempt(at=T0, failed=True, error_kind=None)
    assert status.state is BackupState.FAILED
    assert status.last_error_kind is None


async def test_a_success_after_a_failure_clears_it(
    live_database: Path, backup_directory: Path
) -> None:
    service = service_for(live_database, backup_directory)
    await fail_once(service, live_database)
    assert (await service.status(T0)).state is BackupState.FAILED

    await service.take_scheduled()

    assert service.last_attempt == BackupAttempt(at=T0, failed=False, error_kind=None)
    assert (await service.status(T0)).state is BackupState.OK


async def test_disabled_comes_before_failed_and_stale(backup_directory: Path) -> None:
    """Off means off: the timer's own state is not what the owner should be warned about."""
    plant_copies(backup_directory, [T0 - timedelta(days=30)])
    service = service_for(IN_MEMORY, backup_directory, enabled=False)
    await service.take_scheduled()

    status = await service.status(T0)

    assert status.state is BackupState.DISABLED
    assert status.latest_at == T0 - timedelta(days=30)
    assert status.count == 1
    assert status.last_error_kind is BackupErrorKind.DATABASE_ERROR


async def test_disabled_with_no_copy_is_disabled_not_pending(backup_directory: Path) -> None:
    status = await service_for(IN_MEMORY, backup_directory, enabled=False).status(T0)

    assert status.state is BackupState.DISABLED


async def test_a_directory_that_cannot_be_listed_is_unreadable_before_everything(
    backup_directory: Path,
) -> None:
    """R4: first in the order, and `latest_at` and `count` unknown rather than zero.

    Disabled, and with a failed attempt, and it is still `unreadable`: the directory is the
    thing the owner cannot see past. The last attempt is still served.
    """
    block(backup_directory)
    service = service_for(IN_MEMORY, backup_directory, enabled=False)
    await service.take_scheduled()

    with capture_logs() as captured:
        status = await service.status(T0)

    assert status == BackupStatus(
        state=BackupState.UNREADABLE,
        latest_at=None,
        count=None,
        last_attempt_at=T0,
        last_error_kind=BackupErrorKind.DATABASE_ERROR,
    )
    assert captured == [], "status runs on every request for it, and logs nothing"


async def test_unreadable_before_any_attempt_carries_no_attempt(backup_directory: Path) -> None:
    block(backup_directory)

    status = await service_for(IN_MEMORY, backup_directory).status(T0)

    assert status == BackupStatus(
        state=BackupState.UNREADABLE,
        latest_at=None,
        count=None,
        last_attempt_at=None,
        last_error_kind=None,
    )


async def test_status_reads_the_services_clock_when_given_no_instant(
    backup_directory: Path,
) -> None:
    plant_copies(backup_directory, [T0 - timedelta(days=3)])
    stale_by_its_clock = service_for(IN_MEMORY, backup_directory, clock=fixed(T0))
    fresh_by_its_clock = service_for(
        IN_MEMORY, backup_directory, clock=fixed(T0 - timedelta(days=2))
    )

    assert (await stale_by_its_clock.status()).state is BackupState.STALE
    assert (await fresh_by_its_clock.status()).state is BackupState.OK


def test_the_states_are_the_six_the_spec_names_in_its_order() -> None:
    """Served, and worded by the frontend, so pinned as strings -- in the table's order."""
    assert [state.value for state in BackupState] == [
        "unreadable",
        "disabled",
        "failed",
        "stale",
        "pending",
        "ok",
    ]


# --------------------------------------------------------------------------------------
# Taking a copy: the result, the log lines, and rotation after success only
# --------------------------------------------------------------------------------------


async def test_take_returns_the_copy_and_logs_its_fields(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`duration_ms` is whole milliseconds, floored, from the monotonic clock."""
    readings = iter([5_000_000_000, 5_002_999_999])
    monkeypatch.setattr(service_module, "monotonic_ns", lambda: next(readings))
    old = plant_copies(backup_directory, [T0 - timedelta(days=60)], b"old")

    service = service_for(live_database, backup_directory, keep_daily=1, keep_weekly=0)

    with capture_logs() as captured:
        result = await service.take()

    assert result.name == NAME
    assert result.size_bytes == (backup_directory / NAME).stat().st_size
    assert result.duration_ms == 2
    assert result.deleted == tuple(old)
    assert captured == [
        {
            "event": "backup_completed",
            "name": NAME,
            "bytes": result.size_bytes,
            "duration_ms": 2,
            "deleted": 1,
            "log_level": "info",
        }
    ]


async def test_a_failed_copy_logs_its_kind_and_the_root_class_and_deletes_nothing(
    live_database: Path, backup_directory: Path
) -> None:
    """Criterion 2: rotation runs only after a copy that succeeded."""
    planted = plant_copies(
        backup_directory, [T0 - timedelta(days=days) for days in (30, 60, 90)], b"old"
    )
    corrupt_with_orphan_pages(live_database)
    service = service_for(live_database, backup_directory, keep_daily=1, keep_weekly=0)

    with capture_logs() as captured, pytest.raises(BackupError) as caught:
        await service.take()

    assert caught.value.error_kind is BackupErrorKind.INTEGRITY_FAILED
    assert not isinstance(caught.value, RotationFailedError)
    assert copy_names(backup_directory) == set(planted)
    assert captured == [
        {
            "event": "backup_failed",
            "error_kind": "integrity_failed",
            "error_type": "CheckFailedError",
            "log_level": "error",
        }
    ]


@pytest.mark.parametrize(
    ("database", "error_type"),
    [(IN_MEMORY, "BackupError"), ("missing", "BackupError")],
    ids=["no file in the URL", "no file at the path"],
)
async def test_a_failure_with_no_cause_names_its_own_class(
    tmp_path: Path, backup_directory: Path, database: str, error_type: str
) -> None:
    url = database if database == IN_MEMORY else sqlite_url(tmp_path / "data" / "portfolio.db")

    with capture_logs() as captured, pytest.raises(BackupError) as caught:
        await service_for(url, backup_directory).take()

    assert caught.value.error_kind is BackupErrorKind.DATABASE_ERROR
    assert captured[0]["error_type"] == error_type
    assert "kept" not in captured[0]


async def test_rotation_keeps_the_rule_and_never_touches_what_is_not_a_copy(
    live_database: Path, backup_directory: Path
) -> None:
    """Criterion 3: a file that does not match the name pattern is never deleted, however old."""
    strangers = [
        "notes.txt",
        "portfolio-20200101T000000000000Z.sqlite3.bak",
        "portfolio-20201302T030000123456Z.sqlite3",
        "Portfolio-20200101T000000000000Z.sqlite3",
        "portfolio.db",
    ]
    for name in strangers:
        (backup_directory / name).parent.mkdir(parents=True, exist_ok=True)
        (backup_directory / name).write_bytes(b"the operator's own")
    kept_by_day = plant_copies(backup_directory, [T0 - timedelta(days=1, hours=1)], b"1")
    deleted = plant_copies(
        backup_directory, [T0 - timedelta(days=days) for days in (20, 40)], b"old"
    )

    result = await service_for(live_database, backup_directory, keep_daily=2, keep_weekly=0).take()

    assert set(result.deleted) == set(deleted)
    assert copy_names(backup_directory) == {*strangers, *kept_by_day, NAME}


async def test_the_copy_just_taken_is_never_deleted_even_behind_a_clock_stepped_back(
    live_database: Path, backup_directory: Path
) -> None:
    """Copies named in the future, after the clock was once ahead (R8): rotation keeps those,
    and the copy just taken is older than all of them -- but it is never among the deleted."""
    future = plant_copies(backup_directory, [T0 + timedelta(days=3)], b"from the future")

    result = await service_for(live_database, backup_directory, keep_daily=1, keep_weekly=0).take()

    assert result.deleted == ()
    assert copy_names(backup_directory) == {*future, NAME}


async def test_a_rotation_that_fails_keeps_the_copy_and_names_it(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7: the attempt failed, but it left a copy, and the log says which."""
    (old,) = plant_copies(backup_directory, [T0 - timedelta(days=60)], b"old")
    real_unlink = Path.unlink

    def refuse(self: Path, missing_ok: bool = False) -> None:
        if self.name == old:
            raise PermissionError(13, "Permission denied", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse)
    service = service_for(live_database, backup_directory, keep_daily=1, keep_weekly=0)

    with capture_logs() as captured, pytest.raises(RotationFailedError) as caught:
        await service.take()

    assert caught.value.kept == NAME
    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR
    assert str(caught.value).startswith(
        f"The backup {NAME} was taken and kept, but the rotation after it failed, and may "
        "have deleted only some of the older backups it meant to: An old backup, "
    )
    assert isinstance(caught.value.__cause__, BackupError)
    assert copy_names(backup_directory) == {NAME, old}
    assert table_contents(backup_directory / NAME) == table_contents(live_database)
    assert captured == [
        {
            "event": "backup_failed",
            "error_kind": "storage_error",
            "error_type": "PermissionError",
            "kept": NAME,
            "log_level": "error",
        }
    ]


async def test_a_rotation_that_cannot_list_the_directory_keeps_the_copy_too(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(directory: Path) -> Any:
        message = f"The backup directory {directory} cannot be read: denied"
        raise BackupError(BackupErrorKind.STORAGE_ERROR, message) from PermissionError(13, "x")

    monkeypatch.setattr(service_module, "list_copies", unreadable)
    service = service_for(live_database, backup_directory)

    with pytest.raises(RotationFailedError) as caught:
        await service.take()

    assert caught.value.kept == NAME
    assert copy_names(backup_directory) == {NAME}


async def test_a_rotation_failure_is_recorded_as_a_failed_attempt(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(directory: Path) -> Any:
        raise BackupError(BackupErrorKind.STORAGE_ERROR, f"{directory} cannot be read")

    monkeypatch.setattr(service_module, "list_copies", unreadable)
    service = service_for(live_database, backup_directory)

    await service.take_scheduled()

    assert service.last_attempt == BackupAttempt(
        at=T0, failed=True, error_kind=BackupErrorKind.STORAGE_ERROR
    )


@pytest.mark.parametrize("duplicate", [copy.copy, copy.deepcopy])
def test_a_rotation_failure_copies_with_its_kept_name(duplicate: Callable[[Any], Any]) -> None:
    original = RotationFailedError(NAME, BackupErrorKind.STORAGE_ERROR, "the rotation failed")

    clone = duplicate(original)

    assert type(clone) is RotationFailedError
    assert (clone.kept, clone.error_kind, str(clone)) == (
        NAME,
        BackupErrorKind.STORAGE_ERROR,
        "the rotation failed",
    )


def test_a_rotation_failure_rebuilds_as_pickle_would() -> None:
    """`BaseException` rebuilds from `self.args`, the message alone; `__reduce__` must give
    all three arguments, or a copy across processes loses the kept name."""
    original = RotationFailedError(NAME, BackupErrorKind.STORAGE_ERROR, "the rotation failed")
    factory, arguments = original.__reduce__()

    clone = factory(*arguments)

    assert (clone.kept, clone.error_kind, str(clone)) == (
        NAME,
        BackupErrorKind.STORAGE_ERROR,
        "the rotation failed",
    )


# --------------------------------------------------------------------------------------
# The timer's tick, and its "last run"
# --------------------------------------------------------------------------------------


async def test_a_backup_error_on_the_timer_is_recorded_and_swallowed(
    backup_directory: Path,
) -> None:
    service = service_for(IN_MEMORY, backup_directory)

    await service.take_scheduled()

    assert service.last_attempt == BackupAttempt(
        at=T0, failed=True, error_kind=BackupErrorKind.DATABASE_ERROR
    )


async def test_a_cancelled_tick_records_nothing(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop during a copy is neither a success nor a failure of the copy."""
    service = service_for(live_database, backup_directory)
    await fail_once(service, live_database)
    before = service.last_attempt
    entered = asyncio.Event()

    async def hang(started_at: datetime) -> Any:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_take", hang)
    tick = asyncio.create_task(service.take_scheduled())
    await asyncio.wait_for(entered.wait(), timeout=5)
    tick.cancel()

    with pytest.raises(asyncio.CancelledError):
        await tick
    assert service.last_attempt == before


# --------------------------------------------------------------------------------------
# Ruling R9: a copy in flight is finished and logged before a cancellation goes through
# --------------------------------------------------------------------------------------

HOLD_TIMEOUT: Final = 10
WATCHED: Final = 0.1
"""How long a cancelled task is watched while its copy is held. Had the cancellation gone
straight through, the task would have ended within a few loop iterations."""


class HeldCopy:
    """`take_copy`, held in its worker thread until the test lets it go, then real or failing."""

    def __init__(self) -> None:
        self.copying = threading.Event()
        self.go = threading.Event()
        self.finished = threading.Event()
        self.fail = False
        self.defect: Exception | None = None

    def __call__(self, database: Path, directory: Path, started_at: datetime) -> BackupFile:
        try:
            self.copying.set()
            self.go.wait(HOLD_TIMEOUT)
            if self.defect is not None:
                raise self.defect
            if self.fail:
                message = "The disk is full."
                raise BackupError(BackupErrorKind.STORAGE_ERROR, message)
            return db_backup.take_copy(database, directory, started_at)
        finally:
            self.finished.set()

    async def started(self) -> None:
        assert await asyncio.to_thread(self.copying.wait, HOLD_TIMEOUT)

    async def ended(self) -> None:
        """Wait for the worker thread to return, for a test whose task did not wait for it."""
        assert await asyncio.to_thread(self.finished.wait, HOLD_TIMEOUT)


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> Iterator[HeldCopy]:
    copy_step = HeldCopy()
    monkeypatch.setattr(service_module, "take_copy", copy_step)
    try:
        yield copy_step
    finally:
        copy_step.go.set()


async def cancel_while_held(
    task: asyncio.Task[Any], held: HeldCopy, reasons: Sequence[str]
) -> bool:
    """Cancel `task` once per reason while its copy is held; whether it was still running.

    Each cancellation is delivered before the next is made: the task is parked in an await
    on the copy, so one turn of the loop takes it through its `except` and back to the wait.
    """
    await held.started()
    for reason in reasons:
        task.cancel(reason)
        await asyncio.sleep(0)
    await asyncio.sleep(WATCHED)
    still_running = not task.done()
    held.go.set()
    return still_running


async def cancellation_of(task: asyncio.Task[Any]) -> tuple[Any, ...]:
    """The arguments of the `CancelledError` that `task` ends with, and nothing else of it.

    The error is not kept: its traceback holds every frame it went through, the finished
    copy's task among their locals, and a test that wants that task collected must let go.
    """
    try:
        await task
    except asyncio.CancelledError as exc:
        return exc.args
    message = "The task was not cancelled."
    raise AssertionError(message)


def the_copy_task(tick: asyncio.Task[Any]) -> weakref.ref[asyncio.Task[Any]]:
    """A weak reference to the one task besides `tick` and the test's own: the held copy."""
    (copy_task,) = asyncio.all_tasks() - {tick, asyncio.current_task()}
    return weakref.ref(copy_task)


@pytest.mark.parametrize(
    "reasons", [("stop",), ("stop", "again", "and again")], ids=["once", "three times in a row"]
)
async def test_a_cancelled_copy_is_finished_and_logged_before_the_cancellation_goes_through(
    live_database: Path, backup_directory: Path, held: HeldCopy, reasons: tuple[str, ...]
) -> None:
    """R9: the timer's stop cancels its tick, and the tick still ends with a copy and a line.

    The cancellation then goes through, and the tick records nothing: the process is stopping.
    It is the first cancellation that goes through, as `_finish_before_cancelling` documents:
    the one the stop asked for, not one that arrived while the copy was being finished.
    """
    service = service_for(live_database, backup_directory)

    with capture_logs() as captured:
        tick = asyncio.create_task(service.take_scheduled())
        still_running = await cancel_while_held(tick, held, reasons)
        raised_with = await cancellation_of(tick)

    assert still_running is True
    assert raised_with == ("stop",)
    assert copy_names(backup_directory) == {NAME}
    assert [entry["event"] for entry in captured] == ["backup_completed"]
    assert service.last_attempt is None


@dataclass(frozen=True)
class CancelledTick:
    """What a tick cancelled once while its copy was held showed, and what asyncio reported."""

    still_running: bool
    raised_with: tuple[Any, ...]
    events: list[MutableMapping[str, Any]]
    collected: bool
    reported: list[dict[str, Any]]


async def cancel_a_tick_once(service: BackupService, held: HeldCopy) -> CancelledTick:
    """Cancel a scheduled tick once while its copy is held, then collect the copy's task.

    asyncio reports an exception nobody retrieved -- "Task exception was never retrieved" --
    only when the task that holds it is collected, so the copy's task is collected while a
    handler that records reports is installed, and whether it was collected is part of the
    answer: without it, an empty `reported` would mean nothing. The loop holds the cancelled
    tick's traceback, and so the copy's task, until its next turn: hence the one turn first.
    """
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        with capture_logs() as captured:
            tick = asyncio.create_task(service.take_scheduled())
            await held.started()
            copy_task = the_copy_task(tick)
            still_running = await cancel_while_held(tick, held, ("stop",))
            raised_with = await cancellation_of(tick)
        del tick
        await asyncio.sleep(0)
        gc.collect()
        collected = copy_task() is None
    finally:
        loop.set_exception_handler(None)
    return CancelledTick(still_running, raised_with, captured, collected, reported)


async def test_a_copy_that_fails_while_cancelled_logs_its_failure_and_leaves_nothing_unread(
    live_database: Path, backup_directory: Path, held: HeldCopy
) -> None:
    """The failure is logged by the copy itself; the cancellation, not the error, propagates.

    And the error is marked retrieved: otherwise asyncio reports "Task exception was never
    retrieved" when the finished copy is collected, a second report of an error already logged.
    It is logged once: a `BackupError` is an outcome, not the defect R15 logs.
    """
    held.fail = True
    service = service_for(live_database, backup_directory)

    tick = await cancel_a_tick_once(service, held)

    assert tick.still_running is True
    assert tick.raised_with == ("stop",)
    assert tick.events == [
        {
            "event": "backup_failed",
            "error_kind": "storage_error",
            "error_type": "BackupError",
            "log_level": "error",
        }
    ]
    assert tick.collected is True
    assert tick.reported == []
    assert copy_names(backup_directory) == set()
    assert service.last_attempt is None


async def test_a_defect_that_ends_a_copy_during_a_cancellation_is_logged_before_it_goes_through(
    live_database: Path, backup_directory: Path, held: HeldCopy
) -> None:
    """R15: without a cancellation a defect reaches the timer, which logs it; with one, the
    re-raised cancellation would hide it from everyone, so the service logs it, with its type
    and its traceback, before the cancellation goes through. It is retrieved, too."""
    held.defect = RuntimeError("a bug in the copy, not a backup outcome")
    service = service_for(live_database, backup_directory)

    tick = await cancel_a_tick_once(service, held)

    assert tick.still_running is True
    assert tick.raised_with == ("stop",)
    (logged,) = tick.events
    assert logged == {
        "event": "backup_defect_during_cancellation",
        "error_type": "RuntimeError",
        "exc_info": held.defect,
        "log_level": "error",
    }
    assert tick.collected is True
    assert tick.reported == []
    assert copy_names(backup_directory) == set()
    assert service.last_attempt is None


async def test_a_defect_without_a_cancellation_is_the_callers_to_log(
    live_database: Path, backup_directory: Path, held: HeldCopy
) -> None:
    """The defect line is for a cancellation only: otherwise the defect propagates, and the
    timer logs it as `scheduler_tick_failed`. Logging it here too would log it twice."""
    held.defect = RuntimeError("a bug in the copy, not a backup outcome")
    held.go.set()
    service = service_for(live_database, backup_directory)

    with capture_logs() as captured, pytest.raises(RuntimeError) as caught:
        await service.take_scheduled()

    assert caught.value is held.defect
    assert captured == []


async def test_a_copy_task_cancelled_itself_ends_the_tick_with_the_callers_cancellation(
    live_database: Path, backup_directory: Path, held: HeldCopy
) -> None:
    """The copy's own task can be cancelled only from outside, as `asyncio.run` cancels every
    task left when it ends. That cancellation is the copy's end, not a defect, and the tick
    still ends with the cancellation it was given, logging nothing."""
    held.fail = True
    service = service_for(live_database, backup_directory)

    with capture_logs() as captured:
        tick = asyncio.create_task(service.take_scheduled())
        await held.started()
        copy_task = the_copy_task(tick)
        tick.cancel("stop")
        await asyncio.sleep(0)
        cancelled = copy_task()
        assert cancelled is not None
        cancelled.cancel("swept")
        del cancelled
        raised_with = await cancellation_of(tick)
        held.go.set()
        await held.ended()

    assert raised_with == ("stop",)
    assert captured == []
    assert service.last_attempt is None


async def test_an_anyio_cancellation_returns_the_copy_and_lands_at_the_next_checkpoint(
    live_database: Path, backup_directory: Path, held: HeldCopy
) -> None:
    """A task group's cancellation is held off by the shield, so the copy is returned to the
    caller, and the cancellation is delivered at the caller's next checkpoint, not lost."""
    service = service_for(live_database, backup_directory)
    returned: list[str] = []
    after_checkpoint: list[bool] = []

    async def take() -> None:
        returned.append((await service.take()).name)
        await anyio.lowlevel.checkpoint()
        after_checkpoint.append(True)

    with capture_logs() as captured:
        async with anyio.create_task_group() as group:
            group.start_soon(take)
            await held.started()
            group.cancel_scope.cancel()
            held.go.set()

    assert returned == [NAME]
    assert after_checkpoint == []
    assert [entry["event"] for entry in captured] == ["backup_completed"]


async def test_last_run_at_is_the_newest_copy_and_none_without_one(
    backup_directory: Path,
) -> None:
    service = service_for(IN_MEMORY, backup_directory)
    assert await service.last_run_at() is None

    plant_copies(backup_directory, [T0 - timedelta(days=2), T0 - timedelta(hours=5)])

    assert await service.last_run_at() == T0 - timedelta(hours=5)


async def test_last_run_at_never_raises_for_a_directory_that_cannot_be_listed(
    backup_directory: Path,
) -> None:
    """R4: `None`, so the timer attempts a copy at once and records how it went."""
    block(backup_directory)

    assert await service_for(IN_MEMORY, backup_directory).last_run_at() is None


async def test_list_backups_is_the_directory_newest_first(backup_directory: Path) -> None:
    plant_copies(backup_directory, [T0 - timedelta(days=2), T0, T0 - timedelta(days=1)])

    listed = await service_for(IN_MEMORY, backup_directory).list_backups()

    assert listed == list_copies(backup_directory)
    assert [each.started_at for each in listed] == [
        T0,
        T0 - timedelta(days=1),
        T0 - timedelta(days=2),
    ]


async def test_a_listing_failure_is_raised_by_list_backups(backup_directory: Path) -> None:
    block(backup_directory)

    with pytest.raises(BackupError) as caught:
        await service_for(IN_MEMORY, backup_directory).list_backups()

    assert caught.value.error_kind is BackupErrorKind.STORAGE_ERROR


# --------------------------------------------------------------------------------------
# Restoring through the service
# --------------------------------------------------------------------------------------


async def test_restore_uses_the_configured_database_and_names_the_safety_copy_by_the_clock(
    live_database: Path, backup_directory: Path
) -> None:
    add_notes(live_database, "in the copy")
    taken = await service_for(live_database, backup_directory).take()
    add_notes(live_database, "after the copy")
    later = T0 + timedelta(hours=2)

    result = await service_for(live_database, backup_directory, clock=fixed(later)).restore(
        taken.name
    )

    assert result.restored == taken.name
    assert result.safety_copy == backup_name(later)
    assert table_contents(live_database) == table_contents(backup_directory / taken.name)
    assert sidecars_of(live_database) == []


async def test_a_restore_takes_no_safety_copy_through_rotation(
    live_database: Path, backup_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safety copy is `take_copy`'s, not `take`'s: a restore deletes no older copy."""
    taken = await service_for(live_database, backup_directory).take()
    old = plant_copies(backup_directory, [T0 - timedelta(days=90)], b"old")
    calls: list[str] = []
    real_remove = db_backup.remove_copies

    def recording(directory: Path, names: Any) -> tuple[str, ...]:
        calls.append("remove")
        return real_remove(directory, names)

    monkeypatch.setattr(service_module, "remove_copies", recording)

    await service_for(
        live_database,
        backup_directory,
        keep_daily=1,
        keep_weekly=0,
        clock=fixed(T0 + timedelta(days=2)),
    ).restore(taken.name)

    assert calls == []
    assert set(old) <= copy_names(backup_directory)


async def test_the_utc_clock_is_the_default(live_database: Path, backup_directory: Path) -> None:
    """Built without a clock, a service names copies from the real UTC time."""
    service = BackupService(
        database_url=sqlite_url(live_database),
        directory=backup_directory,
        enabled=True,
        interval_minutes=DAY,
        keep_daily=7,
        keep_weekly=4,
    )
    before = datetime.now(UTC)

    taken = await service.take()

    started = instant_of(taken.name)
    assert started is not None
    assert before <= started <= datetime.now(UTC)
