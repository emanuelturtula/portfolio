"""Reads and writes of `exchange_balances`, and of the two account columns that describe them.

The storage side of #104. Queries and nothing else: no clock, no policy about when a read is
attempted or what a failure means. The repository is handed an `AsyncSession` and **never
commits** -- the sync commits a reading as one transaction, and that decision belongs to the
caller that owns the unit of work.

## A reading is replaced whole

`replace` deletes the account's rows, inserts the new ones, and sets `balances_read_at` and
clears `balances_error` on the account, all in the caller's transaction. So an account's rows
are always one answer from the venue: an asset the venue no longer lists is gone, not left
behind at the amount it last had. `record_failure` writes only the kind, and **keeps the rows
and `balances_read_at` of the last good reading** (spec 025, *Design: storage*). Whether a
kept reading may still be compared is not decided here: `services/reconciliation.py` holds
that rule, and it leaves out the reading of an account whose last read failed.

## Nothing is summed, compared or ordered in SQL on `quantity` or on a datetime

`quantity` is `NumericText`, a `TEXT` column, and `SUM()`, `ORDER BY` or `<` on one would
coerce it to a float in SQLite. `balances_read_at` is text there too. The rows are read whole
and ordered by `exchange_key` and by `asset` -- both plain text, compared as text -- and the
sums are the reconciliation service's, in Python, through `domain.money.add`.

## This module cannot import `AssetBalance`

`repositories` and `providers` are siblings in the layers contract and may not import each
other. `BalanceRecord` is the structural shape `replace` needs, and
`providers.exchanges.base.AssetBalance` satisfies it -- which `mypy --strict` checks at the
one call site, in `services/exchange_sync.py`. The arrangement `FillRecord` has for a fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import delete, insert, select, update

from portfolio.db.models import ExchangeAccount, ExchangeBalance
from portfolio.domain.exchanges import AccountSyncStatus, ExchangeKey
from portfolio.repositories.exchange_sync_runs import ExchangeSyncErrorKind

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "AccountBalances",
    "BalanceRecord",
    "ExchangeBalanceRepository",
    "StoredBalance",
]


class BalanceRecord(Protocol):
    """The shape of a balance `replace` can store. `AssetBalance` is one.

    Read-only properties rather than plain attributes, for the reason `FillRecord` gives:
    `AssetBalance` is a frozen dataclass, and a protocol declaring settable attributes would
    not accept it.
    """

    @property
    def asset(self) -> str:
        """The venue's name for the asset, as that venue's fills spell it."""

    @property
    def quantity(self) -> Decimal:
        """The total held in the spot account. Storable at `FILL_SCALE` without rounding."""


@dataclass(frozen=True, slots=True)
class StoredBalance:
    """One `exchange_balances` row, copied out of the session: an asset and what was held."""

    asset: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class AccountBalances:
    """One exchange account's last balance reading, and how the last attempt went.

    * `sync_status` -- where the account's **fill** sync stands. It says nothing about the
      balances themselves; it is here because balances are read only after a successful fill
      sync, so an account that is not `ok` has a reading nothing is refreshing. Whether that
      makes the reading unusable is the caller's rule, not this layer's.
    * `balances_read_at` -- when a read last succeeded; `None` when none ever has, and then
      `balances` is empty.
    * `balances_error` -- the kind the last attempt failed with; `None` when it succeeded or
      none was made. Set beside a `balances_read_at`, it means `balances` is the reading
      before the failure.
    * `balances` -- the stored rows, sorted by asset. Empty also for an account whose spot
      account holds nothing, which `balances_read_at` tells apart from "never read".
    """

    exchange_key: ExchangeKey
    sync_status: AccountSyncStatus
    balances_read_at: datetime | None
    balances_error: ExchangeSyncErrorKind | None
    balances: tuple[StoredBalance, ...]


class ExchangeBalanceRepository:
    """Every query this application makes against `exchange_balances`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace(
        self,
        account_id: int,
        balances: Sequence[BalanceRecord],
        read_at: datetime,
    ) -> None:
        """Replace the account's stored reading by `balances`, read at `read_at`.

        Deletes every row of the account, inserts one per balance, and records the success
        on the account: `balances_read_at` becomes `read_at` and `balances_error` is cleared.
        An empty `balances` is a reading too -- the spot account holds nothing -- and leaves
        the account with no rows and a fresh `balances_read_at`.

        Neither commits nor rolls back: the caller's transaction is what makes the delete, the
        inserts and the update one change. An amount `NumericText(18)` refuses raises out of
        the insert; `AssetBalance` refuses the same amounts first, so none arrives from a
        provider.
        """
        await self._session.execute(
            delete(ExchangeBalance).where(ExchangeBalance.exchange_account_id == account_id)
        )
        if balances:
            await self._session.execute(
                insert(ExchangeBalance),
                [
                    {
                        "exchange_account_id": account_id,
                        "asset": balance.asset,
                        "quantity": balance.quantity,
                    }
                    for balance in balances
                ],
            )
        await self._session.execute(
            update(ExchangeAccount)
            .where(ExchangeAccount.id == account_id)
            .values(balances_read_at=read_at, balances_error=None)
        )

    async def record_failure(self, account_id: int, kind: ExchangeSyncErrorKind) -> None:
        """Record why the account's last balance read failed, and nothing else.

        The rows and `balances_read_at` stay as the last good reading left them. An `UPDATE`
        by id, for the reason every write in `repositories/exchanges.py` is one.
        """
        await self._session.execute(
            update(ExchangeAccount)
            .where(ExchangeAccount.id == account_id)
            .values(balances_error=kind)
        )

    async def list_for_user(self, user_id: int) -> tuple[AccountBalances, ...]:
        """Every exchange account `user_id` owns, by `exchange_key`, each with its reading.

        Every account row is returned, read or not, so that a venue whose balances could not
        be read is named rather than missing. Two statements whatever the number of accounts:
        the accounts' four columns, then every row belonging to them, ordered by `asset`.
        Both name their columns, and both are read afresh from the database rather than from
        the session's identity map.
        """
        accounts = (
            await self._session.execute(
                select(
                    ExchangeAccount.id,
                    ExchangeAccount.exchange_key,
                    ExchangeAccount.sync_status,
                    ExchangeAccount.balances_read_at,
                    ExchangeAccount.balances_error,
                )
                .where(ExchangeAccount.user_id == user_id)
                .order_by(ExchangeAccount.exchange_key)
            )
        ).all()
        if not accounts:
            return ()
        stored: dict[int, list[StoredBalance]] = {account.id: [] for account in accounts}
        rows = await self._session.execute(
            select(
                ExchangeBalance.exchange_account_id,
                ExchangeBalance.asset,
                ExchangeBalance.quantity,
            )
            .where(ExchangeBalance.exchange_account_id.in_(stored))
            .order_by(ExchangeBalance.asset)
        )
        for account_id, asset, quantity in rows:
            stored[account_id].append(StoredBalance(asset=asset, quantity=quantity))
        return tuple(
            AccountBalances(
                exchange_key=ExchangeKey(account.exchange_key),
                sync_status=AccountSyncStatus(account.sync_status),
                balances_read_at=account.balances_read_at,
                balances_error=(
                    None
                    if account.balances_error is None
                    else ExchangeSyncErrorKind(account.balances_error)
                ),
                balances=tuple(stored[account.id]),
            )
            for account in accounts
        )
