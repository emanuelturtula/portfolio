"""Manual adjustments: the inflows the owner records that no venue's history shows (#18).

The venues keep a window of history, so the owner holds coins bought before the imported
history begins, and selling them oversells the pool (spec 023, *Problem*). An adjustment is the
engine's `Adjustment` event, kept in `manual_adjustments` and entered through the API. This
module is the policy around it: what an acceptable adjustment is, and what changing one sets
off.

## The engine is the authority on the rules

An adjustment is validated by **building the engine's own `EventKey` and `Adjustment`** from
the draft and turning the refusal into an `InvalidAdjustmentError` -- never by a second copy of
the amount rule, the sign rules or the total-cost rule. `services.accounting.adjustment_of`
builds the same two objects from the stored row when the snapshot is recomputed, so the rule
an adjustment was accepted under is the rule it is replayed under, and the two cannot drift
apart (the lesson of #99, spec 020). `validate_draft` builds them in steps -- the key, then the
adjustment without its cost, then with it -- so that each refusal is attributed to the field
that caused it without parsing a message.

The rules this module adds are the ones the engine has no opinion on:

* **`asset` is the venue's symbol**, `^[A-Z0-9]{1,20}$`, and a lower-case one is refused, not
  upper-cased: changing what the owner typed is the wrong fix, and a symbol spelled another way
  is a separate pool. A cash asset is refused too: it is the unit of account, and an
  adjustment of it changes nothing, so entering one is a mistake.
* **`occurred_at` is not in the future**, on the injected clock.
* **`note` is required**: not blank, at most `NOTE_MAX_LENGTH` characters, UTF-8-encodable, and
  stored exactly as given.

The checks run in that order -- asset, occurred_at, quantity, unit_cost, note -- and the first
failure is the one reported. **No refusal carries a value**: each names the field and the rule.

## Every change recomputes, after it is committed

`create`, `update` and `delete` commit their own write, log the id, and then await
`after_change`, which the request dependency binds to the recompute trigger in `main.py`. The
response is therefore sent after the snapshot reflects the change. The trigger never raises;
if a caller's `after_change` does anyway, the change stays saved and the failure is logged by
class name, because a 500 for a change that was saved would be the one wrong answer.

## Nothing the owner typed is logged

`adjustment_created`, `adjustment_updated` and `adjustment_deleted` carry the id and nothing
else: not the asset, not an amount, not a date, and never the note, which is free text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Final

import structlog

from portfolio.db.models import ADJUSTMENT_SCALE
from portfolio.domain.accounting import DEFAULT_CASH_ASSETS, Adjustment, EventKey
from portfolio.domain.money import quantize
from portfolio.repositories.adjustments import ManualAdjustmentRepository
from portfolio.services.accounting import (
    ADJUSTMENT_SOURCE,
    UnconvertibleAdjustmentError,
    adjustment_of,
    external_id_of,
    utc_now,
)

if TYPE_CHECKING:
    import builtins
    from collections.abc import Awaitable, Callable
    from datetime import datetime
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.db.models import ManualAdjustment

__all__ = [
    "ADJUSTMENT_NOT_FOUND_DETAIL",
    "ADJUSTMENT_SOURCE",
    "ASSET_SYMBOL_PATTERN",
    "ASSET_SYMBOL_RULE",
    "CASH_ASSET_RULE",
    "NOTE_BLANK_RULE",
    "NOTE_ENCODING_RULE",
    "NOTE_MAX_LENGTH",
    "NOTE_TOO_LONG_RULE",
    "OCCURRED_IN_FUTURE_RULE",
    "AdjustmentDraft",
    "AdjustmentError",
    "AdjustmentNotFoundError",
    "AdjustmentService",
    "AdjustmentView",
    "AfterChange",
    "InvalidAdjustmentError",
    "UnconvertibleAdjustmentError",
    "adjustment_of",
    "build_adjustment_service",
    "external_id_of",
    "validate_draft",
    "view_of",
]
"""`ADJUSTMENT_SOURCE`, `external_id_of`, `adjustment_of` and `UnconvertibleAdjustmentError` are
**re-exported** from `services/accounting.py`, which defines them beside `trade_of`: the
recompute there is their first caller, and defining them here would make the two modules import
each other."""

_logger = structlog.get_logger(__name__)

NOTE_MAX_LENGTH: Final = 500
"""The most characters a note may have, counted as `len()` counts them, on the note as given."""

ADJUSTMENT_NOT_FOUND_DETAIL: Final = "No adjustment with that id."
"""The 404's detail, for a missing id and for another owner's alike: it says neither."""

ASSET_SYMBOL_PATTERN: Final = r"^[A-Z0-9]{1,20}$"
"""What an asset symbol is: 1 to 20 upper-case ASCII letters or digits, and nothing else.

