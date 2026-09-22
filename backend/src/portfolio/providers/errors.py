"""Why a provider call failed, as a type a caller can branch on.

One hierarchy rather than a mixture of `httpx` exceptions, `KeyError` and `ValueError`,
because a service above this layer has exactly three decisions to make and they do not map
onto the library's taxonomy:

* **the chain is not reachable right now** -- retry later, keep the last known balance,
  and say so in the UI rather than reporting zero;
* **the chain answered and the answer is unusable** -- do not retry, because the next
  identical request produces the same nonsense; this is a bug in the provider's parsing or
  a change at the vendor, and it needs a human;
* **nobody asked a chain anything, because there is no provider for it** -- a wiring
  mistake, discovered at the call site.

Reporting a zero balance for an unreachable chain is the failure this distinction exists
to prevent. A portfolio that silently drops a wallet it could not read looks exactly like
a portfolio whose wallet is empty, and the owner has no way to tell the two apart.

**No exception here carries an address.** `services/wallets.py` establishes the rule and
the reason is the same: an exception message ends up in a log, in a response body, or in a
traceback, and the set of addresses this application watches *is* the owner's holdings. A
message that needs to describe a bad batch says how many entries were wrong, not which.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "DuplicateProviderError",
    "ProviderError",
    "ProviderRateLimitedError",
    "ProviderResponseError",
    "ProviderUnavailableError",
    "UnknownChainError",
]


class ProviderError(Exception):
    """Base class for every failure that originates in `providers/`.

    A caller that only wants "the sync did not work" catches this one. A caller that has
    to decide whether retrying is worth anything catches a subclass.
    """


class ProviderUnavailableError(ProviderError):
    """The chain could not be reached, or it failed to answer after every attempt.

    Transient by assumption: connection refused, a timeout, a 5xx, or a rate limit that
    outlived the retry budget. The balance from the previous successful read is still the
    best information available, and it is a better answer than a zero.

    **A provider raises this; the shared transport does not.** `RetryingTransport` lets an
    `httpx.TransportError` propagate and returns a failing response as a response, because
    it implements `httpx.AsyncBaseTransport` and owes that interface its own exception
    types. The provider is the layer that catches both and decides what they mean, with
    the original attached through `raise ... from error` so the cause survives:

    ```python
    try:
        response = await self._client.get(url, extensions={"endpoint": "address_balance"})
        response.raise_for_status()
    except httpx.TransportError as error:
        raise ProviderUnavailableError(...) from error
    ```

    That one `except` per provider is what keeps `httpx` out of `services/`. Doing it in
    the transport instead would have handled the connection failure and left the 503 as a
    response -- one concept in two shapes for every caller.
    """


class ProviderRateLimitedError(ProviderUnavailableError):
    """The chain refused the request because we asked too often.

    A subclass of "unavailable" rather than a sibling, so that a caller which only cares
    about "try again later" does not have to enumerate both. It is separate at all because
    the remedy differs: an unavailable host is waited out, while being throttled means
    `HostRateLimiter`'s interval is too short for this vendor and the fix is a
    configuration change rather than patience.

    A provider raises this for a 429 that survived the transport's retries. It is the
    easiest of the three to forget, precisely because "try later" is not a *wrong* reading
    of a 429 -- it is just the reading that loses the only actionable fact in it. Neither
    this class nor `ProviderUnavailableError` has a raiser in #6; both arrive with the
    first provider.
    """


class ProviderResponseError(ProviderError):
    """The chain answered, and the answer is either untrustworthy or a refusal.

    Raised by `align_balances` when a batch response does not correspond to the batch
    request -- an address nobody asked about, or a negative base-unit count -- and by a
    provider whose parser meets a shape the vendor's documentation does not describe.

    **Also every 4xx that is not a 429**, which is the mapping most easily got wrong.
    A 400, 401, 403 or 404 means the chain understood the request and said no; that is a
    fact about *this request*, not about the chain's availability. Reporting one as a
    `ProviderUnavailableError` is the concrete failure the hierarchy exists to prevent:
    put a self-hosted Esplora behind an auth proxy and it starts returning 401, and a
    provider that maps it to "unavailable" tells the owner their chain is down forever
    while never mentioning the credential. `docs/providers.md` carries the mapping a new
    provider copies.

    **Not retryable, deliberately.** The same request produces the same unusable answer,
    and three attempts at it only turn one wrong result into three. Something has changed
    at the vendor or the parser has a bug; both need a person.
    """


class UnknownChainError(ProviderError):
    """No provider is registered for that chain key.

    Always a wiring mistake rather than a runtime condition: either the chain module was
    never imported in `providers/chains/__init__.py`, or the key is not a `ChainKey` at
    all. `known_keys` is on the exception because the first question anyone asks when they
    see this is "well, what *is* registered", and making them find the registry to answer
    it wastes the one moment when the process has the answer to hand.
    """

    def __init__(self, chain_key: str, known_keys: Iterable[str]) -> None:
        self.chain_key = chain_key
        self.known_keys: tuple[str, ...] = tuple(sorted(known_keys))
        registered = ", ".join(self.known_keys) or "none"
        super().__init__(
            f"No provider is registered for chain {chain_key!r}; registered: {registered}."
        )


class DuplicateProviderError(ProviderError):
    """Two providers claim the same chain key.

    Raised at registration, which is to say at import, so the process refuses to start
    rather than serving balances from whichever class happened to be decorated last. A
    copy-pasted `@register_chain_provider(ChainKey.BITCOIN)` above a Kaspa provider is a
    one-character mistake that would otherwise present as wrong balances, days later, with
    nothing in any log pointing at the decorator.
    """

    def __init__(self, chain_key: str) -> None:
        self.chain_key = chain_key
        super().__init__(f"A provider is already registered for chain {chain_key!r}.")
