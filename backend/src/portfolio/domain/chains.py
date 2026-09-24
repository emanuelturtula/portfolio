"""The chain registry: which chains exist, and which codec validates an address for each.

A narrow, pure interface. A caller names a chain and hands over a string; it gets back the
canonical and display forms, or an `AddressInvalidError` naming the reason.

**This lives in `domain` rather than in `providers` deliberately.** A provider is an I/O
boundary that a service may call, and a pure function placed there would make the one
guarantee this module exists to give -- *validating an address never costs a network round
trip* -- a matter of discipline rather than of layering. `domain` imports nothing, so the
guarantee is structural: there is no socket to reach for from here.

When the chain **provider** protocol lands it keys off these same `ChainKey` values. The
two registries are siblings: one says what an address for a chain looks like, the other
says how to ask that chain a question. Neither wraps the other.

The alternative was a Pydantic field validator on the request schema. It reads well and it
puts the rule in the wrong place: the CLI and any future importer would each need their
own copy, and the failure would be a Pydantic message rather than a domain one. The schema
validates *shape* -- present, not absurdly long, a known chain key. This module validates
*correctness*.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

from portfolio.domain.addresses import (
    MAX_ADDRESS_LENGTH,
    AddressInvalidError,
    AddressRejection,
    ValidatedAddress,
    validate_bitcoin_address,
    validate_kaspa_address,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = [
    "CHAIN_ASSET_SYMBOLS",
    "CHAIN_VALIDATORS",
    "AddressInvalidError",
    "AddressRejection",
    "ChainKey",
    "ValidatedAddress",
    "validate_address",
]


class ChainKey(StrEnum):
    """Every chain balances can be read from.

    A `StrEnum` so that the value stored in `wallets.chain_key`, the value the `CHECK`
    constraint admits and the value that crosses the API are one string rather than three
    that have to be kept in step. Adding a member is therefore also a migration.
    """

    BITCOIN = "bitcoin"
    KASPA = "kaspa"

    @property
    def asset_symbol(self) -> str:
        """The ticker of the asset this chain's native balance is denominated in.

        **This is here rather than in `providers/prices/base.py`, and the move is the
        point.** `BTC` and `KAS` are declared there too, as the symbols a price source
        builds a vendor pair code out of, and reaching for those from the balance read path
        would make `api.routers -> services.balances -> providers.prices` a real edge --
        which `backend/.importlinter`'s `prices-are-never-fetched-in-a-request` contract
        forbids, deliberately and without `allow_indirect_imports`. That contract has never
        failed on a real chain, and the obvious implementation of the valuation read would
        have been the first thing to fail it, correctly.

        The symbol is a property of the chain rather than of the price package: Bitcoin's
        native asset is BTC whether or not anything is pricing it today. So it lives in the
        layer that imports nothing, and a test asserts the two declarations agree -- the
        same arrangement that holds `_ASSET_KIND_CHECK` and its migration together.

        A property rather than a second enum member value, because a `StrEnum` member *is*
        its chain key and giving it a tuple value would change what
        `wallets.chain_key == ChainKey.BITCOIN` compares.
        """
        return CHAIN_ASSET_SYMBOLS[self]


# A PEP 695 alias rather than an assignment: its right-hand side is evaluated lazily, so
# `Callable` can stay in the type-checking block where ruff's TC rules want it.
type AddressValidator = Callable[[str], ValidatedAddress]

CHAIN_ASSET_SYMBOLS: Final[Mapping[ChainKey, str]] = {
    ChainKey.BITCOIN: "BTC",
    ChainKey.KASPA: "KAS",
}
"""One asset symbol per chain, spelled exactly as `assets.symbol` holds it.

Module level rather than inside `ChainKey.asset_symbol`, so that a test can assert the
mapping is total over `ChainKey` without constructing every member -- the same treatment
`CHAIN_VALIDATORS` gets, and for the same reason: a chain added without an entry must fail
the build rather than raise a `KeyError` on the first sync after it ships.
"""

CHAIN_VALIDATORS: Final[Mapping[ChainKey, AddressValidator]] = {
    ChainKey.BITCOIN: validate_bitcoin_address,
    ChainKey.KASPA: validate_kaspa_address,
}
"""One validator per chain, and the mapping is total by construction.

A test asserts every `ChainKey` member has an entry, so a chain added without a codec fails
the build rather than raising a `KeyError` on the first address anyone enters.
"""


def validate_address(chain_key: str, raw: str) -> ValidatedAddress:
    """Validate an address for a chain, offline.

    Takes a plain `str` for the chain key rather than a `ChainKey`, so that an unknown key
    is one of this function's ordinary rejections instead of a `ValueError` the caller has
    to remember to catch separately. That is what lets a router hand over whatever arrived
    in the request body and deal with exactly one exception type.

    Surrounding whitespace is stripped first: an address pasted out of a wallet arrives
    with a trailing newline more often than not, and refusing that would be refusing the
    address the owner actually copied.

    Args:
        chain_key: the chain the address is claimed to belong to.
        raw: the address as the owner entered it.

    Returns:
        The canonical form, for uniqueness and for provider calls, and the display form.

    Raises:
        AddressInvalidError: unknown chain, empty, over the length ceiling, or refused by
            the chain's own codec. `.reason` says which; neither the exception's message
            nor its arguments ever contain `raw`.
    """
    try:
        chain = ChainKey(chain_key)
    except ValueError:
        raise AddressInvalidError(AddressRejection.UNKNOWN_CHAIN) from None

    candidate = raw.strip()
    if not candidate:
        raise AddressInvalidError(AddressRejection.EMPTY)
    if len(candidate) > MAX_ADDRESS_LENGTH:
        raise AddressInvalidError(AddressRejection.TOO_LONG)
    return CHAIN_VALIDATORS[chain](candidate)
