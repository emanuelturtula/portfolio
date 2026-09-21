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
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from portfolio.domain.addresses import MAX_ADDRESS_LENGTH
from portfolio.domain.chains import ChainKey
from portfolio.services.wallets import MAX_LABEL_LENGTH

if TYPE_CHECKING:
    from portfolio.services.wallets import WalletView

LABEL_FIELD_NAME = "label"
"""The name the router checks against `model_fields_set` to tell an omitted label from an
explicit null. Named once so the string and the field cannot drift apart."""


class WalletCreateRequest(BaseModel):
    """A new wallet: which chain, which address, and optionally what to call it."""

    model_config = ConfigDict(extra="forbid")

    chain_key: ChainKey
    address: str = Field(min_length=1, max_length=MAX_ADDRESS_LENGTH)
    label: str | None = Field(default=None, max_length=MAX_LABEL_LENGTH)


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
    address: str
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
