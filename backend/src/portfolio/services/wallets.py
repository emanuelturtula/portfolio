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

**An extended public key is registered through the same columns as an address** (spec 031,
R1), with `kind` saying which it is. `domain.chains.classify_wallet_key` decides, and it is
the only change to how a wallet is created: the duplicate check, the label and the archive
rules are the same for both. What differs is on the way out. A key is **never served in
full** (R8): `view_of` masks it to its first and last four characters, so nothing above this
layer -- router, response, client -- ever holds more of it than that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Final

from portfolio.domain.chains import WalletKind, classify_wallet_key
from portfolio.repositories.wallets import WalletConstraintError, WalletRepository

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
DUPLICATE_KEY_DETAIL: Final = (
    "This extended public key, or another export of the same account key, is already "
    "registered for this chain."
)
DUPLICATE_KEY_ARCHIVED_DETAIL: Final = (
    "This extended public key, or another export of the same account key, is already "
    "registered for this chain and is archived. Restore it instead of adding it again."
)
WALLET_NOT_FOUND_DETAIL: Final = "No wallet with that id."
"""The strings a failure can carry to the client. **None of them names the address or key.**

The two key sentences exist because a key is compared by its canonical form (spec 031): a
second export of the same account collides with the first, and the masked key in the list
shows the *first* export's last four characters, which the owner cannot match against what
they just pasted. So the sentence says that another export counts, instead of quoting
anything.

A 409 that quoted the address would put the owner's holdings in a response body, and from
there into whatever the client logs -- which is the same leak a 422 would be, arriving
through a different door.
"""

MAX_LABEL_LENGTH: Final = 100
"""What a label may be, in characters. Enforced here as well as in the request schema,
because the CLI does not go through the schema."""

MASK_VISIBLE_CHARACTERS: Final = 4
"""How many characters of an extended key stay visible at each end of its masked form (R8)."""

MASK_ELLIPSIS: Final = "\N{HORIZONTAL ELLIPSIS}"
"""What stands for the hidden middle: one HORIZONTAL ELLIPSIS, not three full stops."""


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
    `kind` picks the address sentences or the extended-key ones; every one is fixed.
    """

    def __init__(self, *, archived: bool, kind: WalletKind = WalletKind.ADDRESS) -> None:
        self.archived = archived
        if kind is WalletKind.EXTENDED_KEY:
            detail = DUPLICATE_KEY_ARCHIVED_DETAIL if archived else DUPLICATE_KEY_DETAIL
        else:
            detail = DUPLICATE_ARCHIVED_DETAIL if archived else DUPLICATE_DETAIL
        super().__init__(detail)


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

    For `kind = extended_key`, `address` is the masked key (R8) and never the key itself.
    """

    id: int
    chain_key: str
    kind: WalletKind
    address: str
    label: str | None
    archived: bool
    created_at: datetime
    updated_at: datetime


def mask_extended_key(key: str) -> str:
    """The first four characters, an ellipsis, and the last four (R8).

    Enough for the owner to tell two keys apart and to recognise the prefix -- and so the
    script type -- of each, and far too little to derive anything from: the 103 characters
    in between carry the chain code and the public key, and both are needed to derive a
    single address. A string too short to have a hidden middle is masked entirely, rather
    than served whole; no key the domain accepts is that short, so this is a floor, not a
    case.
    """
    if len(key) <= 2 * MASK_VISIBLE_CHARACTERS:
        return MASK_ELLIPSIS
    return f"{key[:MASK_VISIBLE_CHARACTERS]}{MASK_ELLIPSIS}{key[-MASK_VISIBLE_CHARACTERS:]}"


