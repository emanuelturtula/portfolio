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

**`pending` is `int | None`, and the `None` is the whole reason the field exists.** #6
refused a `pending` field and named the condition on which it would be reasonable: the
field *plus* a way to say "not answerable here", so that zero is never ambiguous between
"nothing is pending" and "this chain cannot tell you". #7 meets that condition rather than
overriding it. Of the two target APIs only Esplora exposes mempool figures
(`mempool_stats` beside `chain_stats`); the Kaspa REST balance endpoint exposes nothing of
the kind and its provider will leave the field `None` for every address, which is a
statement about the chain rather than a balance of zero.

**It is also signed, and that is not a detail.** A mempool delta is not a balance: an
outgoing payment sitting in the mempool spends a confirmed output and funds nothing, so it
reads negative, which is exactly right and exactly what a naive "balances cannot be
negative" guard would reject. `align_balances` applies its negative refusal to `confirmed`
and deliberately not to `pending`.

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

import json
from dataclasses import dataclass

# A real import, not a `TYPE_CHECKING` one: `decode_json` hands this class to `json.loads`
# as `parse_float`, so it is needed at run time and not only in an annotation.
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from portfolio.domain.money import from_base_units
from portfolio.providers.errors import ProviderResponseError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from portfolio.domain.chains import ChainKey, ValidatedAddress