Public because the OpenAPI schema states it as `pattern` metadata for the UI (spec 023, R6);
this module is still the only thing that validates it. Anchored, so that it means the same in
JSON Schema's unanchored ECMA-262 dialect as under `re.fullmatch` here, where a trailing newline
does not slip past the `$`. ASCII only, so it also refuses any text that is not
UTF-8-encodable, which the engine would refuse too.
"""

_ASSET_SYMBOL: Final = re.compile(ASSET_SYMBOL_PATTERN)

ASSET_SYMBOL_RULE: Final = (
    "asset must be the symbol exactly as the exchange spells it: 1 to 20 upper-case letters "
    "or digits, such as BTC"
)
CASH_ASSET_RULE: Final = (
    f"asset must not be a cash asset ({', '.join(sorted(DEFAULT_CASH_ASSETS))}): cash is the "
    "unit of account, and an adjustment of it changes nothing"
)
OCCURRED_IN_FUTURE_RULE: Final = "occurred_at must not be later than now"
NOTE_BLANK_RULE: Final = "note must not be blank"
NOTE_TOO_LONG_RULE: Final = f"note must be at most {NOTE_MAX_LENGTH} characters"
NOTE_ENCODING_RULE: Final = "note must be text that encodes as UTF-8"
"""The refusals this module owns. The engine's own -- signs, scale, range, the total cost, an
aware and representable instant -- are its messages, with `Adjustment.` and `EventKey.`
removed so that they name the request's fields. None of them quotes a value."""

_ENGINE_QUALIFIERS: Final = ("Adjustment.", "EventKey.")
"""The class prefixes the engine writes its field names with. The request's fields share the
names after them -- `asset`, `quantity`, `unit_cost`, `occurred_at` -- which is what lets an
engine refusal be shown as a refusal of the field the owner sent."""

_VALIDATION_ID: Final = 0
"""The id the key is built with while a new adjustment is validated, before it has one.

The id only becomes `external_id`, which the engine requires to be non-blank UTF-8 text, and
`external_id_of` gives twenty digits for every id; so the rule it is held to is the same for
this id as for the one the insert assigns.
"""

type AfterChange = Callable[[], Awaitable[object]]
"""What the service awaits after it commits a change: the recompute, bound to its reason."""


class AdjustmentError(Exception):
    """Base class for every failure this service raises on purpose."""


class AdjustmentNotFoundError(AdjustmentError):
    """No adjustment with that id belongs to this owner."""

    def __init__(self) -> None:
        """A fixed detail: neither the id nor whether it exists for somebody else."""
        super().__init__(ADJUSTMENT_NOT_FOUND_DETAIL)


class InvalidAdjustmentError(AdjustmentError):
    """An adjustment refused at entry: which field, and which rule it broke. **Never the value.**

    `field` is the request's field name and `rule` a sentence stating the rule, which is also
    the message. The API renders it as a field-level 422, as it renders a rejected address.
    """

    def __init__(self, field: str, rule: str) -> None:
        """Record the field and the rule; the rule is the message."""
        super().__init__(rule)
        self.field = field
        self.rule = rule

    def __reduce__(
        self,
    ) -> tuple[type[InvalidAdjustmentError], tuple[str, str], dict[str, object]]:
        """Rebuild from both arguments, which `self.args` alone does not hold."""
        return (type(self), (self.field, self.rule), dict(self.__dict__))


@dataclass(frozen=True, slots=True)
class AdjustmentDraft:
    """The five fields an owner enters, as a create or a full replacement sends them.

    `unit_cost` is USD per unit, and `None` is an unknown cost, which is not zero. Nothing here
    is validated; `validate_draft` is what does that.
    """

    asset: str
    quantity: Decimal
    unit_cost: Decimal | None
    occurred_at: datetime
    note: str


@dataclass(frozen=True, slots=True)
class AdjustmentView:
    """An adjustment as everything above this layer sees it, **as it is stored**.

    A snapshot rather than the ORM row, for the reason `WalletView` gives. The amounts are at
    the column's eighteen places and `occurred_at` is in UTC, whatever offset the owner wrote
    it with, so the answer to a create is the answer a later read gives.
    """

    id: int
    asset: str
    quantity: Decimal
    unit_cost: Decimal | None
    occurred_at: datetime
    note: str
    created_at: datetime
    updated_at: datetime


