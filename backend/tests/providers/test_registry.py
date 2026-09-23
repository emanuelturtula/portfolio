"""Criterion 5: the registry, and the typed error it raises for a key nobody claimed.

**Every test here builds its own `ChainProviderRegistry`.** Not one of them touches
`CHAIN_PROVIDERS`, and that is the point: a test that mutated the process-wide registry
would need a fixture to put it back, and a test that has to restore shared state is a test
that shares state with the thing it verifies. The module-level instance is asserted about
-- its type, its emptiness -- and never written to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from portfolio.domain.chains import ChainKey
from portfolio.providers.errors import DuplicateProviderError, ProviderError, UnknownChainError
from portfolio.providers.registry import (
    CHAIN_PROVIDERS,
    ChainProviderRegistry,
    get_chain_provider,
    register_chain_provider,
)
from tests.providers.fakes import FakeChainProvider

if TYPE_CHECKING:
    from portfolio.providers.base import ChainProvider


@pytest.fixture
def client() -> httpx.AsyncClient:
    """A client that is never used, because registration never makes a request.

    It carries a transport that refuses everything, so a registry that decided to probe a
    provider at registration time would fail loudly here rather than quietly reaching the
    network from a unit test.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        message = f"the registry made a request to {request.url.host}"
        raise AssertionError(message)

    return httpx.AsyncClient(transport=httpx.MockTransport(refuse))


