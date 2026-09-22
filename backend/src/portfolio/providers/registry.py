"""Which provider answers for which chain, decided by an explicit decorator.

A service holds a `ChainKey` -- it came out of `wallets.chain_key` -- and needs the thing
that can read balances for it. This is the lookup, and it is deliberately dull: a dict, a
decorator that fills it, and a typed error when the key is not there.

## Why a class with a module-level instance, rather than module globals

`ChainProviderRegistry` is a class so that a test can build its own empty one and exercise
every path -- registration, duplicate registration, lookup, unknown key -- without touching
the registry the application uses and without a fixture that puts a global back afterwards.
**A test that has to restore shared state is a test that shares state with the thing it is
verifying**, and the restore is the part that gets forgotten in the rewrite three months
later.

`CHAIN_PROVIDERS` is the process-wide instance, and `register_chain_provider` and
`get_chain_provider` are the two spellings a provider module and a service actually use.

## Why a class object is the factory

`@register_chain_provider(ChainKey.X)` decorates a provider class and returns it unchanged.
Nothing is wrapped, so the class is still importable, still subclassable and still exactly
what `mypy` sees. A class whose `__init__` takes the shared `httpx.AsyncClient` already
*is* a `Callable[[httpx.AsyncClient], ChainProvider]`, so there is no factory function to
write and no second place a construction argument could be added.

## Why there is no auto-discovery

No `pkgutil.walk_packages`. A provider that was written but never imported should fail as
an obvious `UnknownChainError` at the call site, naming what *is* registered -- not as a
mysteriously empty balance, or a 404 from an endpoint three layers up, discovered by a user.
The cost is one import line per provider in `providers/chains/__init__.py`, and a test
asserts that every module in that package is registered, so the line cannot be forgotten
quietly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from portfolio.domain.chains import ChainKey
from portfolio.providers.errors import DuplicateProviderError, UnknownChainError

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from portfolio.providers.base import ChainProvider

__all__ = [
    "CHAIN_PROVIDERS",
    "ChainProviderFactory",
    "ChainProviderRegistry",
    "get_chain_provider",
    "register_chain_provider",
]


type ChainProviderFactory = Callable[[httpx.AsyncClient], ChainProvider]
"""Anything that builds a provider over the shared client -- in practice, a provider class.

A PEP 695 alias rather than an assignment: its right-hand side is evaluated lazily, so
`httpx` and `Callable` can stay in the type-checking block and importing this module does
not drag `httpx` in with it.
"""


class ChainProviderRegistry:
    """A mapping from `ChainKey` to the thing that can read that chain.

    Holds factories rather than instances, because a provider is bound to an
    `httpx.AsyncClient` and the client's lifetime belongs to the application, not to this
    table. Registration happens at import; construction happens when somebody has a client
    to hand over.
    """

    def __init__(self) -> None:
        self._factories: dict[ChainKey, ChainProviderFactory] = {}

    def register[FactoryT: ChainProviderFactory](
        self,
        chain_key: ChainKey,
    ) -> Callable[[FactoryT], FactoryT]:
        """Claim `chain_key`, as a decorator that returns its argument unchanged.

        The generic parameter is what makes the decorated name keep its own type: a
        provider class decorated with this is still that class to `mypy`, not a
        `ChainProviderFactory`, so its own attributes and its constructor signature stay
        visible at every use site.

        Raises:
            DuplicateProviderError: something already claimed this key. Raised at import,
                so a copy-pasted decorator stops the process rather than shadowing the
                provider above it and reporting one chain's balances under another's name.
        """

        def claim(factory: FactoryT) -> FactoryT:
            if chain_key in self._factories:
                raise DuplicateProviderError(chain_key.value)
            self._factories[chain_key] = factory
            return factory

        return claim

    def create(self, chain_key: str, client: httpx.AsyncClient) -> ChainProvider:
        """Build the provider for `chain_key` over `client`.

        Named `create` rather than `get`: it builds a new provider bound to the client it
        was handed, every time, and `get` would suggest it returns something stored.

        Takes a plain `str` for the same reason `domain.chains.validate_address` does --
        the value arrives from a database column or a request body, and an unknown key
        should be this function's ordinary rejection rather than a `ValueError` the caller
        has to remember to catch separately.

        Raises:
            UnknownChainError: the key is not a `ChainKey`, or no provider claimed it. The
                exception carries both the key and the sorted list of what is registered.
        """
        try:
            key = ChainKey(chain_key)
        except ValueError:
            raise UnknownChainError(chain_key, self.registered_keys()) from None
        factory = self._factories.get(key)
        if factory is None:
            raise UnknownChainError(chain_key, self.registered_keys())
        return factory(client)

    def registered_keys(self) -> tuple[str, ...]:
        """Every claimed chain key, sorted, as plain strings.

        Sorted so that the set is stable in an error message and in a test assertion, and
        strings rather than `ChainKey` members so that a caller comparing against what
        came out of the database does not have to convert first.
        """
        return tuple(sorted(key.value for key in self._factories))


CHAIN_PROVIDERS: ChainProviderRegistry = ChainProviderRegistry()
"""The registry the application uses. A test builds its own rather than mutating this one."""


def register_chain_provider[FactoryT: ChainProviderFactory](
    chain_key: ChainKey,
) -> Callable[[FactoryT], FactoryT]:
    """Register a provider class for `chain_key` in the process-wide registry.

    The decorator a provider module reaches for:

    ```python
    @register_chain_provider(ChainKey.BITCOIN)
    class EsploraProvider:
        def __init__(self, client: httpx.AsyncClient) -> None: ...
    ```
    """
    return CHAIN_PROVIDERS.register(chain_key)


def get_chain_provider(chain_key: str, client: httpx.AsyncClient) -> ChainProvider:
    """The provider for `chain_key`, built over `client`, from the process-wide registry.

    Raises:
        UnknownChainError: no provider is registered for that key.
    """
    return CHAIN_PROVIDERS.create(chain_key, client)
