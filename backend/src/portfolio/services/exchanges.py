"""Reading exchange accounts and the exchange run log back. **No provider here, by contract.**

The read side of #15, and what `api/routers/exchanges.py` imports. It imports repositories
and the domain vocabulary and nothing from `providers`, and `backend/.importlinter`'s
`api-never-reaches-an-exchange-provider` contract makes that structural: no module under
`portfolio.api` may import `portfolio.providers.exchanges`, directly or through anything
else. So no request path can reach the module that holds `Credentials`, except through the
coordinator `main.py` wired -- which is why `ExchangeSyncRunSummary` and its vocabulary live
in `repositories/exchange_sync_runs.py` rather than beside the sync that fills them in.

## `configured` is the whole disclosure about credentials

The lifespan publishes `configured_exchanges`, a `frozenset[ExchangeKey]` built from the keys
of the provider mapping, and that set is the only thing this module learns about
credentials. `configured` is `exchange_key in configured_exchanges`. No view here has a field
that could carry a key, a secret or a passphrase, and a test walks the OpenAPI document to
prove no response model does either.

## `history_truncated` is derived, never stored

`effective_since > requested_since`, and `False` when either is unknown. It compares two
aware datetimes in Python, as every datetime comparison in this application does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from portfolio.domain.exchanges import AccountSyncStatus, ExchangeKey
from portfolio.repositories.exchange_sync_runs import (
    AccountOutcome,
    AccountOutcomeStatus,
    ExchangeSyncErrorKind,
    ExchangeSyncRunRepository,
    ExchangeSyncRunSummary,
    SyncRunStatus,
    SyncTrigger,
)
from portfolio.repositories.exchanges import (
    ExchangeAccountRepository,
    ExchangeFillRepository,
    ExchangeSyncWindowRepository,
)

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.repositories.exchanges import ExchangeAccountState
    from portfolio.services.auth import Principal

__all__ = [
    "DEFAULT_EXCHANGE_RUNS_LIMIT",
    "MAX_EXCHANGE_RUNS_LIMIT",
    "AccountOutcome",
    "AccountOutcomeStatus",
    "AccountSyncStatus",
    "ExchangeKey",
    "ExchangeService",
    "ExchangeSyncErrorKind",
    "ExchangeSyncRunSummary",
    "ExchangeView",
    "LastError",
    "SyncRunStatus",
    "SyncTrigger",
    "build_exchange_service",
    "history_truncated",
]
"""The run vocabulary and the domain enums are **re-exported** for `api/schemas/exchanges.py`,
which may not import `portfolio.repositories` -- the reason `services/balances.py` re-exports
its run vocabulary."""

DEFAULT_EXCHANGE_RUNS_LIMIT: Final = 20
MAX_EXCHANGE_RUNS_LIMIT: Final = 100
"""`GET /api/exchanges/runs` page sizes. The schema refuses anything outside `1..100` with a
422; `list_runs` clamps as well, for a caller that does not go through the schema."""


@dataclass(frozen=True, slots=True)
class LastError:
    """Why the account's latest attempted sync failed: the kind, and the recorded detail."""

    error_kind: ExchangeSyncErrorKind
    detail: str | None


@dataclass(frozen=True, slots=True)
class ExchangeView:
    """One venue as `GET /api/exchanges` renders it.

    A configured venue with no account row yet -- nothing has run since its credentials were
    set -- is `never_synced` with every instant `None` and every count zero. A venue with a
    row and no credentials any more is listed with `configured=False` and its last state.
    """

    exchange_key: ExchangeKey
    configured: bool
    status: AccountSyncStatus
    syncing: bool
    requested_since: datetime | None
    effective_since: datetime | None
    history_truncated: bool
    last_synced_at: datetime | None
    fills_stored: int
    pending_windows: int
    last_error: LastError | None