def _as_stored(amount: Decimal) -> Decimal:
    """An amount as `NumericText(ADJUSTMENT_SCALE)` stores it: at its scale, and one zero.

    Exact for every amount `validate_draft` accepts, which has at most that many places.
    """
    stored = quantize(amount, ADJUSTMENT_SCALE)
    return abs(stored) if stored.is_zero() else stored


def view_of(row: ManualAdjustment) -> AdjustmentView:
    """Snapshot a row while its session is open, in the form the database holds it.

    A row just added or updated still carries the values as they were assigned; one read back
    carries them as stored. Both give the same view.
    """
    return AdjustmentView(
        id=row.id,
        asset=row.asset,
        quantity=_as_stored(row.quantity),
        unit_cost=None if row.unit_cost is None else _as_stored(row.unit_cost),
        occurred_at=row.occurred_at.astimezone(UTC),
        note=row.note,
        created_at=row.created_at.astimezone(UTC),
        updated_at=row.updated_at.astimezone(UTC),
    )


def _engine_rule(message: str) -> str:
    """An engine refusal's message, with its class prefixes removed so it names the field."""
    for qualifier in _ENGINE_QUALIFIERS:
        message = message.replace(qualifier, "")
    return message


def _checked[T](field: str, build: Callable[[], T]) -> T:
    """Build an engine object, and turn its refusal into this field's `InvalidAdjustmentError`.

    `TypeError` as well as `ValueError`: a draft built by a caller other than the API could
    carry a `float`, and the engine's refusal of it names the field and the type, not the value.
    """
    try:
        return build()
    except (TypeError, ValueError) as exc:
        raise InvalidAdjustmentError(field, _engine_rule(str(exc))) from exc


def validate_draft(draft: AdjustmentDraft, *, now: datetime) -> None:
    """Refuse a draft the engine could not replay, or that this module's rules forbid.

    In order, and the first failure is the one raised:

    1. `asset`: the venue's symbol, and not a cash asset.
    2. `occurred_at`: the engine's `EventKey` accepts it -- aware, and representable in UTC --
       and it is not later than `now`.
    3. `quantity`: the engine's `Adjustment` accepts it, with no cost.
    4. `unit_cost`, when given: the engine's `Adjustment` accepts it beside the quantity, which
       includes the total cost fitting (spec 019, R2).
    5. `note`: not blank after trimming, at most `NOTE_MAX_LENGTH` characters, UTF-8-encodable.

    Steps 2 to 4 build exactly what `adjustment_of` builds from the stored row, so a draft that
    passes converts once it is stored.

    Raises:
        InvalidAdjustmentError: the field and the rule; never the value.
    """
    if _ASSET_SYMBOL.fullmatch(draft.asset) is None:
        raise InvalidAdjustmentError("asset", ASSET_SYMBOL_RULE)
    if draft.asset in DEFAULT_CASH_ASSETS:
        raise InvalidAdjustmentError("asset", CASH_ASSET_RULE)
    key = _checked(
        "occurred_at",
        lambda: EventKey(
            occurred_at=draft.occurred_at,
            source=ADJUSTMENT_SOURCE,
            external_id=external_id_of(_VALIDATION_ID),
        ),
    )
    if key.occurred_at > now:
        raise InvalidAdjustmentError("occurred_at", OCCURRED_IN_FUTURE_RULE)
    _checked(
        "quantity",
        lambda: Adjustment(key=key, asset=draft.asset, quantity=draft.quantity, unit_cost=None),
    )
    if draft.unit_cost is not None:
        unit_cost = draft.unit_cost
        _checked(
            "unit_cost",
            lambda: Adjustment(
                key=key, asset=draft.asset, quantity=draft.quantity, unit_cost=unit_cost
            ),
        )
    _require_note(draft.note)


def _require_note(note: str) -> None:
    """Refuse a note that is blank, too long, or not text SQLite can store.

    Blank is Python's reading of whitespace, which is wider than the `trim()` the table's
    `CHECK` applies, so nothing accepted here is refused there. A lone surrogate is text a JSON
    body can carry and UTF-8 cannot encode; the insert would fail on it far from here.
    """
    if not note.strip():
        raise InvalidAdjustmentError("note", NOTE_BLANK_RULE)
    if len(note) > NOTE_MAX_LENGTH:
        raise InvalidAdjustmentError("note", NOTE_TOO_LONG_RULE)
    try:
        note.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidAdjustmentError("note", NOTE_ENCODING_RULE) from None


