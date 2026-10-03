"""Scheduled copies of the database: take one, list them, restore one, say how they stand.

The file work is `db/backup.py`'s and the rotation rule is `domain/backups.py`'s. This module
runs the first in a worker thread, applies the second after every copy that succeeded, logs
the outcome, and keeps the one piece of state that is not a file: what the timer's most
recent attempt in this process did (spec 029).

## Where the state lives

**Successful copies are the files.** The newest file's instant is the last success, and it
survives a restart, so the timer's "last run" is read from the directory and a restart does
not take an extra copy.

**A failed attempt is held in memory**: its instant and its `error_kind`, recorded by
`take_scheduled`, which only the timer calls. A restart clears it, as it clears
`last_recompute` (spec 021). After a restart a backup that keeps failing shows as `stale` once
the newest copy is two intervals old, and as `failed` again after the restarted timer's first
attempt. A copy taken by hand -- `python -m portfolio backup`, another process -- records
nothing here.

**A directory that cannot be listed is a state of its own, not an error** (ruling R4 of spec
029). `status` answers `unreadable`, with the newest copy and the count unknown -- `None`, not
zero -- rather than raising, which served a 500 and left the dashboard silent. The timer's
`last_run_at` answers `None` for the same directory, so the timer attempts a copy at once and
records how it went. The scheduler's own rule for a question it cannot answer is to wait a
whole interval, which protects a public API from a process that is already unhealthy; a copy
to a local disk hammers nobody, and a day of silence about backups that cannot be kept is the
worse outcome here.

## The state, first rule that applies

1. `unreadable` -- the backup directory cannot be listed now.
2. `disabled` -- `PORTFOLIO_BACKUP_ENABLED` is false.
3. `failed` -- this process's most recent attempt failed.
4. `stale` -- the newest copy is older than two intervals; or there is no copy, and an
   attempt has finished since startup.
5. `pending` -- there is no copy, and no attempt has finished yet.
6. `ok` -- otherwise, which includes a copy younger than two intervals before this process
   has attempted anything.

**How rule 3 is read**, because the spec's sentence admits two readings: "older than two
intervals" is stale whether or not this process has attempted anything, so a backup that has
been failing across restarts shows up before the restarted timer's first attempt finishes;
the condition "an attempt has finished" belongs to the no-copy half only. That half can only
follow an attempt that finished -- in practice one that succeeded and whose file was then
removed by hand -- so a fresh installation shows `pending` for the seconds its first copy
takes, and not a warning. Two intervals rather than one, because the copy one interval old is
the one the next tick is about to replace.

## What is logged

`backup_completed` with `name`, `bytes`, `duration_ms` and `deleted` (how many older copies
rotation removed), and `backup_failed` with `error_kind` and `error_type` -- the class name of
the error at the bottom of the chain, such as `OperationalError` or `PermissionError`, or
`CheckFailedError` for a copy that failed its check. When the copy was kept and the rotation
after it failed, `backup_failed` also carries `kept`, the copy's name (R7): the attempt
failed, but it left a copy, and it may have deleted some of the older ones before it
stopped. `backup_defect_during_cancellation`, with `error_type` and the traceback, is a
copy that ended in something other than a `BackupError` -- a defect -- while its task was
being cancelled: without cancellation that error reaches the timer, which logs it as
`scheduler_tick_failed`, and with one it would otherwise reach nobody (R15). **Never a
message** beyond the traceback's, and never a row: a message names paths, which are harmless,
but the rule is the one every log line here follows, and the operator's command prints the
message instead. A restore logs nothing; its row counts go to the operator's terminal and
nowhere else.

## A copy in flight is finished, even when its task is cancelled

The scheduler stops its task with `asyncio.Task.cancel()`. anyio's `to_thread.run_sync` holds
off only *anyio*'s cancellation; a native one raises in the awaiting task at once while the
worker thread runs on, so the lifespan went on to dispose of the engine with a copy still
being written, and the copy's outcome was never logged (ruling R9 of spec 029, measured by
`tester-22`). So `_take` runs the copy and its log line as a task of their own, shielded from
the caller's cancellation, waits for that task to end however many times the caller is
cancelled meanwhile, and only then re-raises. Stopping the timer therefore returns after the
copy has finished and said so, which is the first term of the shutdown arithmetic in
`main.drain_coordinators`. An anyio cancellation is held off by a shielded `CancelScope` and
delivered at the caller's next checkpoint, as anyio does for any shielded work.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from time import monotonic_ns
from typing import TYPE_CHECKING, Any, Final

import structlog
from anyio import CancelScope, to_thread

from portfolio.db.backup import (
    BackupError,
    BackupFile,
    RestoreRefusal,
    RestoreRefusedError,
    RestoreResult,
    database_path,
    list_copies,
    remove_copies,
    restore_copy,
    take_copy,
)
from portfolio.domain.backups import BackupErrorKind, backups_to_keep
from portfolio.services.scheduler import SECONDS_PER_MINUTE, utc_now

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from datetime import datetime
    from pathlib import Path

    from portfolio.config import Settings

__all__ = [
    "STALE_AFTER_INTERVALS",
    "BackupAttempt",
    "BackupError",
    "BackupErrorKind",
    "BackupFile",
    "BackupResult",
    "BackupService",
    "BackupState",
    "BackupStatus",
    "RestoreRefusal",
    "RestoreRefusedError",
    "RestoreResult",
    "RotationFailedError",
    "build_backup_service",
]

_NANOSECONDS_PER_MILLISECOND: Final = 1_000_000
STALE_AFTER_INTERVALS: Final = 2
"""How many intervals old the newest copy may be before the state is `stale`."""

_logger = structlog.get_logger(__name__)


# The member is the wire form. The docstring is served in the OpenAPI document.
class BackupState(StrEnum):
    """How the scheduled copies of the database stand. The first that applies:

    * `unreadable` -- the backup directory cannot be listed, so the newest copy and the count
      are unknown.
    * `disabled` -- the backup timer is switched off.
    * `failed` -- the timer's most recent attempt since the application started failed.
    * `stale` -- the newest copy is older than two intervals; or there is no copy although an
      attempt has finished.
    * `pending` -- there is no copy yet, and no attempt has finished.
    * `ok` -- otherwise, including before the first attempt when the newest copy is recent.
    """

    UNREADABLE = "unreadable"
    DISABLED = "disabled"
    FAILED = "failed"
    STALE = "stale"
    PENDING = "pending"
    OK = "ok"


@dataclass(frozen=True, slots=True)
class BackupResult:
    """A copy taken and kept: its name, its size, how long the copy and the rotation took, and
    the names of the older copies rotation deleted."""

    name: str
    size_bytes: int
    duration_ms: int
    deleted: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BackupAttempt:
    """The timer's most recent attempt in this process.

    `at` is when it started, which for a success is the instant the copy is named after.
    `failed` is whether it failed; `error_kind` is the kind when it failed with a
    `BackupError`, and `None` after a success or after any other exception -- a defect, which
    the scheduler logs with its traceback.
    """

    at: datetime
    failed: bool
    error_kind: BackupErrorKind | None


@dataclass(frozen=True, slots=True)
class BackupStatus:
    """What `GET /api/health/detail` serves as `backup`. No configuration value is in it.

    `latest_at` and `count` are `None` when the state is `unreadable`: unknown, not zero.
    """

    state: BackupState
    latest_at: datetime | None
    count: int | None
    last_attempt_at: datetime | None
    last_error_kind: BackupErrorKind | None


class RotationFailedError(BackupError):
    """The copy was taken, checked and kept, and the rotation after it failed (R7).

    A `BackupError` like any other -- the attempt failed, and the timer records it so -- that
    also names the copy it kept, as `kept`, because the copy exists and a reader of the log
    should not conclude otherwise. Rotation stops at the first file it cannot delete, so some
    older copies may be gone. Copies and pickles, like its parent.
    """

    def __init__(self, kept: str, error_kind: BackupErrorKind, message: str) -> None:
        """Keep the copy's name beside the kind and the message."""
        super().__init__(error_kind, message)
        self.kept = kept

    def __reduce__(self) -> tuple[Callable[..., RotationFailedError], tuple[object, ...]]:
        """Rebuild with all three arguments."""
        return (type(self), (self.kept, self.error_kind, str(self)))