def view_of(wallet: Wallet) -> WalletView:
    """Snapshot a row while its session is still open, masking an extended key (R8)."""
    kind = WalletKind(wallet.kind)
    return WalletView(
        id=wallet.id,
        chain_key=wallet.chain_key,
        kind=kind,
        address=(
            mask_extended_key(wallet.address_display)
            if kind is WalletKind.EXTENDED_KEY
            else wallet.address_display
        ),
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
        """Register an address or an extended public key, after proving offline that it is one.

        Validation comes first, before the duplicate lookup, so that a mistyped address is
        reported as mistyped rather than as "no conflict, here is your new wallet". The
        lookup is on the canonical form, which is what the unique constraint indexes.

        `classify_wallet_key` decides which of the two `address` holds (spec 031): an
        extended-key prefix on Bitcoin goes to the key parser, which refuses a private key
        by its prefix before reading a character more of it; everything else is validated
        as an address, exactly as before. The network is not checked here (R3): it is the
        provider's, at read time, as it is for an address.

        Raises:
            AddressInvalidError: the chain key, the address or the key did not validate.
            WalletAlreadyExistsError: the slot is taken, by an active or an archived row.
            ValueError: the label is too long.
        """
        validated = classify_wallet_key(chain_key, address)
        clean_label = normalise_label(label)

        existing = await self._wallets.find_by_canonical(
            user_id=principal.user_id,
            chain_key=chain_key,
            address_canonical=validated.canonical,
        )
        if existing is not None:
            raise WalletAlreadyExistsError(
                archived=existing.archived_at is not None, kind=validated.kind
            )

        try:
            wallet = await self._wallets.add(
                user_id=principal.user_id,
                chain_key=chain_key,
                address_canonical=validated.canonical,
                address_display=validated.display,
                label=clean_label,
                created_at=self._clock(),
                kind=validated.kind.value,
            )
        except WalletConstraintError:
            # **The lookup above is an optimisation. This is the authority.** Nothing
            # holds a lock between a `find_by_canonical` that found nothing and this
            # insert, so two requests for the same address -- a double-clicked button, or
            # a client retrying a response it never received -- both pass the check and
            # the second meets `uq_wallets_user_chain_address`. Before this existed that
            # was a 500 whose traceback carried the row into the log.
            #
            # The reason is established by looking, not by parsing the driver's message.
            # `wallets` carries three constraints and only one of them means "duplicate":
            # if a row now holds the slot, that is what happened; if none does, the
            # foreign key or the chain check refused the insert, which is a bug in this
            # process rather than a race and has no business being reported as a conflict.
            # The rollback is what makes the second query legal at all -- a failed flush
            # leaves the session unusable until its transaction is abandoned.
            await self._session.rollback()
            conflicting = await self._wallets.find_by_canonical(
                user_id=principal.user_id,
                chain_key=chain_key,
                address_canonical=validated.canonical,
            )
            if conflicting is None:
                raise
            raise WalletAlreadyExistsError(
                archived=conflicting.archived_at is not None, kind=validated.kind
            ) from None
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
            await self._set_archived(wallet, archived=archived, now=now)

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
        await self._set_archived(wallet, archived=True, now=self._clock())
        await self._session.commit()

    async def _set_archived(self, wallet: Wallet, *, archived: bool, now: datetime) -> None:
        """Move a wallet to the requested archive state, writing nothing if it is there.

        **Both ways of archiving a wallet go through this, and that is the point.**
        `DELETE /api/wallets/{id}` and `PATCH {"archived": true}` are one logical
        operation, and they used to disagree: `DELETE` returned early on an
        already-archived row while `PATCH` set `archived_at` unconditionally, so
        repeating the first changed nothing and repeating the second silently moved the
        retirement date. Two answers to one question, decided by which endpoint the
        caller happened to reach for.

        The surviving behaviour is the one `archive_wallet` argued for. `archived_at` is
        not a flag spelled as a timestamp -- it records *when* an address stopped being
        watched, and the balance history is going to ask it that. A request that asks for
        no change must therefore write nothing at all, including `updated_at`: bumping
        that would make a no-op edit look like an edit to anything ordering by it.

        `label` deliberately does not get the same treatment. Re-sending an identical
        label does move `updated_at`, because a label carries no history -- there is no
        second column recording when it was set -- so "you asked, it was applied" is a
        complete account of that request. The asymmetry is between a column that is
        history and a column that is a value, not an inconsistency.
        """
        if archived == (wallet.archived_at is not None):
            return
        await self._wallets.set_archived_at(wallet, now if archived else None, now)

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
