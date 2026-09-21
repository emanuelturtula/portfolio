"""The wallet registry's policy: what a duplicate means, and what archiving does.

Three decisions live here rather than in the router or the repository, because the CLI and
any future importer have to inherit them:

* **An address is validated before anything touches the database.** `domain.chains` does
  it, offline, and its rejection travels out of this service unwrapped -- the API layer is
  the one that knows a rejected address is a 422, and this layer has no business knowing
  what HTTP is.
* **A duplicate is a conflict whether or not the existing row is archived.** An archived
  row still occupies its slot in `uq_wallets_user_chain_address`, so allowing a re-add
  would mean either a database error the owner cannot act on, or a silent resurrection of
  a retired wallet complete with its old label and its old history. The conflict says
  which of the two cases it is; un-archiving is an explicit `PATCH`.
* **`DELETE` archives and is idempotent.** Deleting a wallet that is already archived
  succeeds: the end state the caller asked for is the end state they get, and a 404 there
  would make a retry after a dropped response look like a bug.

Nothing in this module logs an address, and nothing puts one in an exception message. The
registry's whole content is the owner's holdings, and a log line is the easiest way for it
to leave the process.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Final

from portfolio.domain.chains import validate_address
from portfolio.repositories.wallets import WalletRepository

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.db.models import Wallet
    from portfolio.services.auth import Principal

DUPLICATE_DETAIL: Final = "This address is already registered for this chain."
DUPLICATE_ARCHIVED_DETAIL: Final = (
    "This address is already registered for this chain and is archived. "
    "Restore it instead of adding it again."
)
WALLET_NOT_FOUND_DETAIL: Final = "No wallet with that id."
"""The three strings a failure can carry to the client. **None of them names the address.**

