"""Whether a wallet's latest reading is current: the one statement of the wallet rule.

A balance snapshot is the last thing known about a wallet, and the dashboard keeps counting it
when it is no longer current, because dropping it would show the coins as gone. What it must
not do is present an old reading as a fresh one. `wallet_reading_problem` says which readings
are not current, and why, so the summary can name them.

* **Unread** -- no balance run has ever read the wallet.
* **Chain failed** -- the latest finished balance run recorded the wallet's chain as `failed`,
  and no later run has written the wallet's reading (spec 028). A run still in flight, or one
  interrupted after it read the chain, may already have committed a reading newer than that
  verdict, and such a reading is current.
* **Stale** -- the reading is more than `MAX_READING_AGE` old with nothing above to explain
  it: the balance timer is off, or no balance run finishes.

**The latest finished balance run** is the newest `sync_runs` row, by `id`, whose status is
`success`, `partial` or `failed` (`SyncRunRepository.latest_finished`). A `running` or
`interrupted` run has no chain rows -- `finish_run` writes them with the final status -- so it
cannot say which chains failed, and the run before it still stands.

A reading dated after the clock -- the clock stepped back since -- is current: its age is not
positive, and refusing it would discard the newest reading there is.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from portfolio.domain.portfolio import MAX_READING_AGE
from portfolio.repositories.sync_runs import SyncRunStatus

if TYPE_CHECKING:
    from datetime import datetime

    from portfolio.db.models import BalanceSnapshot
    from portfolio.repositories.sync_runs import SyncRunSummary

__all__ = ["WalletReadingProblem", "wallet_reading_problem"]


class WalletReadingProblem(StrEnum):
    """Why a wallet's reading is not current.

    Declared in the order they are tested, and the first that applies is the answer. A wallet
    with no reading whose chain failed is `CHAIN_FAILED`, not `UNREAD`: it is one the owner
    can act on.
    """

    CHAIN_FAILED = "chain_failed"
    UNREAD = "unread"
    STALE = "stale"


def wallet_reading_problem(
    chain_key: str,
    reading: BalanceSnapshot | None,
    latest_finished_run: SyncRunSummary | None,
    now: datetime,
) -> WalletReadingProblem | None:
    """Why a wallet's reading is not current as of `now`, or `None` when it is. Pure.

    `chain_key` is the wallet's, `reading` its latest balance snapshot, `None` when no run has
    read it, and `latest_finished_run` what `SyncRunRepository.latest_finished` answered,
    `None` when no run has finished. A chain with no outcome in the run, and no finished run
    at all, are both "did not fail".

    The ids are two integers and the age is `now - observed_at`, two aware datetimes, all
    compared in Python.
    """
    if (
        latest_finished_run is not None
        and _failed_in(latest_finished_run, chain_key)
        and (reading is None or reading.sync_run_id <= latest_finished_run.run_id)
    ):
        return WalletReadingProblem.CHAIN_FAILED
    if reading is None:
        return WalletReadingProblem.UNREAD
    if now - reading.observed_at > MAX_READING_AGE:
        return WalletReadingProblem.STALE
    return None


def _failed_in(run: SyncRunSummary, chain_key: str) -> bool:
    """Whether `run` recorded `chain_key` as failed. A chain it has no outcome for did not."""
    return any(
        outcome.chain_key == chain_key and outcome.status is SyncRunStatus.FAILED
        for outcome in run.chains
    )