class AdjustmentService:
    """The unit of work for manual adjustments, and the trigger of the recompute after one.

    It owns the transaction: the repository flushes and this class commits. The caller -- the
    request dependency -- owns the session and closes it, so an exception before the commit
    leaves nothing half applied.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        adjustments: ManualAdjustmentRepository,
        after_change: AfterChange,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._adjustments = adjustments
        self._after_change = after_change
        self._clock = clock

    async def list(self, user_id: int) -> builtins.list[AdjustmentView]:
        """Every adjustment the owner has recorded, by `occurred_at` and then by id.

        Ordered here, on the datetimes, rather than in SQL on their text (spec 023, *Data
        model*). The id breaks a tie in the order the engine replays two adjustments at one
        instant, so the list reads in replay order.
        """
        rows = await self._adjustments.list_for_user(user_id)
        return sorted((view_of(row) for row in rows), key=lambda view: (view.occurred_at, view.id))

    async def create(self, user_id: int, draft: AdjustmentDraft) -> AdjustmentView:
        """Validate, store, commit, recompute, and return the adjustment as stored.

        Raises:
            InvalidAdjustmentError: the draft is refused. Nothing is written.
        """
        now = self._clock()
        validate_draft(draft, now=now)
        row = await self._adjustments.add(
            user_id=user_id,
            asset=draft.asset,
            quantity=draft.quantity,
            unit_cost=draft.unit_cost,
            occurred_at=draft.occurred_at,
            note=draft.note,
            created_at=now,
        )
        view = view_of(row)
        await self._session.commit()
        _logger.info("adjustment_created", adjustment_id=view.id)
        await self._changed(view.id)
        return view

    async def update(
        self,
        user_id: int,
        adjustment_id: int,
        draft: AdjustmentDraft,
    ) -> AdjustmentView:
        """Replace the five editable fields, commit, recompute, and return it as stored.

        A full replacement: `unit_cost=None` makes the cost unknown. The draft is validated
        **before** the lookup, so a refused body is refused whatever the id, and nothing about
        the id is learned from a request whose body was wrong.

        Raises:
            InvalidAdjustmentError: the draft is refused. Nothing is written.
            AdjustmentNotFoundError: no adjustment with that id belongs to the owner.
        """
        now = self._clock()
        validate_draft(draft, now=now)
        row = await self._require(user_id, adjustment_id)
        await self._adjustments.update(
            row,
            asset=draft.asset,
            quantity=draft.quantity,
            unit_cost=draft.unit_cost,
            occurred_at=draft.occurred_at,
            note=draft.note,
            updated_at=now,
        )
        view = view_of(row)
        await self._session.commit()
        _logger.info("adjustment_updated", adjustment_id=view.id)
        await self._changed(view.id)
        return view

    async def delete(self, user_id: int, adjustment_id: int) -> None:
        """Delete the adjustment, commit, and recompute. Not idempotent: a repeat is a 404.

        Raises:
            AdjustmentNotFoundError: no adjustment with that id belongs to the owner.
        """
        row = await self._require(user_id, adjustment_id)
        await self._adjustments.delete(row)
        await self._session.commit()
        _logger.info("adjustment_deleted", adjustment_id=adjustment_id)
        await self._changed(adjustment_id)

    async def _require(self, user_id: int, adjustment_id: int) -> ManualAdjustment:
        """The owner's adjustment, or the not-found that does not say whose it was."""
        row = await self._adjustments.get(user_id, adjustment_id)
        if row is None:
            raise AdjustmentNotFoundError
        return row

    async def _changed(self, adjustment_id: int) -> None:
        """Await `after_change` once the change is committed. **Never raises an `Exception`.**

        The change is saved by now, and the recompute bound here records its own failure where
        `GET /api/accounting/positions` shows it. Anything that still escapes is logged by class
        name, with the id -- never its message -- and the caller is answered with the change it
        made. A cancellation propagates.
        """
        try:
            await self._after_change()
        except Exception as exc:  # the change is saved; the failure is logged, not raised
            _logger.error(
                "adjustment_after_change_failed",
                adjustment_id=adjustment_id,
                error=type(exc).__name__,
            )


def build_adjustment_service(
    session: AsyncSession,
    *,
    after_change: AfterChange,
    clock: Callable[[], datetime] = utc_now,
) -> AdjustmentService:
    """Assemble the service over one session, with what to run after each change.

    `after_change` is a parameter rather than something this module finds, because the trigger
    lives in `main.py`, which nothing in `services` may import; the request dependency binds it.
    The clock is injectable so that a test can name "now" for the future-date rule.
    """
    return AdjustmentService(
        session=session,
        adjustments=ManualAdjustmentRepository(session),
        after_change=after_change,
        clock=clock,
    )
