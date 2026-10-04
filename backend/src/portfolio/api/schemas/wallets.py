"""Request and response models for the wallet registry.

**These validate shape, not correctness.** Present, within a length bound, and a chain key
the application knows -- that is all. Whether a string is a real address is decided by
`domain.chains`, because the CLI and any future importer have to reach the same verdict
without going through Pydantic, and because "the checksum does not match" is a domain
answer rather than a field-format one.

`address` on the way out is the **display** form. The canonical form is an implementation
detail of the uniqueness rule and is deliberately not published: a response carrying both
would invite a client to pick the wrong one, and the only caller that needs the canonical
form is the chain provider, which is on this side of the wire.

`chain_key` is typed as `ChainKey` on the way in, so the OpenAPI document carries the
enumeration and the generated TypeScript is a union of string literals rather than a bare
`string`. An unknown value is then a 422 from the schema, which is the same status the
domain would have produced for it.

**An extended public key travels in `address` too** (spec 031): on the way in it is the key,
and on the way out `kind` says which of the two the wallet holds. For `extended_key`,
`address` is masked by the service (R8) before it reaches this module, so no response model
here can serve a key in full, whichever router renders it. `kind` is typed as `WalletKind`
for the reason `chain_key` is typed on the way in: the client gets a union of literals.

**A private key is refused here before the length bound** (spec 031, R2b). Pydantic applies
`max_length` before the router ever sees the value, so an over-long paste carrying a private
key would otherwise come back as `string_too_long`, and the owner would be told to shorten
something they must never have entered at all. The test is the domain's own
`looks_like_private_key`, so this and `classify_wallet_key` cannot disagree, and the 422 has
the same `loc`, `type` and fixed sentence as the router's. The validator only refuses; it
never changes the value, and it adds nothing to the OpenAPI document.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from portfolio.domain.addresses import MAX_ADDRESS_LENGTH, REJECTION_MESSAGES, AddressRejection
from portfolio.domain.chains import ChainKey, WalletKind
from portfolio.domain.extended_keys import looks_like_private_key
from portfolio.services.wallets import MAX_LABEL_LENGTH

if TYPE_CHECKING:
    from portfolio.services.wallets import WalletView

LABEL_FIELD_NAME = "label"
"""The name the router checks against `model_fields_set` to tell an omitted label from an
explicit null. Named once so the string and the field cannot drift apart."""


class WalletCreateRequest(BaseModel):
    """A new wallet: which chain, which address or extended key, and optionally a label."""

    model_config = ConfigDict(extra="forbid")

    chain_key: ChainKey
    address: str = Field(
        min_length=1,
        max_length=MAX_ADDRESS_LENGTH,
        description=(
            "An address, or on Bitcoin a single-signature extended public key "
            "(xpub, ypub, zpub, tpub, upub or vpub). Never a private key."
        ),
    )
    label: str | None = Field(default=None, max_length=MAX_LABEL_LENGTH)

    @field_validator("address", mode="before")
    @classmethod
    def _refuse_private_key(cls, value: object) -> object:
        """Name a private key as one, ahead of the length bound (R2b).

        A `before` validator runs ahead of the field's own constraints. Anything that is not
        a string is passed through untouched, for Pydantic's type check to refuse.
        """
        if isinstance(value, str) and looks_like_private_key(value):
            raise PydanticCustomError(
                AddressRejection.PRIVATE_KEY.value,
                REJECTION_MESSAGES[AddressRejection.PRIVATE_KEY],
            )
        return value


class WalletUpdateRequest(BaseModel):
    """A change to a wallet. Both fields are optional, and absent is not the same as null.

    Omitting `label` leaves the label alone; sending `"label": null` clears it. The router
    tells the two apart through `model_fields_set`, which is the only reason this is not
    simply a nullable field with a default.

    `archived` is the simpler case: omitted and null both mean "leave it as it is", because
    a three-valued boolean has no third meaning worth having.
    """

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=MAX_LABEL_LENGTH)
    archived: bool | None = None


class WalletResponse(BaseModel):
    """One wallet, as the API publishes it."""

    id: int
    chain_key: str
    kind: WalletKind = Field(
        description="What the wallet was registered with: one address, or an extended public key."
    )
    address: str = Field(
        description=(
            "The address as it was entered. For an extended public key, only its first and "
            "last four characters around an ellipsis: the key itself is never served."
        )
    )
    label: str | None
    archived: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, view: WalletView) -> WalletResponse:
        """Render a service view. The canonical address is deliberately not among these."""
        return cls(
            id=view.id,
            chain_key=view.chain_key,
            kind=view.kind,
            address=view.address,
            label=view.label,
            archived=view.archived,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class WalletListResponse(BaseModel):
    """The wallet collection, wrapped in an object rather than returned as a bare array.

    A top-level array has nowhere to grow: adding a count or a cursor later would break
    every client, and an object costs one key now.
    """

    wallets: list[WalletResponse]