__all__ = [
    "AddressBalance",
    "ChainCapabilities",
    "ChainProvider",
    "ProviderHealth",
    "align_balances",
    "chunk_addresses",
    "decode_json",
    "require_json_object",
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

    `pending` is the net mempool delta in the same base units, **signed**, and `None`
    means this chain cannot answer the question rather than that the answer is zero. It
    defaults to `None` so that a provider which says nothing about the mempool says
    nothing rather than claiming a zero. Spendable is `confirmed + pending`; nothing here
    computes it, because a caller that has to reach for `pending` has also had to decide
    what to do about its `None`.
    """

    address: str
    confirmed: int
    decimals: int
    pending: int | None = None

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
    pending: Mapping[str, int] | None = None,
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

    **`pending` does not zero-fill, and the asymmetry with `found` is the point.** A
    requested address missing from `found` is a zero, because an address with no history
    holds nothing and that is a fact about the chain. A requested address missing from
    `pending` is `None`, because the chain said nothing about its mempool and "nothing
    pending" is not the same statement. Zero-filling it would collapse exactly the two
    meanings the field was added to keep apart. Omitting the argument entirely is the
    short spelling of "this chain cannot answer at all", and every result then carries
    `pending=None`.

    The other two rules do apply to `pending`: an address nobody requested is refused
    there as well, and `_require_base_units` guards it too, because a batch that
    correlates wrongly correlates wrongly in both halves and a vendor that renders one sum
    as a float renders both that way. **The negative refusal does not apply to it**, and
    must not: a spend sitting in the mempool is a negative delta and is the normal case.

    No message here names an address. The count is enough to act on, and the addresses are
    the owner's holdings.

    Args:
        requested: the addresses, canonical, in the order the answer must come back in.
        found: what the provider parsed, keyed by the same canonical form.
        decimals: the chain's exponent, copied onto every balance.
        pending: the net mempool deltas the provider parsed, signed, keyed the same way.
            `None`, or an address absent from it, means "this chain cannot tell you".

    Returns:
        One `AddressBalance` per entry in `requested`, in that order.

    Raises:
        ValueError: `requested` contains the same address more than once.
        ProviderResponseError: `found` or `pending` has an address nobody asked about, a
            count that is not a whole number of base units -- a `float` from a vendor that
            renders its balances with a decimal point, say -- or, for `found` alone, a
            negative base-unit count.
    """
    asked = set(requested)
    if len(asked) != len(requested):
        message = (
            f"align_balances was given {len(requested)} addresses "
            f"of which only {len(asked)} are distinct"
        )
        raise ValueError(message)

    _refuse_unrequested(found, asked)
    if pending is not None:
        _refuse_unrequested(pending, asked)

    balances: list[AddressBalance] = []
    for address in requested:
        units = _require_base_units(found.get(address, 0))
        if units < 0:
            message = f"The response carried a negative base-unit count of {units}."
            raise ProviderResponseError(message)
        balances.append(
            AddressBalance(
                address=address,
                confirmed=units,
                decimals=decimals,
                pending=_pending_units(pending, address),
            )
        )
    return tuple(balances)


def _refuse_unrequested(reported: Mapping[str, int], asked: set[str]) -> None:
    """Refuse a mapping that answers about an address nobody requested.

    A batch API answering about something we did not ask about is a correlation bug -- a
    paging mistake, an off-by-one in the request, a vendor echoing a cached batch -- and
    silently dropping the entry would hide it behind a total that still looks plausible.

    One function rather than the check written twice, because `found` and `pending` come
    out of the same response and a check applied to only one of them is a check that
    passes for half of a correlation failure.

    Raises:
        ProviderResponseError: at least one key was not requested. The message says how
            many; which ones is the owner's holdings.
    """
    unexpected = len(set(reported) - asked)
    if unexpected:
        message = (
            f"The response carried {unexpected} address(es) that were not requested, "
            "so it cannot be matched to the request."
        )
        raise ProviderResponseError(message)


def _pending_units(pending: Mapping[str, int] | None, address: str) -> int | None:
    """This address's mempool delta, or `None` for "the chain did not say".

    Three ways to arrive at `None`, and they mean the same thing to a caller: the provider
    passed no mapping at all, or it passed one with no entry for this address -- which is
    what an Esplora response carrying no `mempool_stats` produces -- or it passed one
    holding an explicit `None` for it.

    The third is off the static contract, since the parameter is `Mapping[str, int]`, and
    it is treated as the other two rather than refused on purpose. A provider that builds
    its mapping with `pending[address] = parsed.pending` before checking for `None` has
    written down the same fact in the spelling the type does not allow, and turning that
    into a `ProviderResponseError` would report a vendor for a mistake the provider made.
    `tests/providers/test_base.py` pins the equivalence, so it is a decision rather than
    an accident of `dict.get`.
    """
    if pending is None:
        return None
    reported = pending.get(address)
    if reported is None:
        return None
    return _require_base_units(reported)


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


def decode_json(body: str | bytes) -> object:
    """`json.loads`, with every failure it has translated into this package's vocabulary.

    Shared because the catch clause is the interesting part and a second copy of it would
    drift. #7 wrote it inside `chains/bitcoin.py`, review corrected the clause there, and
    #8 needed the same boundary for a second vendor -- the same argument
    `providers/endpoints.py` makes about the failover loop.

    **`ValueError` and `RecursionError`, not `JSONDecodeError` and `UnicodeDecodeError`**,
    and that is a correction rather than defensive breadth. Measured:

    | Body | What `json.loads` raises |
    |---|---|
    | `not json` | `json.JSONDecodeError` |
    | bytes that are not UTF-8 | `UnicodeDecodeError` |
    | an integer past the digit limit | `ValueError: Exceeds the limit (4300 digits)` |
    | arrays nested past the scanner's depth | `RecursionError` |

    The first two are `ValueError` subclasses, so naming `ValueError` subsumes them and
    catches the integer-limit case that the narrower pair let escape untyped. The last one
    is not a `ValueError` at all and has to be named. Both escaping arms reached every
    parser in `chains/bitcoin.py`, which is to say they reached `health()`, whose contract
    is that it never raises -- from a body a hostile or broken instance chooses freely.

    `CPython` sets the digit limit and the recursion limit; neither is something this
    application configures, and both are the kind of boundary a vendor can cross by
    accident. **Neither depth is a number to write down.** The digit limit is per process
    (`PYTHONINTMAXSTRDIGITS`), and the nesting limit is `Py_C_RECURSION_LIMIT`, a build
    constant `sys.setrecursionlimit` does not move: measured at 2998 arrays on a Windows
    build and past 5000 on `ubuntu-24.04`, which is the platform this deploys to. So the
    same body that trips this arm on a developer's machine parses cleanly on the Pi and is
    refused one layer later for not being an object -- a difference invisible in a green
    suite, and the reason the fixtures for this arm probe the interpreter instead.

    "A malformed body raises a typed schema error rather than propagating a parse error" is
    a criterion on both providers, and "parse error" is exactly what these two were.

    The message says the body did not parse and **never shows it**. A parser error that
    quotes the offending text is the disclosure every parser in this package is written to
    avoid: the text is a response body containing the owner's addresses.

    ## `parse_float=Decimal`, which is rule 2 applied at the only moment it can be

    A JSON number is read into an IEEE-754 double by every mainstream parser, `json.loads`
    included. **By the time a value reaches the first line any of our code could inspect,
    the digits the vendor sent are already gone**: `0.04228645` is a `float` whose nearest
    representable value is not that number, and no care afterwards recovers it. Rule 2 is
    not "do not write the word `float`"; it is "do not let a monetary value pass through
    binary floating point", and this hook is where that is decided.

    `Decimal` is constructed from the *literal text* of the number, so a price arrives
    carrying exactly the digits that were on the wire. The one vendor that forces the
    issue is the Kaspa price endpoint, whose body is `{"price": 0.04228645}` -- a JSON
    number where Kraken and Coinbase both send a string -- but this is not a special case
    for that vendor: any future API rendering money as a number is covered by the same
    line, in the one place every provider's decode already passes through.

    **It is fixed rather than a parameter, deliberately.** A `parse_float` argument would
    let a call site ask for the float back, and the guarantee is worth more than the
    flexibility; there is no vendor for whom the double is the more faithful answer.

    Nothing this change touches loosens a balance parser. `_require_base_units`,
    `chains.kaspa._require_sompi` and `chains.bitcoin.parse_tip_height` each demand an
    `int`, and `Decimal("1.0E+8")` is no more an `int` than `1.0e8` was -- so a vendor
    rendering a balance with a decimal point is refused exactly as before, with the type
    in the message reading `Decimal` instead of `float`.

    ## `parse_float` alone leaves a hole, and `parse_constant` is the rest of it

    **Measured, because it is not what the argument name suggests.** `json.loads` routes
    the three bare tokens `NaN`, `Infinity` and `-Infinity` through `parse_constant`, not
    through `parse_float`, and its default hands back a Python `float`:

    ```
    json.loads('{"p": NaN}', parse_float=Decimal)["p"]  -> nan   (a float)
    json.loads('{"p": 1.5}', parse_float=Decimal)["p"]  -> Decimal("1.5")
    ```

    So a vendor sending `{"price": Infinity}` would have put a `float` inside `providers/`
    through the very decoder that exists to stop that -- invisible to the AST ban, which
    reads source and would find no literal and no name. The individual price and balance
    parsers do refuse it, each by demanding a type it is not, but relying on that means the
    guarantee is "every parser remembered" rather than "the decoder does not produce one".

    `parse_constant` therefore refuses outright. **None of the three is valid JSON**: RFC
    8259 admits no non-finite number, so this is Python's extension being turned off rather
    than a vendor's legitimate output being rejected. The refusal is typed and says which
    token, which is a fixed word from a closed set of three and discloses nothing.

    Raises:
        ProviderResponseError: the body is not JSON, is JSON the decoder cannot finish, or
            carries one of JSON's three non-finite extensions.
    """
    try:
        return json.loads(body, parse_float=Decimal, parse_constant=_refuse_json_constant)
    except (ValueError, RecursionError) as error:
        message = "The response body is not JSON."
        raise ProviderResponseError(message) from error


def _refuse_json_constant(token: str) -> object:
    """Refuse `NaN`, `Infinity` and `-Infinity`, which `parse_float` never sees.

    Raised rather than returned, and **deliberately not a `ValueError`**:
    `ProviderResponseError` travels out through `json.loads` and past `decode_json`'s own
    `except (ValueError, RecursionError)` untouched, so the caller gets a message naming
    the token instead of the generic "not JSON" that every malformed body produces. A
    `ValueError` here would be caught by that clause and the reason would be lost.

    Returning a sentinel instead would push the decision back into every parser, which is
    the arrangement this function exists to replace.
    """
    message = (
        f"The response body carries the JSON extension {token}, which is not a number "
        "and is not valid JSON."
    )
    raise ProviderResponseError(message)


def require_json_object(body: str | bytes) -> Mapping[str, object]:
    """The body as a JSON object, or a refusal naming what it was instead.

    Status is decided before this is ever called -- see `EndpointSet`. A 502 carrying an
    HTML error page is an unavailable upstream, not a schema error, and deciding that from
    the body would file it under "needs a human" forever.

    The message names the type and never the value, for the reason `decode_json` gives.
    A provider whose endpoint documents an **array** rather than an object checks that
    itself: there is one shape per endpoint, and a helper taking "which shape did you want"
    as an argument would be a spelling of `isinstance` with a longer name.

    Raises:
        ProviderResponseError: the body is not JSON, or is not a JSON object.
    """
    document = decode_json(body)
    if not isinstance(document, dict):
        message = (
            f"The response is a {type(document).__name__} rather than the JSON object "
            "this endpoint documents."
        )
        raise ProviderResponseError(message)
    return document