class BackupService:
    """Takes, lists and restores copies, and keeps the timer's last attempt.

    One per application, built by `create_app` and published on `app.state`, because the last
    attempt is process-wide state; the command line builds its own. Nothing is read from the
    file system when it is built: the database URL is turned into a path when a copy is taken,
    so an in-memory URL fails then, as `database_error`, and not at startup.
    """

    def __init__(
        self,
        *,
        database_url: str,
        directory: Path,
        enabled: bool,
        interval_minutes: int,
        keep_daily: int,
        keep_weekly: int,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Hold the settings. `clock` names copies and judges staleness; a test replaces it."""
        self._database_url = database_url
        self._directory = directory
        self._enabled = enabled
        self._interval = timedelta(seconds=interval_minutes * SECONDS_PER_MINUTE)
        self._keep_daily = keep_daily
        self._keep_weekly = keep_weekly
        self._clock = clock
        self._last_attempt: BackupAttempt | None = None

    @property
    def directory(self) -> Path:
        """Where copies are written. For the command line's messages; never served."""
        return self._directory

    @property
    def last_attempt(self) -> BackupAttempt | None:
        """The timer's most recent attempt in this process, or `None` before the first."""
        return self._last_attempt

    async def take(self) -> BackupResult:
        """Take a copy now, then rotate. Logs `backup_completed` or `backup_failed`.

        Rotation runs only after the copy succeeded, so a failing backup never deletes
        anything; and the copy just taken is never among the deleted, even if the clock was
        stepped back behind an older copy's name.

        Raises:
            BackupError: the copy failed, and nothing was kept or deleted.
            RotationFailedError: the copy was kept, and the rotation after it failed, possibly
                after deleting some older copies. Its `kept` names the copy.
        """
        return await self._take(self._clock())

    async def take_scheduled(self) -> None:
        """The timer's tick: take a copy and record how it went, for `status`.

        A `BackupError` is recorded and swallowed -- it is already logged, and the loop goes
        on. Any other exception is recorded as a failure with no kind and re-raised, so the
        scheduler logs it with its traceback: it is a defect, not a backup outcome. A
        cancellation lets a copy in flight finish and log its outcome, then propagates and
        records nothing: the process is stopping.
        """
        started_at = self._clock()
        try:
            await self._take(started_at)
        except BackupError as exc:
            self._last_attempt = BackupAttempt(
                at=started_at, failed=True, error_kind=exc.error_kind
            )
            return
        except Exception:
            self._last_attempt = BackupAttempt(at=started_at, failed=True, error_kind=None)
            raise
        self._last_attempt = BackupAttempt(at=started_at, failed=False, error_kind=None)

    async def list_backups(self) -> tuple[BackupFile, ...]:
        """Every copy in the directory, newest first. None when the directory does not exist.

        Raises:
            BackupError: `storage_error`, when the directory cannot be read.
        """
        return await to_thread.run_sync(list_copies, self._directory)

    async def last_run_at(self) -> datetime | None:
        """The timer's "last run": the newest copy's instant. **Never raises.**

        `None` when there is no copy, and also when the directory cannot be listed (R4): the
        timer then attempts a copy at once, which fails or succeeds on its own terms and is
        recorded either way, rather than waiting a whole interval having recorded nothing.
        """
        try:
            copies = await self.list_backups()
        except BackupError:
            return None
        return copies[0].started_at if copies else None

    async def restore(self, name: str) -> RestoreResult:
        """Restore the copy `name` over the live database. See `db.backup.restore_copy`.

        The application must be stopped: the restore refuses while the database is open.

        Raises:
            RestoreRefusedError: refused before anything was written.
            BackupError: the copy, the safety copy or the live database failed.
        """
        return await to_thread.run_sync(self._restore, name)

    async def status(self, now: datetime | None = None) -> BackupStatus:
        """How the copies stand at `now` (the service's clock if omitted). **Never raises.**

        A directory that cannot be listed is the state `unreadable`, with `latest_at` and
        `count` unknown (R4). Nothing is logged for it here, because this runs on every
        request for the state; the timer's next attempt logs the failure it meets.
        """
        at = self._clock() if now is None else now
        attempt = self._last_attempt
        last_attempt_at = None if attempt is None else attempt.at
        last_error_kind = None if attempt is None else attempt.error_kind
        try:
            copies = await self.list_backups()
        except BackupError:
            return BackupStatus(
                state=BackupState.UNREADABLE,
                latest_at=None,
                count=None,
                last_attempt_at=last_attempt_at,
                last_error_kind=last_error_kind,
            )
        latest = copies[0].started_at if copies else None
        return BackupStatus(
            state=self._state(at, latest, attempt),
            latest_at=latest,
            count=len(copies),
            last_attempt_at=last_attempt_at,
            last_error_kind=last_error_kind,
        )

    def _state(
        self,
        now: datetime,
        latest: datetime | None,
        attempt: BackupAttempt | None,
    ) -> BackupState:
        """Rules 2 to 6 of the module docstring's list; `status` has already applied rule 1."""
        if not self._enabled:
            return BackupState.DISABLED
        if attempt is not None and attempt.failed:
            return BackupState.FAILED
        if latest is None:
            return BackupState.PENDING if attempt is None else BackupState.STALE
        if now - latest > self._interval * STALE_AFTER_INTERVALS:
            return BackupState.STALE
        return BackupState.OK

    async def _take(self, started_at: datetime) -> BackupResult:
        """`_take_and_log`, finished before a cancellation of the caller is let through (R9)."""
        return await _finish_before_cancelling(self._take_and_log(started_at))

    async def _take_and_log(self, started_at: datetime) -> BackupResult:
        """Copy and rotate in a worker thread, timed, and log the outcome."""
        started_ns = monotonic_ns()
        try:
            copy, deleted = await to_thread.run_sync(self._copy_and_rotate, started_at)
        except BackupError as exc:
            kept = {"kept": exc.kept} if isinstance(exc, RotationFailedError) else {}
            _logger.error(
                "backup_failed",
                error_kind=exc.error_kind.value,
                error_type=type(_root_cause(exc)).__name__,
                **kept,
            )
            raise
        duration_ms = (monotonic_ns() - started_ns) // _NANOSECONDS_PER_MILLISECOND
        _logger.info(
            "backup_completed",
            name=copy.name,
            bytes=copy.size_bytes,
            duration_ms=duration_ms,
            deleted=len(deleted),
        )
        return BackupResult(
            name=copy.name,
            size_bytes=copy.size_bytes,
            duration_ms=duration_ms,
            deleted=deleted,
        )

    def _copy_and_rotate(self, started_at: datetime) -> tuple[BackupFile, tuple[str, ...]]:
        """Take the copy, then delete every copy rotation does not keep. Synchronous.

        Raises:
            BackupError: the copy failed, and nothing was kept or deleted.
            RotationFailedError: the copy was kept, and listing or deleting the older ones
                failed after it.
        """
        database = database_path(self._database_url)
        copy = take_copy(database, self._directory, started_at)
        try:
            deleted = self._rotate(copy)
        except BackupError as exc:
            message = (
                f"The backup {copy.name} was taken and kept, but the rotation after it failed, "
                f"and may have deleted only some of the older backups it meant to: {exc}"
            )
            raise RotationFailedError(copy.name, exc.error_kind, message) from exc
        return copy, deleted

    def _rotate(self, copy: BackupFile) -> tuple[str, ...]:
        """Delete every copy `backups_to_keep` does not keep, never `copy` itself."""
        copies = list_copies(self._directory)
        keep = backups_to_keep(
            (each.started_at for each in copies),
            keep_daily=self._keep_daily,
            keep_weekly=self._keep_weekly,
        )
        doomed = [
            each.name for each in copies if each.started_at not in keep and each.name != copy.name
        ]
        return remove_copies(self._directory, doomed)

    def _restore(self, name: str) -> RestoreResult:
        """`restore_copy` over the configured database and directory. Synchronous."""
        database = database_path(self._database_url)
        return restore_copy(database, self._directory, name, clock=self._clock)


async def _finish_before_cancelling[T](work: Coroutine[Any, Any, T]) -> T:
    """Run `work` to its end even if the calling task is cancelled meanwhile; then re-raise.

    `work` becomes a task of its own, so a cancellation of the caller does not reach it. The
    caller waits for it in a loop, because a cancellation can arrive more than once, and in a
    shielded anyio `CancelScope`, so that anyio does not keep re-delivering its own. When the
    caller was cancelled, the first `CancelledError` is re-raised once `work` has ended.
    `work`'s exception, if it had one, is retrieved first: a `BackupError` has already been
    logged by `work` itself, and anything else is a defect that the re-raised cancellation
    would hide, so it is logged here as `backup_defect_during_cancellation`, with its type and
    traceback (ruling R15 of spec 029). Otherwise `work`'s result or exception is the
    caller's.

    Measured on 2026-10-02 (anyio 4.15, CPython 3.12) with a worker thread held for 0.3 s: one
    native cancellation and three in a row each waited for the thread and the log line, then
    ended the task as cancelled; a failing thread logged its failure first; and an anyio task
    group's cancellation returned the result and was delivered at the next checkpoint.
    """
    task = asyncio.ensure_future(work)
    cancellation: asyncio.CancelledError | None = None
    with CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
    if cancellation is not None:
        defect = None if task.cancelled() else task.exception()
        if defect is not None and not isinstance(defect, BackupError):
            _logger.error(
                "backup_defect_during_cancellation",
                error_type=type(defect).__name__,
                exc_info=defect,
            )
        raise cancellation
    return task.result()


def _root_cause(exc: BaseException) -> BaseException:
    """The error at the bottom of `exc`'s `__cause__` chain, or `exc` itself without one.

    What `backup_failed` names as `error_type`: the `OSError` under a rotation failure under
    a `BackupError`, rather than the wrapper the service or `db` raised around it.
    """
    while exc.__cause__ is not None:
        exc = exc.__cause__
    return exc


def build_backup_service(
    settings: Settings,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> BackupService:
    """The service the application and the command line use, from the same settings.

    The directory is made absolute here, against the working directory, so that every
    message names the same place whatever the process does to its working directory later.
    """
    return BackupService(
        database_url=settings.database_url,
        directory=settings.backup_dir.expanduser().absolute(),
        enabled=settings.backup_enabled,
        interval_minutes=settings.backup_interval_minutes,
        keep_daily=settings.backup_keep_daily,
        keep_weekly=settings.backup_keep_weekly,
        clock=clock,
    )