A 409 that quoted the address would put the owner's holdings in a response body, and from
there into whatever the client logs -- which is the same leak a 422 would be, arriving
through a different door.
"""

MAX_LABEL_LENGTH: Final = 100
"""What a label may be, in characters. Enforced here as well as in the request schema,
because the CLI does not go through the schema."""


def utc_now() -> datetime:
    """The clock, in one place, so a test can replace it with a value it chose."""
    return datetime.now(UTC)


class Unset(Enum):
    """The absence of a value, for a `PATCH` that must tell "omitted" from "null".

    A single-member enum rather than a bare `object()` sentinel: a type checker narrows
    `str | None | Unset` on an `isinstance` check and cannot narrow an anonymous object,
    so this is the spelling that keeps `--strict` honest about which branch has a `str`.
    """

    TOKEN = 1


UNSET: Final = Unset.TOKEN
"""`PATCH {"archived": false}` must leave the label alone; `PATCH {"label": null}` must
clear it. Those are different requests and a plain `None` default cannot tell them apart."""


class WalletError(Exception):
    """Base class for every failure this service raises."""


class WalletAlreadyExistsError(WalletError):
    """This address is already registered on this chain for this account.

    `archived` says whether the row holding the slot is a retired one, because that is the
    difference between "you already have this" and "you had this and put it away".
    """

    def __init__(self, *, archived: bool) -> None:
        self.archived = archived
        super().__init__(DUPLICATE_ARCHIVED_DETAIL if archived else DUPLICATE_DETAIL)


class WalletNotFoundError(WalletError):
    """No wallet with that id belongs to this account."""

    def __init__(self) -> None:
        super().__init__(WALLET_NOT_FOUND_DETAIL)


@dataclass(frozen=True, slots=True)
class WalletView:
    """A wallet as everything above this layer sees it.

    A frozen snapshot rather than the ORM row, for two reasons. The row is attached to a
    session the request dependency closes on the way out, so a router serialising one
    would be reading a detached instance. And `address_canonical` is an implementation
    detail of the uniqueness rule -- publishing both forms would invite a client to pick
    the wrong one -- so the view carries only the display form, under the name `address`.
    """

    id: int
    chain_key: str
    address: str
    label: str | None
    archived: bool
    created_at: datetime
    updated_at: datetime


def view_of(wallet: Wallet) -> WalletView:
    """Snapshot a row while its session is still open."""
    return WalletView(
        id=wallet.id,
        chain_key=wallet.chain_key,
        address=wallet.address_display,
        label=wallet.label,
        archived=wallet.archived_at is not None,
        created_at=wallet.created_at,
        updated_at=wallet.updated_at,
    )


def normalise_label(label: str | None) -> str | None:
    """Trim a label, and treat a blank one as no label at all.

    `""` and `"   "` are what an emptied text field sends, and storing either would put a
    row in the database whose label renders as nothing while `label is None` is false
    everywhere that checks.

    Raises:
        ValueError: the label is longer than `MAX_LABEL_LENGTH` after trimming.
    """
    if label is None:
        return None
    trimmed = label.strip()
    if len(trimmed) > MAX_LABEL_LENGTH:
        message = f"A label may be at most {MAX_LABEL_LENGTH} characters."
        raise ValueError(message)
    return trimmed or None


class WalletService:
    """The unit of work for the wallet registry.

    It owns the transaction: the repository flushes and this class commits. The caller --
    a request dependency or the CLI -- owns the session and closes it, so an exception
    leaves uncommitted work rolled back rather than half applied.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        wallets: WalletRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._wallets = wallets
        self._clock = clock

    async def list_wallets(
        self,
        principal: Principal,
        *,
        include_archived: bool = False,
    ) -> list[WalletView]:
        """Every wallet the caller holds. Archived ones are hidden unless asked for."""
        rows = await self._wallets.list_for_user(
            principal.user_id,
            include_archived=include_archived,
        )
        return [view_of(row) for row in rows]

    async def create_wallet(
        self,
        principal: Principal,
        *,
        chain_key: str,
        address: str,
        label: str | None = None,
    ) -> WalletView:
        """Register an address, after proving offline that it is one.

        Validation comes first, before the duplicate lookup, so that a mistyped address is
        reported as mistyped rather than as "no conflict, here is your new wallet". The
        lookup is on the canonical form, which is what the unique constraint indexes.

        Raises:
            AddressInvalidError: the chain key or the address did not validate.
            WalletAlreadyExistsError: the slot is taken, by an active or an archived row.
            ValueError: the label is too long.
        """
        validated = validate_address(chain_key, address)
        clean_label = normalise_label(label)

        existing = await self._wallets.find_by_canonical(
            user_id=principal.user_id,
            chain_key=chain_key,
            address_canonical=validated.canonical,
        )
        if existing is not None:
            raise WalletAlreadyExistsError(archived=existing.archived_at is not None)

        wallet = await self._wallets.add(
            user_id=principal.user_id,
            chain_key=chain_key,
            address_canonical=validated.canonical,
            address_display=validated.display,
            label=clean_label,
            created_at=self._clock(),
        )
        view = view_of(wallet)
        await self._session.commit()
        return view

    async def update_wallet(
        self,
        principal: Principal,
        wallet_id: int,
        *,
        label: str | Unset | None = UNSET,
        archived: bool | Unset = UNSET,
    ) -> WalletView:
        """Rename a wallet, archive it, or bring it back.

        Both fields are optional and `UNSET` is not `None`: a request that sends only
        `archived` leaves the label exactly as it was, and one that sends `"label": null`
        clears it. A request that sends neither is a no-op that still returns the wallet,
        which is what makes a retried `PATCH` safe.

        Raises:
            WalletNotFoundError: no wallet with that id belongs to the caller.
            ValueError: the label is too long.
        """
        wallet = await self._require_wallet(principal, wallet_id)
        now = self._clock()

        if not isinstance(label, Unset):
            await self._wallets.set_label(wallet, normalise_label(label), now)
        if not isinstance(archived, Unset):
            await self._wallets.set_archived_at(wallet, now if archived else None, now)

        view = view_of(wallet)
        await self._session.commit()
        return view

    async def archive_wallet(self, principal: Principal, wallet_id: int) -> None:
        """Retire a wallet without deleting it, idempotently.

        Archiving an already-archived wallet changes nothing and does not fail: the
        timestamp is left at the moment it was first retired, because moving it would
        rewrite history on a request that asked for no change.

        Raises:
            WalletNotFoundError: no wallet with that id belongs to the caller.
        """
        wallet = await self._require_wallet(principal, wallet_id)
        if wallet.archived_at is not None:
            return
        now = self._clock()
        await self._wallets.set_archived_at(wallet, now, now)
        await self._session.commit()

    async def _require_wallet(self, principal: Principal, wallet_id: int) -> Wallet:
        """Fetch a wallet the caller owns, or raise.

        Scoped by `user_id`, so a wallet that exists but belongs to somebody else is
        indistinguishable from one that does not exist. That is the only answer that does
        not confirm the id.
        """
        wallet = await self._wallets.get_for_user(principal.user_id, wallet_id)
        if wallet is None:
            raise WalletNotFoundError
        return wallet


def build_wallet_service(
    session: AsyncSession,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> WalletService:
    """Assemble the service over one database session.

    The repository is built here rather than injected because there is exactly one
    implementation of it; the clock is injectable so that a test can name the instant.
    """
    return WalletService(session=session, wallets=WalletRepository(session), clock=clock)
