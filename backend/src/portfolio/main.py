"""Application factory.

Run with `uvicorn portfolio.main:app`.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final, Protocol

import structlog
from anyio import to_thread
from fastapi import FastAPI

from portfolio import __version__
from portfolio.api.dependencies import auth_service_for, install_auth_runtime
from portfolio.api.errors import register_exception_handlers
from portfolio.api.middleware import API_PREFIX, RequestGuardMiddleware, is_api_path
from portfolio.api.request_context import RequestContextMiddleware, documentation_paths
from portfolio.api.routers import (
    accounting,
    adjustments,
    auth,
    balances,
    exchanges,
    health,
    wallets,
)
from portfolio.config import get_settings
from portfolio.db.alembic_config import upgrade_to_head
from portfolio.db.engine import (
    create_database_engine,
    create_session_factory,
    ensure_database_directory,
)
from portfolio.logging import configure_logging

# Imported for its side effect: each module in `providers.chains` registers its provider
# class by decorator, and a decorator only runs when its module is imported. The registry
# deliberately does not discover them -- see `providers/registry.py` -- so this is the one
# line that makes `get_chain_provider` able to answer for any chain at all.
from portfolio.providers import chains as _registered_chain_providers  # noqa: F401
from portfolio.providers.exchanges.registry import exchange_providers
from portfolio.providers.http import build_http_client, monotonic_ms
from portfolio.providers.prices.registry import price_sources
from portfolio.providers.registry import get_chain_provider
from portfolio.repositories.exchange_sync_runs import ExchangeSyncRunRepository
from portfolio.repositories.prices import PriceRepository
from portfolio.repositories.sync_runs import SyncRunRepository
from portfolio.repositories.users import UserRepository
from portfolio.services.accounting import (
    AccountingStatus,
    RecomputeOutcome,
    RecomputeReason,
    RecomputeReport,
    UnconvertibleAdjustmentError,
    build_accounting_service,
    utc_now,
)
from portfolio.services.backup import BackupService, build_backup_service
from portfolio.services.balance_sync import build_balance_sync_service
from portfolio.services.exchange_sync import build_exchange_sync_service
from portfolio.services.price_refresh import build_price_refresh_service
from portfolio.services.scheduler import IntervalScheduler
from portfolio.services.sync_coordinator import SyncCoordinator, SyncTrigger
from portfolio.web.spa import mount_spa

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from datetime import datetime

    import httpx

    from portfolio.config import Settings
    from portfolio.domain.exchanges import ExchangeKey
    from portfolio.providers.exchanges.base import ExchangeProvider
    from portfolio.repositories.exchange_sync_runs import ExchangeSyncRunSummary
    from portfolio.repositories.sync_runs import SyncRunSummary
    from portfolio.services.password_hasher import PasswordHasher
    from portfolio.services.price_refresh import RefreshReport
    from portfolio.services.sync_coordinator import SyncRunner

EXCHANGE_SYNC_TASK_NAME: Final = "exchange-sync"
"""The exchange run's task name, and the exchange timer's name in every log line."""

EXCHANGE_SYNC_LOG_PREFIX: Final = "exchange_sync"
"""What every event the exchange coordinator logs begins with."""

ACCOUNTING_STARTUP_TASK_NAME: Final = "accounting-startup-recompute"
"""The startup recompute's task name, as `app.state.accounting_startup_task` carries it."""

BACKUP_TASK_NAME: Final = "backup"
"""The backup timer's name in every log line the scheduler writes, and in its task's name."""

_logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Bring the database up to date, then own everything with a lifetime for the process.

    The migration runs in a worker thread because Alembic's async `env.py` calls
    `asyncio.run`, which raises `RuntimeError` when a loop is already running in the
    calling thread -- and by the time a lifespan runs, one always is.

    Five things are owned here and all five are shut down here: the engine, the shared
    `httpx.AsyncClient`, the sync coordinator, and the two timers -- the balance sync and the
    price refresh. The client is the wiring #6 through #9 each deferred to this issue; it is
    process-wide *by construction*, because the per-host rate limiter's state lives on its
    transport, so a second one would silently halve the interval it claims to enforce.

    **The two timers are separate tasks on separate intervals with separate switches**, and
    that separation is the isolation: neither can stop the other, because they share nothing
    but a class. They answer to different vendors on different schedules -- chain indexes
    that ban you for asking too often, against market-data APIs where one call covers every
    configured pair -- and one switch for both would mean an operator waiting out a chain
    outage also stopped valuing the balances they already had.

    Everything is published on `app.state` rather than held in a module global so that two
    applications in one process -- which is exactly what the test suite builds -- do not
    share a pool, a client or a run in flight.

    **The orphan sweep runs before the scheduler starts and again after it stops.** A
    `sync_runs` row is written at `running` before the first provider call, so a process
    killed mid-sync leaves one behind; without the sweep a crashed run and a live run are the
    same row. Doing it at shutdown too is what records a run that outlived the grace period,
    and doing it there rather than inside the cancelled task is what makes it reliable.

    ## The exchange sync (#15)

    A third timer and a second coordinator, with the same shape and nothing shared:

    * **The provider mapping is built once, here**, by `exchange_providers`, the way
      `price_sources` is. It holds the credentials and it is never published: the runner
      closes over it, and `app.state` only learns `configured_exchanges`, the set of its keys.
    * **`exchange_sync_coordinator` is installed always**, so a manual sync works with the
      timer off. The timer is built only when `PORTFOLIO_EXCHANGE_SYNC_ENABLED` is true *and*
      at least one venue is configured: an install without credentials writes no empty run
      every fifteen minutes.
    * **Both run tables are swept at startup and at shutdown**, and the two coordinators are
      drained **concurrently**: the deployment's `stop_grace_period` is twenty seconds, and
      two ten-second grace periods one after the other would spend all of it.

    ## The accounting recompute (#19)

    `run_accounting_recompute` runs once at startup, as `app.state.accounting_startup_task`,
    **started after the migrations and the sweeps and never awaited here**: readiness -- and
    the deploy's health check -- does not wait on a replay of the owner's whole history. It is
    cancelled first thing on shutdown, before anything it could still be using is taken away.
    The startup run is what covers the first deploy over fills already stored, and an engine
    upgrade, whose new `ENGINE_VERSION` changes every fingerprint. The other two triggers are a
    sync that stored a fill, and a change to a manual adjustment (#18), which reaches the
    trigger through `app.state.accounting_recompute`.

    ## The backups (#22)

    A fourth timer, `backup_scheduler_for`, over the `BackupService` that `create_app`
    published as `app.state.backup_service`. It shares nothing with the other three, so a
    failing copy stops no sync and no sync stops a copy. Its "last run" is the newest copy's
    instant, read from the backup directory, so a restart takes no extra copy and a fresh
    volume gets one at startup. It is stopped **last of the timers** (ruling R15 of spec 029),
    and before the engine is disposed. A copy in flight finishes and logs its outcome first,
    because `BackupService` lets the timer's cancellation through only after the copy has ended
    (ruling R9), and the copy holds connections of its own rather than the engine's; the other
    timers are already stopped by then, so none of them starts a tick while it waits. What
    that costs the shutdown is counted in `drain_coordinators`.
    """
    settings = get_settings()
    ensure_database_directory(settings.database_url)
    await to_thread.run_sync(upgrade_to_head, settings.database_url)

    engine = create_database_engine(settings.database_url)
    app.state.db_engine = engine
    app.state.db_sessionmaker = create_session_factory(engine)
    client = build_http_client()
    app.state.http_client = client
    coordinator: SyncCoordinator[SyncRunSummary] | None = None
    exchange_coordinator: SyncCoordinator[ExchangeSyncRunSummary] | None = None
    schedulers: list[IntervalScheduler] = []
    startup_recompute: asyncio.Task[AccountingStatus] | None = None
    try:
        await bootstrap_owner(app, settings)
        await warm_password_hasher(app)
        await sweep_interrupted_runs(app)
        await sweep_interrupted_exchange_runs(app)
        startup_recompute = asyncio.create_task(
            run_accounting_recompute(app, RecomputeReason.STARTUP),
            name=ACCOUNTING_STARTUP_TASK_NAME,
        )
        app.state.accounting_startup_task = startup_recompute
        coordinator = SyncCoordinator(balance_sync_runner(app, client))
        app.state.sync_coordinator = coordinator
        app.state.balance_scheduler = balance_scheduler_for(app, settings, coordinator)
        app.state.price_scheduler = price_scheduler_for(app, settings, client)

        providers = exchange_providers(client, settings=settings)
        configured = frozenset(providers)
        app.state.configured_exchanges = configured
        exchange_coordinator = SyncCoordinator(
            exchange_sync_runner(app, providers, settings),
            task_name=EXCHANGE_SYNC_TASK_NAME,
            log_prefix=EXCHANGE_SYNC_LOG_PREFIX,
        )
        app.state.exchange_sync_coordinator = exchange_coordinator
        app.state.exchange_scheduler = exchange_scheduler_for(
            app, settings, exchange_coordinator, configured
        )
        app.state.backup_scheduler = backup_scheduler_for(settings, app.state.backup_service)
        # Four timers, four tasks, sharing nothing but a class. That is what makes "a failed
        # price refresh does not stop the balance sync" structural rather than a promise, and
        # the same for a venue refusing a key or a backup that cannot be written.
        schedulers = [
            timer
            for timer in (
                app.state.balance_scheduler,
                app.state.price_scheduler,
                app.state.exchange_scheduler,
                app.state.backup_scheduler,
            )
            if timer is not None
        ]
        for scheduler in schedulers:
            await scheduler.start()
        yield
    finally:
        # Ordered, and the order is the content. The startup recompute is cancelled and
        # awaited first: it is derived data, the next startup computes it again, and nothing
        # is lost by abandoning it. Every timer stops next so that no new tick can start, the
        # backup timer last: stopping it waits for a copy in flight, and a timer still running
        # meanwhile could start a sync that then needs its grace period too. The syncs already
        # in flight then get their grace periods, side by side; the sweeps record whatever did
        # not finish; and only then are the client and the engine taken away, because a sync
        # still running would need both.
        await cancel_and_wait(startup_recompute)
        others = [timer for timer in reversed(schedulers) if timer.name != BACKUP_TASK_NAME]
        backup = [timer for timer in schedulers if timer.name == BACKUP_TASK_NAME]
        for scheduler in (*others, *backup):
            await scheduler.stop()
        await drain_coordinators(
            (coordinator, settings.balance_sync_shutdown_grace_seconds),
            (exchange_coordinator, settings.exchange_sync_shutdown_grace_seconds),
        )
        await sweep_interrupted_runs(app)
        await sweep_interrupted_exchange_runs(app)
        await client.aclose()
        await engine.dispose()


class Drainable(Protocol):
    """What shutdown needs of a coordinator: wait for its run, cancel it past a grace period."""

    async def drain(self, *, grace_seconds: int) -> bool:
        """Wait up to `grace_seconds` for the run in flight; see `SyncCoordinator.drain`."""
        ...


async def drain_coordinators(*coordinators: tuple[Drainable | None, int]) -> None:
    """Give every coordinator's run in flight its grace period, **concurrently**.

    Concurrently because the deployment's `stop_grace_period` is twenty seconds: two
    ten-second grace periods in sequence would leave nothing for the sweeps and the engine
    before the container is killed. A coordinator that was never built is skipped.

    **The shutdown's arithmetic has a third term: a backup in flight.** The timers are stopped
    before this runs, the backup timer last, and stopping it waits for a copy already under
    way: `BackupService` holds the timer's cancellation until the copy has ended and logged
    its outcome, then lets it through (ruling R9 of spec 029; an anyio thread call alone would
    not, because a native task cancellation does not wait for the thread). So the worst case
    is the copy, *then* the longer of the two grace periods, then the sweeps and the disposal:
    the copy plus ten seconds plus well under one, against twenty. The owner's database copies
    in well under a second -- a 65 MB database took under 200 ms on a development machine --
    so the budget is not at risk. What would put it at risk is a copy that takes more than
    about nine seconds: a database grown to many gigabytes on slow storage, or a backup disk
    that stops answering and leaves the copy's write hanging. Docker then kills the container.
    The live database is safe even so -- a copy only reads it -- the unfinished copy's
    temporary file is removed by an attempt more than an hour later, and a sync cut short is
    recorded as interrupted by the next start's sweep. If the database ever grows that large,
    raise `stop_grace_period` in `deploy/compose.yml` rather than shortening a grace period.

    **Never raises**: `drain` already handles its own run's failure and cancellation, and
    anything else that escapes one drain is logged here, so that the other drain, both sweeps
    and the disposal still happen -- this runs in the lifespan's `finally`.
    """
    drains = [
        instance.drain(grace_seconds=max(0, grace))
        for instance, grace in coordinators
        if instance is not None
    ]
    results = await asyncio.gather(*drains, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            _logger.error(
                "sync_drain_failed",
                error_type=type(result).__name__,
                exc_info=result,
            )


def balance_sync_runner(app: FastAPI, client: httpx.AsyncClient) -> SyncRunner[SyncRunSummary]:
    """Build the closure the coordinator runs: a session per run, over the shared client.

    **A run must not share the session of whatever asked for it.** A manual sync is joined
    rather than refused, so a run outlives the request that started it, and a session closed
    by a request dependency on the way out would be pulled out from under the work. Opening
    one here means the run's unit of work begins and ends with the run.

    `provider_for` is a lambda over `get_chain_provider` rather than the registry itself,
    which is what keeps `httpx` out of `services/` while the sync is the one thing in this
    application that actually causes network traffic. An unregistered chain raises
    `UnknownChainError` out of that call, which the sync records against that chain alone.
    """

    async def run(trigger: SyncTrigger) -> SyncRunSummary:
        sessionmaker = app.state.db_sessionmaker
        async with sessionmaker() as session:
            service = build_balance_sync_service(
                session,
                provider_for=lambda chain_key: get_chain_provider(chain_key, client),
            )
            return await service.sync(trigger)

    return run


def balance_scheduler_for(
    app: FastAPI,
    settings: Settings,
    coordinator: SyncCoordinator[SyncRunSummary],
) -> IntervalScheduler | None:
    """Build the balance timer, or `None` when the operator has switched it off.

    `PORTFOLIO_BALANCE_SYNC_ENABLED=false` disables **the loop and nothing else**:
    `POST /api/balances/sync` still works, because the manual trigger is the tool an operator
    debugging a vendor is reaching for, and taking it away with the same switch would be the
    opposite of what the switch is for.

    The tick goes through the coordinator rather than straight to the sync, which is what
    makes criterion 6 hold from both directions: a tick arriving while a manual refresh is
    still in flight joins it instead of piling a second run on a public index.

    `at_startup` becomes the run's recorded `trigger`. That distinction is the whole reason
    `ScheduledRun` carries the flag -- `startup` is the run an operator is looking at when
    they ask whether the deploy worked.

    Returned rather than started, so that the caller's `finally` can be written against the
    same list it will have to stop.
    """
    if not settings.balance_sync_enabled:
        _logger.info("scheduler_disabled", scheduler="balance-sync")
        return None

    async def run(at_startup: bool) -> None:
        await coordinator.sync(SyncTrigger.STARTUP if at_startup else SyncTrigger.SCHEDULED)

    return IntervalScheduler(
        name="balance-sync",
        interval_minutes=settings.balance_sync_interval_minutes,
        last_run_at=lambda: latest_sync_attempt(app),
        run=run,
    )


def exchange_sync_runner(
    app: FastAPI,
    providers: Mapping[ExchangeKey, ExchangeProvider],
    settings: Settings,
) -> SyncRunner[ExchangeSyncRunSummary]:
    """Build the closure the exchange coordinator runs: a session per run, the providers built once.

    A session of its own per run, for the reason `balance_sync_runner` gives. `providers` is
    the mapping the lifespan built with `exchange_providers` -- **the only reference to the
    objects holding credentials**, and this closure is the only thing that keeps it. A request
    reaches it through the coordinator and no other way.

    **A run that stored a fill recomputes the accounting snapshot before it returns** (#19).
    After the sync's session is closed, so the recompute's write never waits on the sync's;
    and before the summary is handed back, so a manual sync's response means the dashboard is
    current. The recompute never raises, so the summary is the sync's own whatever became of
    it -- a failed recompute is logged and shown on `GET /api/accounting/positions`, not
    reported as a failed sync.

    **A run that stored nothing recomputes only if the last recompute failed** (spec 021, R7).
    Otherwise the fills it would replay are the ones already replayed, and the snapshot is left
    alone. After a failure they are not: a transient one -- "database is locked" while another
    write held SQLite's lock -- then clears at the next sync rather than waiting for the next
    stored fill or a restart. A failure caused by the data fails again, which costs one replay
    per sync and changes nothing.
    """

    async def run(trigger: SyncTrigger) -> ExchangeSyncRunSummary:
        sessionmaker = app.state.db_sessionmaker
        async with sessionmaker() as session:
            service = build_exchange_sync_service(
                session,
                providers=providers,
                history_start=settings.exchange_history_start,
            )
            summary = await service.sync(trigger)
        if summary.fills_inserted > 0 or last_recompute_failed(app):
            await run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC)
        return summary

    return run


def exchange_scheduler_for(
    app: FastAPI,
    settings: Settings,
    coordinator: SyncCoordinator[ExchangeSyncRunSummary],
    configured: frozenset[ExchangeKey],
) -> IntervalScheduler | None:
    """Build the exchange timer, or `None` when it is off or there is nothing to sync.

    Two conditions, both required:

    * **`PORTFOLIO_EXCHANGE_SYNC_ENABLED`**, an off switch for the loop only --
      `POST /api/exchanges/sync` keeps working, because the coordinator is installed anyway;
    * **at least one venue configured.** Without credentials there is nothing to ask, and a
      timer would write an empty run every interval forever.

    `last_run_at` is the newest exchange run's `started_at`, counting attempts, for the
    crash-loop reason `latest_sync_attempt` gives. `at_startup` becomes the recorded trigger,
    and a startup run skips an `auth_failed` account the same as a scheduled one.
    """
    if not settings.exchange_sync_enabled:
        _logger.info("scheduler_disabled", scheduler=EXCHANGE_SYNC_TASK_NAME, reason="disabled")
        return None
    if not configured:
        _logger.info(
            "scheduler_disabled",
            scheduler=EXCHANGE_SYNC_TASK_NAME,
            reason="no_exchange_configured",
        )
        return None

    async def run(at_startup: bool) -> None:
        await coordinator.sync(SyncTrigger.STARTUP if at_startup else SyncTrigger.SCHEDULED)

    return IntervalScheduler(
        name=EXCHANGE_SYNC_TASK_NAME,
        interval_minutes=settings.exchange_sync_interval_minutes,
        last_run_at=lambda: latest_exchange_sync_attempt(app),
        run=run,
    )


def backup_scheduler_for(settings: Settings, service: BackupService) -> IntervalScheduler | None:
    """Build the backup timer, or `None` when `PORTFOLIO_BACKUP_ENABLED` is false.

    The switch stops the timer and nothing else: `python -m portfolio backup`, `list-backups`
    and `restore-backup` build their own service and work either way, and the endpoint still
    serves the copies there are, as `disabled`.

    `last_run_at` is `BackupService.last_run_at`, the newest copy's instant, so the
    scheduler's first-run rule applies as written: a fresh volume, or a newest copy older than
    one interval, gets a copy at startup, and otherwise the timer sleeps what is left of the
    interval. It counts **successes**, unlike the balance timer's attempts, because a failed
    attempt leaves no file; a container that crash-loops while copies keep failing therefore
    tries once per restart. A copy is local work against a local disk rather than a request to
    someone else's API, so that costs nothing anybody bans you for.

    **It never raises** (ruling R4 of spec 029). A backup directory that cannot be listed
    answers `None`, so the timer attempts a copy at startup and records the failure, rather
    than taking the scheduler's "wait a whole interval" path for a question it could not ask
    -- a path that suits a public API and leaves a backup that cannot be kept unrecorded for a
    day. The rule is the service's, so `IntervalScheduler` keeps its general one.

    The tick is `take_scheduled`, which records the outcome for `GET /api/health/detail` and
    swallows a `BackupError` after logging it, so the loop goes on.
    """
    if not settings.backup_enabled:
        _logger.info("scheduler_disabled", scheduler=BACKUP_TASK_NAME)
        return None

    async def run(at_startup: bool) -> None:
        del at_startup  # A copy is the same work whenever it is taken.
        await service.take_scheduled()

    return IntervalScheduler(
        name=BACKUP_TASK_NAME,
        interval_minutes=settings.backup_interval_minutes,
        last_run_at=service.last_run_at,
        run=run,
    )


def install_accounting_runtime(app: FastAPI) -> None:
    """Publish the recompute lock, "no attempt yet", and the trigger, on the application state.

    From the application factory rather than the lifespan, for the reason
    `install_auth_runtime` gives: the objects exist for a test that never starts the lifespan,
    and two applications in one process never share a lock. `asyncio.Lock` binds to an event
    loop only on its first contended acquire, so building it here, outside any loop, is safe.

    **`accounting_recompute` is `run_accounting_recompute` bound to this application** (#18), a
    `(RecomputeReason) -> Awaitable[AccountingStatus]`. A change to a manual adjustment has to
    recompute before its response, and the request path cannot import this module -- it imports
    every router -- so the trigger is published here and `api.dependencies` reads it back. It is
    the same function the startup run and the exchange sync call, so it takes the same lock and
    records to the same `accounting_status`, and it never raises.
    """
    app.state.accounting_lock = asyncio.Lock()
    app.state.accounting_status = None

    async def accounting_recompute(reason: RecomputeReason) -> AccountingStatus:
        return await run_accounting_recompute(app, reason)

    app.state.accounting_recompute = accounting_recompute


async def run_accounting_recompute(app: FastAPI, reason: RecomputeReason) -> AccountingStatus:
    """Recompute every owner's snapshot, log what happened, and record it. **Never raises.**

    * **Serialised** by `app.state.accounting_lock`: the startup run, a sync's run and a
      request's run after an adjustment changed (#18) can overlap in time, and two
      replacements of one snapshot interleaving their writes would be a snapshot neither
      computed.
    * **Its own session**, never a request's or a sync's, for the reason `balance_sync_runner`
      gives. Every owner is recomputed in turn -- there is one today, and the loop is the
      honest shape for "every owner" -- each in its own transaction, so one owner's failure
      neither stops nor undoes another's.
    * **Logged once per run**: `accounting_recompute_finished` with the reason, the duration,
      the events replayed and the outcome (`written` if any owner's snapshot was written); or
      `accounting_recompute_failed` with the reason, the duration and **the first failure's
      class name only** -- plus, for an `UnconvertibleAdjustmentError`, the adjustment's id,
      which is what an operator needs to correct it. Never its message and never a traceback.
      The engine is built with `hide_parameters=True`, so a `StatementError` does not carry the
      values it was binding; but its text still quotes the statement, and a message is free
      text that no exception has promised to keep clear of a trade id or an amount. The class
      name is enough to act on, and `docs/operations.md` says what each one means.
    * **Recorded** on `app.state.accounting_status`, which `GET /api/accounting/positions`
      shows as `last_recompute`.

    "Never raises" means no `Exception` escapes: whatever the recompute raised is the outcome
    `failed`, and the snapshot stored before stays in place. A cancellation is not an outcome
    and propagates, which is what lets shutdown stop the startup run.

    Returns:
        What was recorded.
    """
    lock: asyncio.Lock = app.state.accounting_lock
    async with lock:
        started_ms = monotonic_ms()
        failure: Exception | None = None
        reports: list[RecomputeReport] = []
        try:
            sessionmaker = app.state.db_sessionmaker
            async with sessionmaker() as session:
                owner_ids = [user.id for user in await UserRepository(session).list_all()]
                service = build_accounting_service(session)
                for owner_id in owner_ids:
                    try:
                        reports.append(await service.recompute(owner_id))
                    except Exception as exc:  # recorded as the outcome below
                        failure = failure or exc
        except Exception as exc:  # recorded as the outcome below
            failure = failure or exc
        duration_ms = monotonic_ms() - started_ms
        if failure is not None:
            status = AccountingStatus(
                at=utc_now(),
                outcome=RecomputeOutcome.FAILED,
                error=type(failure).__name__,
            )
            _logger.error(
                "accounting_recompute_failed",
                reason=reason.value,
                duration_ms=duration_ms,
                error=status.error,
                **_failed_row_of(failure),
            )
        else:
            outcome = (
                RecomputeOutcome.WRITTEN
                if any(report.outcome is RecomputeOutcome.WRITTEN for report in reports)
                else RecomputeOutcome.UNCHANGED
            )
            status = AccountingStatus(at=utc_now(), outcome=outcome, error=None)
            _logger.info(
                "accounting_recompute_finished",
                reason=reason.value,
                duration_ms=duration_ms,
                event_count=sum(report.event_count for report in reports),
                outcome=outcome.value,
            )
        app.state.accounting_status = status
        return status


def _failed_row_of(failure: Exception) -> dict[str, int]:
    """The log fields that name the row a failed recompute stopped on, when naming it is safe.

    **Only an adjustment's id** (spec 023, R8). `docs/operations.md` tells an operator to
    correct or delete the adjustment an `UnconvertibleAdjustmentError` stopped on, which is
    only possible knowing which one; and an adjustment id is already logged on every change to
    one. A fill's identity -- its account and its venue's trade id -- stays out of the log, as
    it always has: a trade id is a venue's record of the owner's trading.
    """
    if isinstance(failure, UnconvertibleAdjustmentError):
        return {"adjustment_id": failure.adjustment_id}
    return {}


def last_recompute_failed(app: FastAPI) -> bool:
    """Whether the last recompute attempt recorded on the application failed (spec 021, R7).

    `False` before the first attempt, and for anything on `app.state.accounting_status` that
    is not an `AccountingStatus` -- the reading `get_accounting_status` gives it too.
    """
    status = getattr(app.state, "accounting_status", None)
    return isinstance(status, AccountingStatus) and status.outcome is RecomputeOutcome.FAILED


async def cancel_and_wait(task: asyncio.Task[AccountingStatus] | None) -> None:
    """Cancel a task and wait until it has actually stopped. A finished one is left alone.

    `asyncio.wait` rather than `await task`, so the `CancelledError` does not need suppressing
    here -- the pattern `IntervalScheduler.stop` uses.
    """
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.wait({task})


def price_scheduler_for(
    app: FastAPI,
    settings: Settings,
    client: httpx.AsyncClient,
) -> IntervalScheduler | None:
    """Build the price timer, or `None` when the operator has switched it off.

    **This is the caller `services/price_refresh.py` was written without.** #9 shipped
    `refresh_prices()` with no caller in the running application so the call budget could be
    measured by hand first, and `docs/providers.md` recorded the scheduling as #10's. Without
    it a deployed instance reads balances every fifteen minutes and reports every one of them
    `unpriced / never_fetched` forever, which is the flagship endpoint of the dashboard
    returning a zero total on a working install.

    `price_sources` is called **here, in the composition root**, and the built sources are
    handed to the service -- the same shape `cli.run_price_refresh` uses, and what keeps
    `services/price_refresh.py` dependent on the `PriceSource` protocol rather than on which
    vendors exist. Built once rather than per tick, because which vendors exist is a
    process-wide decision like the client itself.

    Its own switch and its own interval rather than sharing the balance pair: the two answer
    to different vendors, and an operator waiting out a chain outage should not also stop
    valuing the balances they already have.
    """
    if not settings.price_refresh_enabled:
        _logger.info("scheduler_disabled", scheduler="price-refresh")
        return None
    sources = price_sources(client, settings=settings)

    async def run(at_startup: bool) -> None:
        del at_startup  # A refresh is the same work whenever it happens; nothing records it.
        sessionmaker = app.state.db_sessionmaker
        async with sessionmaker() as session:
            service = build_price_refresh_service(session, sources=sources)
            report = await service.refresh_prices()
        _report_price_refresh(report)

    return IntervalScheduler(
        name="price-refresh",
        interval_minutes=settings.price_refresh_interval_minutes,
        last_run_at=lambda: latest_price_refresh(app),
        run=run,
    )


def _report_price_refresh(report: RefreshReport) -> None:
    """Log what one refresh did. **Counts and pairs, never an amount.**

    A price is public market data and `cli.refresh-prices` prints it, because printing it is
    the point of running that command by hand. A scheduled refresh is different: nobody is
    reading its output, it happens every hour forever, and a log line carrying a number
    invites the habit of putting values in logs -- which is one careless edit away from a log
    line carrying a *quantity*, and a quantity is the owner's holdings.

    Warning when anything went unpriced, because that is the line an operator should see;
    `every_source_failed` is the one of the four reasons that means "go and look at a vendor".
    """
    unavailable = tuple(
        f"{entry.asset_symbol}/{entry.quote_currency}" for entry in report.unavailable
    )
    if unavailable:
        _logger.warning(
            "price_refresh_incomplete",
            refreshed=len(report.refreshed),
            unavailable=unavailable,
        )
        return
    _logger.info("price_refresh_finished", refreshed=len(report.refreshed))


async def latest_price_refresh(app: FastAPI) -> datetime | None:
    """When the newest price row was written, over a session of its own.

    The price timer's startup condition, and the counterpart of `latest_sync_attempt`. A
    fresh deployment prices its holdings immediately rather than showing `never_fetched` for
    an hour.

    **Unlike the balance timer, this counts successes**, because there is no record of a
    price *attempt* -- the refresh writes rows only for what it fetched, and a history of
    refresh runs was ruled out of scope. The residual is bounded and stated in
    `docs/operations.md`: while every source is failing, a crash loop costs one price request
    per restart, and the first refresh that succeeds writes rows and suppresses the next one.
    """
    sessionmaker = app.state.db_sessionmaker
    async with sessionmaker() as session:
        return await PriceRepository(session).latest_fetched_at()


async def latest_sync_attempt(app: FastAPI) -> datetime | None:
    """When the newest sync of any status started, over a session of its own.

    The balance timer's "last run", and it counts **attempts** rather than successes: a
    crash-looping container leaves `interrupted` runs with no `finished_at`, and a guard that
    only counted finished runs let every restart sync again against both public indexes. The
    property is "at most one sync per interval across restarts", which `docs/operations.md`
    states in those words.
    """
    sessionmaker = app.state.db_sessionmaker
    async with sessionmaker() as session:
        return await SyncRunRepository(session).latest_started_at()


async def sweep_interrupted_runs(app: FastAPI) -> None:
    """Mark every run still at `running` as `interrupted`, and say so if there were any.

    **Never raises.** At startup a failure here would stop an otherwise healthy application
    over bookkeeping; at shutdown it would mask whatever was already going wrong and leave
    the engine undisposed. Either way the next startup sweeps again, so the cost of skipping
    one is a row that stays `running` for an interval rather than data.
    """
    try:
        sessionmaker = app.state.db_sessionmaker
        async with sessionmaker() as session:
            swept = await SyncRunRepository(session).sweep_interrupted()
            await session.commit()
    except Exception:
        _logger.exception("balance_sync_orphan_sweep_failed")
        return
    if swept:
        _logger.warning("balance_sync_runs_marked_interrupted", runs=swept)


async def latest_exchange_sync_attempt(app: FastAPI) -> datetime | None:
    """When the newest exchange sync of any status started, over a session of its own.

    The exchange timer's "last run": an attempt, not a success, so a crash-looping container
    does not ask the venue again on every restart.
    """
    sessionmaker = app.state.db_sessionmaker
    async with sessionmaker() as session:
        return await ExchangeSyncRunRepository(session).latest_started_at()


async def sweep_interrupted_exchange_runs(app: FastAPI) -> None:
    """Mark every exchange run still at `running` as `interrupted`. **Never raises.**

    Its own function and its own `try`, separate from the balance sweep, so that one table
    failing to sweep does not leave the other unswept.
    """
    try:
        sessionmaker = app.state.db_sessionmaker
        async with sessionmaker() as session:
            swept = await ExchangeSyncRunRepository(session).sweep_interrupted()
            await session.commit()
    except Exception:
        _logger.exception("exchange_sync_orphan_sweep_failed")
        return
    if swept:
        _logger.warning("exchange_sync_runs_marked_interrupted", runs=swept)


async def warm_password_hasher(app: FastAPI) -> None:
    """Compute the dummy hash now, so that no request is the first to pay for it.

    `PasswordHasher.dummy_hash` is what a login against an unknown username verifies
    against, and it is a cached property: whoever touches it first pays to build it. Left
    to the request path, that first toucher is the first login for a username that does
    not exist -- which then costs two Argon2id operations against one for a wrong
    password, roughly 500 ms against 250 ms on the Pi. That is a clean "no such user"
    signal, once per process, in the one place the design says there must not be one.

    Here rather than in `create_app`, deliberately: the image's build-time smoke check
    calls `create_app()`, so warming there would run a 64 MiB hash during `docker build`
    and would break the invariant that building the application has no side effects. A
    lifespan has already run the migrations by this point and is the right place to spend
    a quarter of a second.

    In a worker thread for the same reason the migration is: it is a CPU-bound call, and
    the event loop is the one thing in this process that must not be blocked.
    """
    hasher: PasswordHasher = app.state.password_hasher
    await to_thread.run_sync(lambda: hasher.dummy_hash)


async def bootstrap_owner(app: FastAPI, settings: Settings) -> None:
    """Create the owner account from the bootstrap password, if there is not one already.

    This is how a fresh deployment becomes usable without an interactive shell: the
    variable is set once in the host's environment file, the first start creates the
    account, and every start after that finds one and leaves it alone. Leaving the
    variable in place therefore does not reset the password on the next deploy, which is
    the failure this would otherwise cause on every single release.

    The password itself never reaches this function as a `str`: it is a `SecretStr`
    unwrapped at the call into the service, so it cannot be carried into a log record or
    a traceback frame by accident.
    """
    if settings.bootstrap_password is None:
        return
    sessionmaker = app.state.db_sessionmaker
    async with sessionmaker() as session:
        service = auth_service_for(app, session)
        created = await service.bootstrap_user(
            settings.bootstrap_username,
            settings.bootstrap_password.get_secret_value(),
        )
    _logger.info(
        "bootstrap_user_created" if created else "bootstrap_user_already_exists",
        username=settings.bootstrap_username,
    )


def create_app() -> FastAPI:
    """Build the application: settings, logging, error rendering, routes, SPA."""
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Portfolio API",
        version=__version__,
        # Everything the API owns lives under /api, including its own documentation,
        # because the SPA mount below takes over the rest of the URL space.
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        swagger_ui_oauth2_redirect_url=f"{API_PREFIX}/docs/oauth2-redirect",
        redoc_url=None,
        # Nothing here runs until the server starts the application, so `create_app()`
        # stays a pure, side-effect-free build -- which is what the image's build-time
        # smoke check and the ASGI-transport tests rely on.
        lifespan=lifespan,
    )

    register_exception_handlers(app)
    install_auth_runtime(app, settings)
    install_accounting_runtime(app)
    # From the factory rather than the lifespan, for the reason `install_auth_runtime` gives:
    # it holds the timer's last attempt, which `GET /api/health/detail` serves, and building it
    # reads nothing from the file system, so `create_app()` stays free of side effects.
    app.state.backup_service = build_backup_service(settings)

    # Middleware runs before routing, which is the whole point: a check that ran after
    # routing would see an API request only when a route happened to exist for it.
    app.add_middleware(RequestGuardMiddleware, settings=settings)
    # Added last, so it is the outermost of the application's own middleware: the request id
    # it binds reaches the guard above, every route, and the 500 handler outside them all
    # (#23). A pure ASGI middleware, for the reason `api/request_context.py` gives. It labels
    # a request no route matched by the guard's own `/api` test and by the documentation
    # paths configured above, read from the application rather than repeated here.
    app.add_middleware(
        RequestContextMiddleware,
        is_api_path=is_api_path,
        documentation_paths=documentation_paths(app),
    )

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(wallets.router, prefix=API_PREFIX)
    # After `wallets`, and it does not matter: the balance router declares its own full
    # paths -- `/wallets/{wallet_id}/balances` among them -- and none of them collides with
    # a wallet route. Only the SPA mount below is order-sensitive.
    app.include_router(balances.router, prefix=API_PREFIX)
    app.include_router(exchanges.router, prefix=API_PREFIX)
    app.include_router(accounting.router, prefix=API_PREFIX)
    # `/accounting/adjustments` beside `/accounting/positions`: two routers under one prefix,
    # and no path of one is a path of the other.
    app.include_router(adjustments.router, prefix=API_PREFIX)

    # Mounted last and at the root: it matches every path outside `/api`, so a route
    # registered after it there would be unreachable. It never matches one under `/api`,
    # which is left to the router (#132).
    mount_spa(app)

    return app


app = create_app()