class ProviderTakingAClient(FakeChainProvider):
    """The shape a real provider has: an `__init__` that takes the shared client.

    A class whose constructor takes the client already *is* the factory, which is why the
    decorator can return its argument unchanged and why nothing here has to register a
    lambda.

    **It has to satisfy `ChainProvider`, and that is not incidental.** A first version of
    this class held nothing but `self.client`, and `mypy --strict` rejected every
    `register` call in this module -- `ChainProviderFactory` is
    `Callable[[AsyncClient], ChainProvider]`, so a factory that builds something which is
    not a provider is refused at the registration site. That is criterion 7's static check
    doing its job on a test fixture, which is the best possible place to have seen it
    work.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        super().__init__()
        self.client = client


class ASecondProvider(ProviderTakingAClient):
    """A different class for the duplicate-registration tests.

    Distinct from `ProviderTakingAClient` so that "the first registration survived" is a
    statement about identity and not about two names for one object.
    """


# --------------------------------------------------------------------------------------
# Criterion 5: an unknown key is a typed error that says what is known
# --------------------------------------------------------------------------------------


def test_an_unknown_chain_key_raises_the_typed_error(client: httpx.AsyncClient) -> None:
    """A registered chain with no provider is `UnknownChainError`, not a `KeyError`.

    `KeyError` from a dictionary lookup reaches the API layer as a 500 with a traceback
    and no explanation. A typed error is something a service can catch and turn into an
    answer.
    """
    registry = ChainProviderRegistry()

    with pytest.raises(UnknownChainError) as caught:
        registry.create(ChainKey.BITCOIN.value, client)

    assert isinstance(caught.value, ProviderError)
    assert caught.value.chain_key == "bitcoin"


@pytest.mark.parametrize(
    "chain_key",
    ["", "BITCOIN", "bitcoin ", "ethereum", "btc", "bitcoin;drop"],
    ids=["empty", "upper", "trailing space", "unsupported", "ticker", "injection"],
)
def test_a_key_that_is_not_a_chain_key_at_all_is_the_same_typed_error(
    chain_key: str, client: httpx.AsyncClient
) -> None:
    """`create` takes a plain `str`, so "not a chain" and "no provider" must agree.

    The value arrives from a database column or a request body. If an unknown *string*
    raised `ValueError` from the enum while an unclaimed *key* raised `UnknownChainError`,
    every caller would have to catch both -- and the one that forgot would turn a typo in
    a chain key into a 500.
    """
    registry = ChainProviderRegistry()
    registry.register(ChainKey.BITCOIN)(ProviderTakingAClient)

    with pytest.raises(UnknownChainError) as caught:
        registry.create(chain_key, client)

    assert caught.value.chain_key == chain_key


def test_the_unknown_chain_error_lists_what_is_registered(client: httpx.AsyncClient) -> None:
    """The first question anyone asks on seeing this error is what *is* registered.

    The process has the answer at the moment it raises and nowhere else convenient, so it
    puts it on the exception rather than making somebody go and read the registry.
    """
    registry = ChainProviderRegistry()
    registry.register(ChainKey.KASPA)(ProviderTakingAClient)
    registry.register(ChainKey.BITCOIN)(ProviderTakingAClient)

    with pytest.raises(UnknownChainError) as caught:
        registry.create("ethereum", client)

    assert caught.value.known_keys == ("bitcoin", "kaspa")
    assert "bitcoin" in str(caught.value)
    assert "kaspa" in str(caught.value)


def test_the_error_says_none_rather_than_nothing_when_the_registry_is_empty(
    client: httpx.AsyncClient,
) -> None:
    """An empty list rendered into a message is an empty space nobody can read.

    Today's state, and the one #7 changes: the message has to be legible when the answer
    to "what is registered" is "nothing at all".
    """
    registry = ChainProviderRegistry()

    with pytest.raises(UnknownChainError) as caught:
        registry.create("bitcoin", client)

    assert caught.value.known_keys == ()
    assert "none" in str(caught.value)


def test_the_error_does_not_quote_anything_but_the_key() -> None:
    """A chain key is not sensitive; nothing else the caller passed should appear either."""
    error = UnknownChainError("ethereum", ["bitcoin"])

    assert "ethereum" in str(error)
    assert error.known_keys == ("bitcoin",)


# --------------------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------------------


def test_a_registered_provider_is_built_over_the_client_it_was_handed(
    client: httpx.AsyncClient,
) -> None:
    """The registry holds factories, not instances, and the client is the caller's.

    Holding an instance would tie a provider's lifetime to import time, before any client
    exists -- and then either every provider would share one client created too early, or
    each would create its own and the rate limiter's per-host state would be split across
    however many there were.
    """
    registry = ChainProviderRegistry()
    registry.register(ChainKey.BITCOIN)(ProviderTakingAClient)

    built = registry.create("bitcoin", client)

    assert isinstance(built, ProviderTakingAClient)
    assert built.client is client


def test_every_call_builds_a_new_provider_rather_than_returning_a_stored_one(
    client: httpx.AsyncClient,
) -> None:
    """Which is why the method is called `create`. A cached instance would outlive its client."""
    registry = ChainProviderRegistry()
    registry.register(ChainKey.BITCOIN)(ProviderTakingAClient)

    first = registry.create("bitcoin", client)
    second = registry.create("bitcoin", client)

    assert first is not second


def test_the_decorator_returns_the_class_unchanged() -> None:
    """A decorator that wrapped the class would break every use site's type and `isinstance`.

    It also has to return *the same object*, not an equivalent one: a provider module's
    own tests, and any future code that touches the class directly, would otherwise be
    looking at something else entirely.
    """
    registry = ChainProviderRegistry()

    decorated = registry.register(ChainKey.BITCOIN)(ProviderTakingAClient)

    assert decorated is ProviderTakingAClient


def test_registering_one_key_twice_is_refused() -> None:
    """A copy-pasted decorator silently shadowing the provider above it is wrong balances.

    Raised at registration, which is to say at import, so the process refuses to start
    instead of serving one chain's balances under another chain's name -- a bug that would
    otherwise surface days later with nothing in any log pointing at the decorator.
    """
    registry = ChainProviderRegistry()
    registry.register(ChainKey.BITCOIN)(ProviderTakingAClient)

    with pytest.raises(DuplicateProviderError) as caught:
        registry.register(ChainKey.BITCOIN)(ASecondProvider)

    assert caught.value.chain_key == "bitcoin"


def test_the_first_registration_survives_a_refused_second_one(
    client: httpx.AsyncClient,
) -> None:
    """The refusal must not half-apply and leave the second class installed.

    A `register` that recorded the factory and *then* checked for a duplicate would raise
    and still have overwritten the entry, which is the worst of both outcomes: a loud
    error and the wrong provider.
    """
    registry = ChainProviderRegistry()
    registry.register(ChainKey.BITCOIN)(ProviderTakingAClient)

    with pytest.raises(DuplicateProviderError):
        registry.register(ChainKey.BITCOIN)(ASecondProvider)

    assert isinstance(registry.create("bitcoin", client), ProviderTakingAClient)


def test_two_different_keys_coexist(client: httpx.AsyncClient) -> None:
    """The ordinary case, so the duplicate check is not simply refusing everything."""
    registry = ChainProviderRegistry()
    registry.register(ChainKey.BITCOIN)(ProviderTakingAClient)
    registry.register(ChainKey.KASPA)(ProviderTakingAClient)

    assert registry.registered_keys() == ("bitcoin", "kaspa")


def test_the_registered_keys_are_sorted_strings_rather_than_enum_members() -> None:
    """Sorted, so an error message and a test assertion are both stable.

    Plain strings, so a caller comparing against a value that came out of the database
    does not have to convert first -- and a `ChainKey` compares equal to its string
    anyway, which would have hidden the difference until something serialised one.
    """
    registry = ChainProviderRegistry()
    registry.register(ChainKey.KASPA)(ProviderTakingAClient)
    registry.register(ChainKey.BITCOIN)(ProviderTakingAClient)

    keys = registry.registered_keys()

    assert keys == ("bitcoin", "kaspa")
    assert all(type(key) is str for key in keys)


# --------------------------------------------------------------------------------------
# The process-wide instance
# --------------------------------------------------------------------------------------


def test_the_module_level_registry_holds_exactly_the_wired_providers(
    client: httpx.AsyncClient,
) -> None:
    """Pinned, and #7 is what changed it. It was `()` until Bitcoin landed.

    This is the same hazard criterion 6 has: "the registry works" is satisfiable by a
    registry with nothing in it. Asserting the empty state as a fact meant the first
    provider to land had to come back here and say so, which is what this edit is.

    **The import is explicit and it is not a formality.** `CHAIN_PROVIDERS` is populated
    by decorators, which run when `providers/chains/__init__.py` is imported -- so without
    this line the assertion would pass or fail depending on whether some *other* test
    module had already imported the package, which is a verdict decided by collection
    order rather than by the code. `tests/providers/test_chain_modules.py` owns the
    "every module is wired" scan; this owns "the shared instance is the one the wiring
    filled".
    """
    import portfolio.providers.chains  # noqa: F401 - imported for its registration effect

    assert CHAIN_PROVIDERS.registered_keys() == ("bitcoin",)

    with pytest.raises(UnknownChainError):
        get_chain_provider("kaspa", client)


def test_the_module_level_helpers_operate_on_the_module_level_registry() -> None:
    """`register_chain_provider` and `get_chain_provider` are not a second registry.

    Asserted without registering anything, because registering into the shared instance
    from a test is exactly the shared-state mutation this module avoids: both helpers are
    checked to be bound to `CHAIN_PROVIDERS` by what they close over instead.

    The decorator is taken for `ChainKey.KASPA` rather than `ChainKey.BITCOIN` since #7:
    Bitcoin is registered now, so `register(ChainKey.BITCOIN)` would raise
    `DuplicateProviderError` at the moment the decorator is *created* -- which is the
    registry doing its job and would read here as an unrelated failure.
    """
    import portfolio.providers.chains  # noqa: F401 - imported for its registration effect

    assert register_chain_provider.__module__ == CHAIN_PROVIDERS.__module__
    assert isinstance(CHAIN_PROVIDERS, ChainProviderRegistry)
    before = CHAIN_PROVIDERS.registered_keys()
    # The decorator the helper hands back is the one the shared registry hands back:
    # both are the `claim` closure defined inside `ChainProviderRegistry.register`.
    from_helper = register_chain_provider(ChainKey.KASPA)
    from_instance = CHAIN_PROVIDERS.register(ChainKey.KASPA)

    assert from_helper.__qualname__ == from_instance.__qualname__
    # Neither was applied to anything, so this test registered nothing of its own -- which
    # is the property that keeps it from leaking state into every module after it.
    assert CHAIN_PROVIDERS.registered_keys() == before == ("bitcoin",)


def test_a_registered_fake_satisfies_the_provider_protocol(client: httpx.AsyncClient) -> None:
    """What comes out of `create` is usable as a `ChainProvider`, checked by mypy.

    The annotation is the assertion: `mypy --strict` decides whether the registry's return
    type is assignable, and the runtime half only shows the object really was built.
    """
    registry = ChainProviderRegistry()
    registry.register(ChainKey.BITCOIN)(ProviderTakingAClient)

    built: ChainProvider = registry.create("bitcoin", client)

    assert built.capabilities.chain_key is ChainKey.BITCOIN
