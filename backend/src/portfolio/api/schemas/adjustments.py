"""Request and response models for the manual adjustments (#18).

**These validate shape, not the rules.** A string for each amount, a datetime, text -- that is
all. Whether an amount is one the engine can replay, whether a symbol is spelled as a venue
spells it, whether a note says anything: `services.adjustments.validate_draft` decides, by
building the engine's own `Adjustment`, so that the API and the recompute cannot disagree about
what an acceptable adjustment is (spec 023, *Validation*).

**Two limits are stated here, and neither is enforced here** (spec 023, R6). `note` carries
`maxLength` and `asset` carries `pattern` as `json_schema_extra`: metadata in the OpenAPI
document, taken from the service's own constants, that a form can read (#111). Pydantic does
not validate `json_schema_extra`, so the service stays the only validator of both and keeps
its field-specific messages -- the one for `asset` tells the owner to use the exchange's
spelling, which Pydantic's "String should match pattern" would not.

## Money arrives as a JSON string, and only as one

`MoneyStr` already refuses a JSON float, which is inexact before anything reads it. An
adjustment's amounts refuse **every** JSON number, integers included (spec 023, criterion 9
and R5): the owner's figures cross the wire one way, and a client that sends `1` today sends
`0.1` tomorrow. `MoneyInput` is `MoneyStr` with that refusal in front of it. On the way out
every amount is a string at eighteen places, as `GET /api/accounting/positions` sends them.

## `occurred_at` is a string too

Pydantic reads a JSON number for a `datetime` as a Unix timestamp, and accepts it. The contract
is an ISO 8601 string with an offset (spec 023, R5), so `InstantInput` refuses a number before
Pydantic sees it. What a string must then be -- aware, representable in UTC, not later than
now -- is the service's to decide, as for every other rule.

## `unit_cost: null` is unknown, never zero

Required on `PUT` -- present, and possibly `null` -- because a replacement that omitted it could
not say whether the cost became unknown or was forgotten. Optional on `POST`, where omitting it
means unknown. The field descriptions say what unknown does to the positions endpoint, because
that is the consequence an owner has to choose knowingly.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from portfolio.api.schemas.money import MoneyStr
from portfolio.services.adjustments import (
    ASSET_SYMBOL_PATTERN,
    NOTE_MAX_LENGTH,
    AdjustmentDraft,
)

if TYPE_CHECKING:
    from portfolio.services.adjustments import AdjustmentView

JSON_NUMBER_REFUSAL: Final = "a monetary value must arrive as a JSON string"
"""The refusal of a JSON number for an amount. Pydantic prefixes it with `Value error, `."""

INSTANT_NUMBER_REFUSAL: Final = (
    "occurred_at must arrive as an ISO 8601 string with a timezone, not as a JSON number"
)
"""The refusal of a JSON number for `occurred_at`. Pydantic prefixes it with `Value error, `."""


def _require_a_json_string(value: object) -> object:
    """Refuse a JSON number -- an integer as well as a float -- or a boolean, for an amount.

    A `bool` is an `int`, so one test covers all three. Anything else goes on to `MoneyStr`,
    which parses a string and refuses whatever does not parse, without quoting it.
    """
    if isinstance(value, int | float):
        raise ValueError(JSON_NUMBER_REFUSAL)
    return value


def _require_an_iso_string(value: object) -> object:
    """Refuse a JSON number -- or a boolean -- for `occurred_at`, before Pydantic reads it.

    Without this, `1767225600` is accepted as a Unix timestamp. Anything else goes on to
    Pydantic's `datetime`, which parses a string -- or passes a `datetime` built in Python --
    and refuses whatever does not parse, without quoting it.
    """
    if isinstance(value, int | float):
        raise ValueError(INSTANT_NUMBER_REFUSAL)
    return value


MoneyInput = Annotated[MoneyStr, BeforeValidator(_require_a_json_string)]
"""An amount in a request body: a JSON string and nothing else, then `MoneyStr`'s checks.

The validator is last in the annotation, and Pydantic runs before-validators last-first, so it
sees the raw JSON value before `MoneyStr`'s own."""

InstantInput = Annotated[datetime, BeforeValidator(_require_an_iso_string)]
"""`occurred_at` in a request body: a string, never a Unix timestamp (spec 023, R5)."""

