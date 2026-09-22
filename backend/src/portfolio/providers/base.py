"""The seam every chain provider is on the other side of: one protocol, four shapes.

A provider answers three questions and nothing else -- what it can do, whether a string is
an address on its chain, and what those addresses hold -- plus a liveness check so an
operator can tell a broken vendor from a broken sync.

## Balances are integers, not `Decimal`

Both chains this product reads count in integer base units: Bitcoin in satoshis and Kaspa
in sompi, each with eight decimals. Converting to `Decimal` here would introduce a rounding
decision at a boundary that has nothing to round -- the number arriving from the API is
already exact -- and it would make every provider test approximate where it could be
exact. `AddressBalance.amount()` converts on demand through `domain.money.from_base_units`,
so the one rule for turning base units into an amount stays in the one module that owns it.

`decimals` is carried on the balance as well as on the capabilities, and that duplication
is deliberate: a balance snapshot that outlives the provider instance -- a row in a table,
a value in a queue -- has to be interpretable without going back to ask which provider
produced it.

**There is no `pending` or `unconfirmed` field.** Of the two target APIs, only Esplora
exposes mempool figures (`mempool_stats` beside `chain_stats`); the Kaspa REST balance
endpoint exposes nothing of the kind. A field that one provider always sets to zero makes
zero ambiguous between "nothing is pending" and "this chain cannot tell you", and the
second is not a balance -- it is the absence of one. If #7 wants mempool visibility it adds
the field *and* a way to say "not answerable here", which is a decision with a caller
behind it rather than one made in advance.

## The protocol is checked by `mypy`, never by `isinstance`

`ChainProvider` is deliberately **not** `@runtime_checkable`. `isinstance` against a
runtime-checkable protocol compares attribute *names* and nothing else: a class whose
`fetch_balances` takes the wrong arguments, or is a plain function rather than a coroutine
function, or returns a single balance rather than a sequence, passes that check happily.
That is a verifier that can only confirm its own account, and this project has now found
that shape of mistake often enough to write it down.

The real check is `mypy --strict` deciding assignability, which is why the conformance
"test" for a fake provider is a module-level annotated assignment rather than an assertion.
It costs nothing at runtime and it actually reads the signatures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from portfolio.domain.money import from_base_units
from portfolio.providers.errors import ProviderResponseError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from decimal import Decimal

    from portfolio.domain.chains import ChainKey, ValidatedAddress

__all__ = [
    "AddressBalance",
    "ChainCapabilities",
    "ChainProvider",
    "ProviderHealth",
    "align_balances",
    "chunk_addresses",
]


@dataclass(frozen=True, slots=True)
class AddressBalance:
    """What one address holds, in the units the chain itself counts in.

    `address` is the canonical form the provider was asked about, carried on the result so
    that a caller can correlate without relying on list position. `fetch_balances`
    guarantees the position too, but a guarantee that is also checkable is worth more than
    one that is only promised.

    Nothing validates `confirmed` here. The place the fetch contract is enforced is
    `align_balances`, which refuses a negative count with a `ProviderResponseError`; a
    second refusal in this constructor would give one condition two exception types and
    leave a caller guessing which to catch.
    """

    address: str
    confirmed: int
    decimals: int

    def amount(self) -> Decimal:
        """The balance as a decimal amount, by the domain's conversion rule.

        Deliberately a method rather than a field: the conversion is only correct through
        `domain.money.from_base_units`, and a field would let a provider compute it some
        other way and store the result. Called on demand, there is exactly one rule.

        Raises:
            TypeError: `confirmed` or `decimals` is not an `int`.
            ValueError: `decimals` is negative.
        """
        return from_base_units(self.confirmed, self.decimals)


@dataclass(frozen=True, slots=True)
class ChainCapabilities:
    """What a provider can do, declared rather than assumed.

    `max_addresses_per_call` is an integer and not a `can_batch` boolean, because the
    boolean is derivable from the integer and the integer is not derivable from the
    boolean. The two target APIs settle it: Esplora documents a single-address balance
    endpoint and no batch endpoint at all, so its provider declares `1`; the Kaspa REST
    server documents `POST /addresses/balances` taking a list, so its provider declares
    more. A caller that only knew "can batch: false" would still have to guess how many
    addresses the other one accepts.

    `chunk_addresses` consumes this, which is the point -- a capability nothing reads is
    decoration, and it drifts out of date without anything noticing.
    """

    chain_key: ChainKey
    decimals: int
    max_addresses_per_call: int

    def __post_init__(self) -> None:
        """Refuse a declaration that cannot describe a real provider.

        `max_addresses_per_call = 0` would make `chunk_addresses` loop forever, and a
        negative `decimals` would be refused later by `from_base_units` -- at the point a
        balance is converted, which is far from the declaration that was actually wrong.

        Raises:
            ValueError: `decimals` is negative, or the call size is below one.
        """
        if self.decimals < 0:
            message = f"decimals must not be negative, got {self.decimals}"
            raise ValueError(message)
        if self.max_addresses_per_call < 1:
            message = (
                "max_addresses_per_call must be at least 1 "
                f"(1 means the provider cannot batch), got {self.max_addresses_per_call}"
            )
            raise ValueError(message)

    @property
    def can_batch(self) -> bool:
        """Whether more than one address fits in a single call.

        Derived, never stored: a stored copy is a second source of truth that can
        disagree with the number it was derived from.
        """
        return self.max_addresses_per_call > 1


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Whether a provider's upstream answered, for an operator rather than for a balance.

    `detail` is a short human-readable reason -- "connect timeout", "HTTP 503" -- and it
    **must never carry an address, a URL or a response body**. It is rendered in an
    operations view and it reaches a log; both are places the owner's holdings must not
    be. The same rule the log transport enforces for URLs applies to anything written
    here by hand.

    There is no timestamp field. The caller knows when it asked, and a second timestamp
    produced inside the provider is one that can disagree with the caller's own -- two
    answers to "when was this true" is how a stale reading gets presented as a fresh one.
    """

    chain_key: ChainKey
    healthy: bool
    detail: str | None = None