def history_truncated(
    requested_since: datetime | None,
    effective_since: datetime | None,
) -> bool:
    """Whether the history held starts later than the owner asked. `False` if either is unknown.

    Strictly later: an effective start equal to the requested one is the whole request.
    """
    if requested_since is None or effective_since is None:
        return False
    return effective_since > requested_since


def _last_error_of(outcome: AccountOutcome | None) -> LastError | None:
    """The error of the latest attempted outcome, if that outcome failed.

    A failed outcome always carries a kind when this application wrote it; one without is a
    hand edit, and reporting no error is better than inventing one.
    """
    if (
        outcome is None
        or outcome.status is not AccountOutcomeStatus.FAILED
        or outcome.error_kind is None
    ):
        return None
    return LastError(error_kind=outcome.error_kind, detail=outcome.detail)


class ExchangeService:
    """Builds the account list and the run log. Read-only: the session is never committed."""

    def __init__(
        self,
        *,
        accounts: ExchangeAccountRepository,
        windows: ExchangeSyncWindowRepository,
        fills: ExchangeFillRepository,
        runs: ExchangeSyncRunRepository,
        configured: frozenset[ExchangeKey],
        syncing: bool,
    ) -> None:
        self._accounts = accounts
        self._windows = windows
        self._fills = fills
        self._runs = runs
        self._configured = configured
        self._syncing = syncing

    async def list_exchanges(self, principal: Principal) -> list[ExchangeView]:
        """Every configured venue plus every venue the caller has an account row for.

        Sorted by `exchange_key`. Empty when nothing is configured and nothing was ever
        synced, which is #16's empty state.

        `syncing` is `True` for a configured venue while an exchange sync is in flight: the
        run covers every configured venue, and one without credentials is not in it.
        """
        rows = {
            state.exchange_key: state
            for state in await self._accounts.list_for_user(principal.user_id)
        }
        keys = sorted(self._configured | rows.keys())
        return [await self._view(key, rows.get(key)) for key in keys]

    async def _view(
        self, exchange_key: ExchangeKey, state: ExchangeAccountState | None
    ) -> ExchangeView:
        """One venue's view, from its account row if it has one."""
        configured = exchange_key in self._configured
        syncing = self._syncing and configured
        if state is None:
            return ExchangeView(
                exchange_key=exchange_key,
                configured=configured,
                status=AccountSyncStatus.NEVER_SYNCED,
                syncing=syncing,
                requested_since=None,
                effective_since=None,
                history_truncated=False,
                last_synced_at=None,
                fills_stored=0,
                pending_windows=0,
                last_error=None,
            )
        return ExchangeView(
            exchange_key=exchange_key,
            configured=configured,
            status=state.sync_status,
            syncing=syncing,
            requested_since=state.requested_since,
            effective_since=state.effective_since,
            history_truncated=history_truncated(state.requested_since, state.effective_since),
            last_synced_at=state.last_synced_at,
            fills_stored=await self._fills.count_for_account(state.id),
            pending_windows=await self._windows.count_for_account(state.id),
            last_error=_last_error_of(await self._runs.latest_attempted_outcome(state.id)),
        )

    async def list_runs(self, *, limit: int) -> list[ExchangeSyncRunSummary]:
        """The most recent exchange sync runs, newest first, `limit` clamped to `1..100`."""
        bounded = max(1, min(limit, MAX_EXCHANGE_RUNS_LIMIT))
        return await self._runs.list_runs(limit=bounded)


def build_exchange_service(
    session: AsyncSession,
    *,
    configured: frozenset[ExchangeKey],
    syncing: bool,
) -> ExchangeService:
    """Assemble the read side over one session.

    `configured` is the lifespan's `configured_exchanges`; `syncing` is the exchange
    coordinator's `in_flight` at the moment the request was served.
    """
    return ExchangeService(
        accounts=ExchangeAccountRepository(session),
        windows=ExchangeSyncWindowRepository(session),
        fills=ExchangeFillRepository(session),
        runs=ExchangeSyncRunRepository(session),
        configured=configured,
        syncing=syncing,
    )