_ASSET_DESCRIPTION: Final = (
    "The symbol exactly as the exchanges spell it: 1 to 20 upper-case letters or digits, such "
    "as `BTC`. A lower-case symbol is refused rather than corrected, and so are the cash "
    "assets USDC and USDT."
)
_QUANTITY_DESCRIPTION: Final = (
    "How much was acquired, above zero, as a JSON string. At most 18 decimal places and 20 "
    "digits before the point."
)
_UNIT_COST_DESCRIPTION: Final = (
    "USD per unit, zero or more, as a JSON string, or `null`. **`null` is an unknown cost, not "
    "zero**: the quantity then counts toward the position but not toward its cost, and the "
    "asset shows the `unknown_basis` flag and the quantity in `unknown_basis_quantity` on "
    "`GET /api/accounting/positions`. Zero is a known cost of nothing. Unit cost times quantity "
    "must have at most 20 digits before the point."
)
_OCCURRED_AT_DESCRIPTION: Final = (
    "When the coins were acquired: an ISO 8601 datetime with an offset, not later than now. It "
    "places the adjustment among the exchange fills, and an adjustment at the same instant as a "
    "fill replays after it -- so date an opening balance **before** the first sale it covers."
)
_NOTE_DESCRIPTION: Final = (
    f"Why, in your words. Required, not blank, at most {NOTE_MAX_LENGTH} characters, and "
    "stored as given."
)


class _AdjustmentRequest(BaseModel):
    """The fields every adjustment request carries, and the draft the service validates."""

    model_config = ConfigDict(extra="forbid")

    # `pattern` and `maxLength` below are OpenAPI metadata only, never validated here: see the
    # module docstring. `json_schema_extra` is the one `Field` argument Pydantic does not check.
    asset: str = Field(
        description=_ASSET_DESCRIPTION,
        json_schema_extra={"pattern": ASSET_SYMBOL_PATTERN},
    )
    quantity: MoneyInput = Field(description=_QUANTITY_DESCRIPTION)
    # Declared again on each subclass, which is where whether it may be omitted differs. Here
    # it fixes the field's place in the order: after `quantity`, as the wire shows it.
    unit_cost: MoneyInput | None = Field(description=_UNIT_COST_DESCRIPTION)
    occurred_at: InstantInput = Field(description=_OCCURRED_AT_DESCRIPTION)
    note: str = Field(
        description=_NOTE_DESCRIPTION,
        json_schema_extra={"maxLength": NOTE_MAX_LENGTH},
    )

    def to_draft(self) -> AdjustmentDraft:
        """The five fields as the service takes them. Nothing is changed on the way."""
        return AdjustmentDraft(
            asset=self.asset,
            quantity=self.quantity,
            unit_cost=self.unit_cost,
            occurred_at=self.occurred_at,
            note=self.note,
        )


class AdjustmentCreateRequest(_AdjustmentRequest):
    """A new adjustment. `unit_cost` may be omitted, which records an unknown cost."""

    unit_cost: MoneyInput | None = Field(default=None, description=_UNIT_COST_DESCRIPTION)


class AdjustmentReplaceRequest(_AdjustmentRequest):
    """A full replacement of an adjustment's five fields.

    `unit_cost` is **required** and may be `null`: a replacement states the cost, known or
    unknown, rather than leaving it to be guessed from an omission.
    """

    unit_cost: MoneyInput | None = Field(description=_UNIT_COST_DESCRIPTION)


class AdjustmentResponse(BaseModel):
    """One adjustment, as stored: amounts as strings at 18 places, instants in UTC."""

    id: int
    asset: str
    quantity: MoneyStr
    unit_cost: MoneyStr | None = Field(
        description="USD per unit. `null` is an unknown cost, not zero: the asset shows "
        "`unknown_basis` on `GET /api/accounting/positions`."
    )
    occurred_at: datetime
    note: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, view: AdjustmentView) -> AdjustmentResponse:
        """Render a service view."""
        return cls(
            id=view.id,
            asset=view.asset,
            quantity=view.quantity,
            unit_cost=view.unit_cost,
            occurred_at=view.occurred_at,
            note=view.note,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class AdjustmentListResponse(BaseModel):
    """The owner's adjustments, by `occurred_at` and then id, wrapped in an object.

    An object rather than a bare array, for the reason `WalletListResponse` gives.
    """

    adjustments: list[AdjustmentResponse]