class ChainProvider(Protocol):
    """What a chain provider must offer. Structural, and checked statically.

    Not `@runtime_checkable`; the module docstring says why at length. The short version
    is that `isinstance` would compare four attribute names and nothing about their
    signatures, and it is the signatures that matter.
    """

    @property
    def capabilities(self) -> ChainCapabilities:
        """What this provider can do. Constant for the life of the instance."""

    def validate_address(self, raw: str) -> ValidatedAddress:
        """Decide whether `raw` is an address on this chain, offline.

        **Synchronous, and it must stay synchronous.** It delegates to
        `domain.chains.validate_address`, which opens no socket and reads no clock. That
        is what lets a caller tell a mistyped address from an unreachable API without a
        round trip, and it is why the address codecs live in `domain` rather than here.

        Raises:
            AddressInvalidError: the string is not an address on this chain. The
                rejection names a reason and never contains `raw`.
        """

    async def fetch_balances(self, addresses: Sequence[str]) -> Sequence[AddressBalance]:
        """Read the confirmed balance of each address, in the order they were given.

        Always a sequence, even for one address: a provider that also offered a
        single-address method would have two code paths to keep in step, and the one used
        less often would be the one that rots.

        The contract is one result per requested address, same length, same order, each
        carrying the address it is about, and an address with no history is a zero rather
        than an omission -- that is what an unused address means on chain. Implementations
        get this by building their result with `align_balances` rather than by being
        careful.

        Raises:
            ProviderUnavailableError: the chain could not be reached, or did not answer.
            ProviderResponseError: it answered with something that cannot be trusted.
        """

    async def health(self) -> ProviderHealth:
        """Whether this provider's upstream is answering, without reading any address.

        Separate from `fetch_balances` so that an operations view can report "the chain is
        down" without naming a wallet, and so that a failing health check is not itself a
        disclosure of what is being watched.
        """


