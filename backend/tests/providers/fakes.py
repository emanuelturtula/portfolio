"""The fake chain provider, and the one line that is criterion 7.

`_CONFORMS: ChainProvider = FakeChainProvider()` at module level is the whole of the
static check. It costs nothing at runtime -- an assignment of an instance to a typed name
-- and the gate's `mypy --strict` over `tests` decides whether `FakeChainProvider` is
actually assignable to the protocol. If a member is missing, takes the wrong arguments, or
is a plain `def` where the protocol says `async def`, that line fails to type check.

**Why not `isinstance`.** `ChainProvider` is deliberately not `@runtime_checkable`, and
the spec says why: `isinstance` against a runtime-checkable protocol compares attribute
*names* and nothing else, so a class whose `fetch_balances` takes the wrong arguments or
is not a coroutine function passes it. That is a verifier that can only confirm its own
account. `tests/providers/test_protocol.py::test_mypy_rejects_a_provider_with_the_wrong_signature`
proves the static check here is one that can actually fail.

**Why the fake lives in `tests/` and not in `providers/chains/`.** Two reasons, and each
is sufficient: criterion 6 asserts every module in `providers/chains/` is registered, and a
test-only class placed there would either have to be registered -- shipping a fake provider
in the production image -- or would fail that scan forever.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from portfolio.domain.chains import ChainKey, validate_address
from portfolio.providers.base import (
    AddressBalance,
    ChainCapabilities,
    ChainProvider,
    ProviderHealth,
    align_balances,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from portfolio.domain.chains import ValidatedAddress

#: Bitcoin's exponent. The fake answers about Bitcoin because `tests/address_vectors.py`
#: has the richest set of testnet vectors for it, and because a fake that declared a chain
#: nothing validates could not exercise `validate_address` at all.
FAKE_DECIMALS: Final = 8

#: Above one, so `can_batch` is true and `chunk_addresses` has something to chunk. A fake
#: that could not batch would leave the batching half of criterion 2 with no exercise.
FAKE_MAX_ADDRESSES_PER_CALL: Final = 3


class FakeChainProvider:
    """A provider that answers from a dictionary and never opens a socket.

    It implements the protocol structurally -- it does not inherit from it -- which is the
    property an ABC would have taken away and the reason the spec chose a `Protocol`.
    """

    def __init__(self, balances: Mapping[str, int] | None = None) -> None:
        self._balances: dict[str, int] = dict(balances or {})
        #: Every call, in order, so a caller's chunking can be asserted against what the
        #: provider was actually asked rather than against what it was supposed to ask.
        self.calls: list[tuple[str, ...]] = []
        self._capabilities = ChainCapabilities(
            chain_key=ChainKey.BITCOIN,
            decimals=FAKE_DECIMALS,
            max_addresses_per_call=FAKE_MAX_ADDRESSES_PER_CALL,
        )

    @property
    def capabilities(self) -> ChainCapabilities:
        return self._capabilities

    def validate_address(self, raw: str) -> ValidatedAddress:
        """Delegate, so the fake cannot disagree with the domain about what is valid."""
        return validate_address(self._capabilities.chain_key, raw)

    async def fetch_balances(self, addresses: Sequence[str]) -> Sequence[AddressBalance]:
        """Answer about exactly what was asked, through the shared alignment rule."""
        self.calls.append(tuple(addresses))
        found = {
            address: self._balances[address] for address in addresses if address in self._balances
        }
        return align_balances(addresses, found, decimals=self._capabilities.decimals)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(chain_key=self._capabilities.chain_key, healthy=True, detail=None)


_CONFORMS: ChainProvider = FakeChainProvider()
"""Criterion 7. Do not delete, and do not replace with an `isinstance` assertion.

This is not dead code: `mypy --strict` is the assertion, and it runs in the gate. Ruff
would otherwise be within its rights to call an unused module-level name pointless, which
is exactly why the name is not underscore-prefixed by accident -- it is private because
nothing should import it, and it exists because something has to type check it.
"""
