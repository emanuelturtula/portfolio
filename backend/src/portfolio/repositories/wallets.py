"""Reads and writes of the `wallets` table.

Queries and nothing else: no validation, no clock, no policy about what a duplicate means.
The repository is handed an `AsyncSession` and it does not commit -- the service that
opened the unit of work decides when it ends.

Every lookup is scoped by `user_id`, including the ones that take a primary key. The
product is single user today, and a `WHERE id = ?` that trusts the id would be the one
query that stops being correct the moment that changes. Scoping it costs nothing and turns
"another owner's wallet" into an ordinary not-found rather than a leak.

`archived_at` is a nullable timestamp rather than a flag, so "still active" is
`IS NULL` and the archived rows stay in the table where the unique constraint can still
see them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from portfolio.db.models import Wallet

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


class WalletConstraintError(Exception):
    """An insert the database refused, re-raised without the driver's exception.

    The point of this class is what it does **not** carry. SQLAlchemy renders the bound
    parameters into a `StatementError`'s message, so letting an `IntegrityError` travel
    upward means the row being written travels with it -- into a traceback, and from there
    into a log line that `redact_sensitive` cannot help with, because that processor
    matches key names and the field is called `exception`.

    Re-raising a bare exception here is what keeps `sqlalchemy` out of the service layer
    as well: a caller can tell "the database refused this" from "something else went
    wrong" without importing the driver to name its error type.

    Deliberately not called `DuplicateWalletError`. It means only that a constraint
    rejected the insert, and `wallets` has three -- a unique constraint, a foreign key and
    a check. Deciding it was a duplicate is the service's job, and the service decides it
    by looking, not by parsing a message.
    """


class WalletRepository:
    """Every query this application makes against `wallets`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(self, user_id: int, *, include_archived: bool = False) -> list[Wallet]:
        """Every wallet an account holds, oldest first.

        Ordered by the primary key, which is an `INTEGER` column: ordering by a `TEXT`
        money column would coerce it to a float, but there is no money here and an id is
        exactly the insertion order the owner added the wallets in.
        """
        statement = select(Wallet).where(Wallet.user_id == user_id)
        if not include_archived:
            statement = statement.where(Wallet.archived_at.is_(None))
        result = await self._session.scalars(statement.order_by(Wallet.id))
        return list(result)

    async def list_all_active(self) -> list[Wallet]:
        """Every active wallet in the database, whoever owns it, oldest first.

        **The one query in this module that is not scoped by `user_id`, and the exception
        is deliberate rather than an oversight of the rule above.** The balance sync has no
        principal: it is a scheduled background read, and the tick that runs it was started
        by a clock rather than by a request. Scoping it would mean either inventing an
        owner to attribute the run to, or reading one account's wallets on a schedule and
        leaving the others unread with nothing saying so.

        The name says so out loud -- `list_all_active` rather than another `list_for_*`
        overload -- because the failure this guards against is somebody reaching for it from
        a request path, where it would return another account's rows. Nothing in `api/`
        calls it; `services/balance_sync.py` is its only caller, and that module has no
        principal parameter to be careless with.

        Archived wallets are excluded: an address the owner retired is one they asked us to
        stop reading, and `archived_at` records when that happened precisely so the history
        already recorded survives the retirement.
        """
        result = await self._session.scalars(
            select(Wallet).where(Wallet.archived_at.is_(None)).order_by(Wallet.id)
        )
        return list(result)

    async def get_for_user(self, user_id: int, wallet_id: int) -> Wallet | None:
        """One wallet by id, provided it belongs to this account. Archived rows included."""
        found: Wallet | None = await self._session.scalar(
            select(Wallet).where(Wallet.id == wallet_id, Wallet.user_id == user_id)
        )
        return found

    async def find_by_canonical(
        self,
        *,
        user_id: int,
        chain_key: str,
        address_canonical: str,
    ) -> Wallet | None:
        """The row occupying this address's slot in the unique constraint, archived or not.

        The three columns are exactly the ones `uq_wallets_user_chain_address` covers, so
        what this finds is what an insert would collide with. Archived rows are
        deliberately not filtered out: they still hold their slot, and a caller that
        skipped them would report a conflict only after the database raised one.
        """
        found: Wallet | None = await self._session.scalar(
            select(Wallet).where(
                Wallet.user_id == user_id,
                Wallet.chain_key == chain_key,
                Wallet.address_canonical == address_canonical,
            )
        )
        return found

    async def add(
        self,
        *,
        user_id: int,
        chain_key: str,
        address_canonical: str,
        address_display: str,
        label: str | None,
        created_at: datetime,
    ) -> Wallet:
        """Insert a wallet. The caller has already validated the address.

        `updated_at` starts equal to `created_at`: a row that has never been edited has
        been "updated" exactly once, when it was created, and a null here would make every
        reader handle a case that lasts until the first `PATCH`.

        Raises:
            WalletConstraintError: the database refused the insert. The driver's exception
                is chained but is not allowed to propagate on its own, because its message
                contains the bound row -- both address columns included.
        """
        wallet = Wallet(
            user_id=user_id,
            chain_key=chain_key,
            address_canonical=address_canonical,
            address_display=address_display,
            label=label,
            archived_at=None,
            created_at=created_at,
            updated_at=created_at,
        )
        self._session.add(wallet)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # `from exc` keeps the chain for a debugger and for `logger.exception`, and it
            # is safe to keep *because* the engine is built with `hide_parameters=True`.
            # The two are one control: this translation alone would still let the
            # parameters through in the `__cause__`'s rendering.
            raise WalletConstraintError from exc
        return wallet

    async def set_label(self, wallet: Wallet, label: str | None, updated_at: datetime) -> None:
        """Replace the label in place. `None` clears it."""
        wallet.label = label
        wallet.updated_at = updated_at
        await self._session.flush()

    async def set_archived_at(
        self,
        wallet: Wallet,
        archived_at: datetime | None,
        updated_at: datetime,
    ) -> None:
        """Archive the row, or bring it back. `None` means active.

        Nothing is deleted here and there is no method that deletes one, which is the
        point: the balance snapshots that will reference `wallets.id` need the row to
        outlive the owner's interest in the address.
        """
        wallet.archived_at = archived_at
        wallet.updated_at = updated_at
        await self._session.flush()