def align_balances(
    requested: Sequence[str],
    found: Mapping[str, int],
    *,
    decimals: int,
) -> tuple[AddressBalance, ...]:
    """Turn what a provider parsed out of a response into the answer the contract promises.

    **This function is where the `fetch_balances` contract is enforced by construction.**
    A provider parses its response into a `{address: base_units}` mapping and hands it
    here; length, order and the missing-address rule then follow from the code rather than
    from the implementer having remembered them. Four cases, and each is a decision:

    | Case | Outcome |
    |---|---|
    | a requested address is missing from `found` | a zero balance |
    | `found` carries an address that was not requested | `ProviderResponseError` |
    | a count that is not a whole number of base units | `ProviderResponseError` |
    | a negative base-unit count | `ProviderResponseError` |
    | the same address requested twice | `ValueError` |

    The extra-address case is the one worth stating out loud. A batch API answering about
    something we did not ask about is a correlation bug -- a paging mistake, an off-by-one
    in the request, a vendor echoing a cached batch -- and silently dropping the entry
    would hide it behind a total that still looks plausible. The duplicate-request case is
    a `ValueError` rather than a `ProviderResponseError` because the caller made that
    mistake, not the vendor, and the two deserve different blame.

    No message here names an address. The count is enough to act on, and the addresses are
    the owner's holdings.

    Args:
        requested: the addresses, canonical, in the order the answer must come back in.
        found: what the provider parsed, keyed by the same canonical form.
        decimals: the chain's exponent, copied onto every balance.

    Returns:
        One `AddressBalance` per entry in `requested`, in that order.

    Raises:
        ValueError: `requested` contains the same address more than once.
        ProviderResponseError: `found` has an address nobody asked about, a negative
            base-unit count, or a count that is not a whole number of base units -- a
            `float` from a vendor that renders its balances with a decimal point, say.
    """
    asked = set(requested)
    if len(asked) != len(requested):
        message = (
            f"align_balances was given {len(requested)} addresses "
            f"of which only {len(asked)} are distinct"
        )
        raise ValueError(message)

    unexpected = len(set(found) - asked)
    if unexpected:
        message = (
            f"The response carried {unexpected} address(es) that were not requested, "
            "so it cannot be matched to the request."
        )
        raise ProviderResponseError(message)

    balances: list[AddressBalance] = []
    for address in requested:
        units = _require_base_units(found.get(address, 0))
        if units < 0:
            message = f"The response carried a negative base-unit count of {units}."
            raise ProviderResponseError(message)
        balances.append(AddressBalance(address=address, confirmed=units, decimals=decimals))
    return tuple(balances)


def _require_base_units(value: object) -> int:
    """Refuse anything that is not a whole number of base units, before it is stored.

    **`Mapping[str, int]` is a static claim, and this boundary meets values `mypy` never
    saw.** A provider parses its response with `json.loads`, which hands back whatever the
    vendor sent: a vendor that renders a balance as `1.0e8`, or as `100000000.0`, produces
    a `float`, and `100000000.0 < 0` is perfectly `False`. Without this guard that float
    is stored on an `AddressBalance` and lives inside `providers/`, where the AST ban in
    `backend/tests/security/test_no_float.py` cannot see it -- the ban reads source, and
    this float has no literal and no `float` anywhere in the file.

    `from_base_units` would eventually refuse it, but only when somebody calls `.amount()`.
    Anything that sums or compares `confirmed` first -- a total, a sort, an equality check
    against a stored snapshot -- has already done float arithmetic on money by then, which
    is the whole failure rule 2 exists to prevent, arriving at the one boundary that claims
    to be the enforcement point.

    A `bool` is refused with everything else, for the reason `domain/money.py` gives: it is
    an `int` subclass, so `True` would pass an `isinstance(..., int)` check and convert to
    one base unit and be reported as a holding.

    **The `bool` arm is the one the static type cannot stand in for**, which is worth
    knowing before someone deletes it as redundant. Measured under `mypy --strict`:
    `takes({"a": True})` against a `Mapping[str, int]` parameter is accepted with no
    error, because `bool` is a subtype of `int` and `Mapping` is covariant in its value;
    `takes({"a": 1.0})` on the same signature *is* rejected. So the annotation catches a
    literal float and never catches a bool -- and catches neither once the value has come
    through `json.loads`, which is typed `Any`.

    Takes `object` rather than `int` on purpose. Declared as `int` the check would be
    statically dead, and `warn_unreachable` would -- correctly -- report the raise as
    unreachable code. The parameter type is the honest description of what actually
    arrives here.

    The message names the type, never the address: which address a vendor mangled is the
    owner's holdings, and the type is the part anyone can act on.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        message = (
            f"The response carried a base-unit count of type {type(value).__name__}; "
            "a balance must be a whole number of base units."
        )
        raise ProviderResponseError(message)
    return value


def chunk_addresses(
    addresses: Sequence[str],
    capabilities: ChainCapabilities,
) -> list[tuple[str, ...]]:
    """Split a request into calls the provider has declared it can actually make.

    Sized from `capabilities.max_addresses_per_call`, so a caller never has to know which
    chain batches. For Esplora that yields one call per address and for Kaspa it yields
    one call per `max_addresses_per_call` addresses, from identical calling code.

    A list rather than a generator, deliberately: a caller that wants to log "reading 40
    addresses in 4 calls" before it starts needs a length, and a generator would make that
    either impossible or a second pass.

    An empty `addresses` yields no chunks at all rather than one empty chunk -- a call
    asking about nothing is a request we should not make.
    """
    size = capabilities.max_addresses_per_call
    return [tuple(addresses[start : start + size]) for start in range(0, len(addresses), size)]
