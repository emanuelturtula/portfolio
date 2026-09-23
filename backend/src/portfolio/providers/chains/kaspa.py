"""Kaspa balances from a kaspa-rest-server instance, singly and in batches.

The second provider through #6's seam, and the first that **batches**, the first whose read
is a `POST`, and the first to meet a vendor that documents its failures. It reads
`GET /addresses/{address}/balance` for one address, `POST /addresses/balances` for more
than one, and `GET /info/health` for health.

## What was confirmed against the vendor's documentation, and when

Read off the live OpenAPI document on **2026-09-22**, and separated from what was measured
and from what was assumed, because the next person cannot tell the three apart otherwise
and will trust them equally.

**Confirmed, from the published OpenAPI document:**

* `GET /addresses/{kaspaAddress}/balance` returns `{"address": string, "balance": integer}`,
  the balance in **sompi**, eight decimals. The only error it documents is **422**.
* `POST /addresses/balances`, body `{"addresses": [string]}`, returns an **array** of those
  same objects.
* `GET /info/health` returns `{"kaspadServers": [{"kaspadHost", "serverVersion",
  "isUtxoIndexed", "isSynced", "p2pId", "blueScore"}], "database": {"isSynced",
  "blueScore", "blueScoreDiff", "acceptedTxBlockTime", "acceptedTxBlockTimeDiff"}}`, and
  its own description says it answers **503** when the database lags by around ten minutes
  or no node is synced.

**Measured against the live service on 2026-09-23**, because the document is silent:

* **No `ratelimit-*` or `x-ratelimit-*` header on any response.** The service sits behind a
  CDN -- `Server: cloudflare`, `cf-cache-status`, `CF-RAY` -- so the realistic throttle is
  a 429 carrying `Retry-After`, which the shared transport has honoured since #6, or a 403,
  which the failover moves on from. `providers/http.py`'s `parse_rate_limit` exists for the
  self-hosted deployment that would send them and says in its own docstring that nothing in
  production exercises it.
* **`Cache-Control: public, max-age=8`** on the balance response *and on the error*. A
  balance read can therefore be served from an edge cache rather than from the index. Eight
  seconds is immaterial to a sync that runs on a schedule of minutes, so this changes no
  code -- it is recorded because the mechanism is invisible in the OpenAPI document, and
  whoever next asks "why did two reads a second apart return the same number" deserves to
  find the answer written down rather than rediscover it against a CDN.
* **One instance serves one network, by construction.** A `kaspatest:` address sent to the
  public instance is answered 422 quoting the server's own rule: the path must match
  `^kaspa:[a-z0-9]{61,63}$`. The prefix is a literal in that regex.
* **The vendor does not check the checksum.** That regex is prefix, charset and length, so
  a *mistyped* mainnet address matching it is accepted and answered with a balance -- `0`,
  for a wallet that does not exist. That is exactly the failure #5's codec was built to
  prevent: a typo that reports an empty wallet forever and looks no different from an empty
  one. **Our offline validation is strictly stronger than the vendor's**, and since
  2026-09-23 that is a measurement rather than a preference.

**Assumed, because nothing documents it:**

* **`MAX_ADDRESSES_PER_CALL`.** The document declares `addresses` as an array of strings
  with no `maxItems`, and the operation description names no ceiling. See the constant.
* **Anything about pagination or retention.** Neither matters for a balance read; both will
  matter for transaction history, and neither has been checked.

`docs/providers.md` carries the same split, and the dates, for a reader who never opens
this file.

## The parser is hand-written, and that is a disclosure decision rather than a taste

A pydantic model would be shorter. Its `ValidationError` **renders the input that failed**,
and the input here is a response body full of the owner's addresses -- which then travels
into a log the moment anything calls `logger.exception`. So every refusal below is raised by
hand, and every message names **a field, a type or a count** and never a value. No rejection
in this module contains an address or any part of a body.

## The batch answer is an array, which brings a correlation failure the single read cannot

| Case | Outcome |
|---|---|
| an entry whose `address` was not in this call | `ProviderResponseError` |
| **two entries for the same address** | `ProviderResponseError` -- new here |
| a requested address with no entry | zero, by `align_balances` |
| `balance` absent, non-integer, boolean or negative | `ProviderResponseError` |
| the body is not an array | `ProviderResponseError` |

**The duplicate case is the one worth stating.** The parser builds a `dict` for
`align_balances`, and a `dict` keeps the last value silently -- so two entries for one
address, with different balances, would resolve to whichever the vendor happened to send
second and no assertion in `align_balances` could ever see it. Both plausible resolutions,
first-wins and last-wins, are wrong, and a test asserting either one pins a coin flip. It is
refused at the point where both values still exist.

The unrequested-address check is made **per batch** rather than left to `align_balances`,
and that is not redundancy. `align_balances` sees the union of every batch against the whole
request, so an entry in the first batch naming an address from the third would pass it --
a correlation failure that the per-batch check catches and the whole-call check cannot see.
It also means a batch's entries can never collide with another batch's, which is why merging
them is a plain `update`.

## `pending` is `None`, never zero

The Kaspa REST balance endpoint exposes nothing about the mempool, so this provider never
sets `pending` and `align_balances` is called without the mapping. Every Kaspa balance
carries `None`.

**#8's issue text asks for zero and it predates #7.** Zero would be wrong in the precise way
#6 predicted: a Kaspa balance of zero pending and a Bitcoin balance of zero pending would
render identically while meaning different things -- one says "nothing is in the mempool",
the other says "nobody asked the mempool". A dashboard cannot honour a distinction it was
never given, and `None` is the whole reason `AddressBalance.pending` is `int | None`.

## Health is not a ping, and this vendor is the reason

Healthy requires a 200, `database.isSynced`, **and** at least one `kaspadServers` entry with
both `isSynced` and `isUtxoIndexed` true.

`isUtxoIndexed` is the one a reader would drop as redundant. It is not: a node without the
UTXO index is synced and simply cannot answer a balance query, which is precisely the state
where a ping-shaped health check says yes and every read fails. A node that is reachable but
not synced is worse still -- it returns balances that are *stale and well-formed*, and a
wrong number is worse than an error.

**`kaspadHost` must never leave this module.** It names the vendor's internal node topology,
and `ProviderHealth.detail` is rendered in an operations view and reaches a log. `detail`
says how many nodes were synced and indexed, never which or where.

This check is stricter than the vendor's own, which returns 503 only when its database lags.
If `isUtxoIndexed` means something narrower than the schema suggests, this reports unhealthy
where the vendor reports healthy -- a false alarm rather than a false balance, which is the
right direction to be wrong in. It is a guess about a field's meaning and is recorded as one.

## Nothing here logs

Not one call. The shared transport logs `"{scheme}://{host}/{label}"` and nothing else,
which is the only log contract in this package that is enforced rather than remembered. A
log line written here would bypass all of it, and this vendor puts the address in the path.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

import httpx

from portfolio.config import get_settings
from portfolio.domain.addresses import (
    AddressInvalidError,
    AddressRejection,
    KaspaNetwork,
    kaspa_network_of,
)
from portfolio.domain.chains import ChainKey
from portfolio.domain.chains import validate_address as validate_chain_address
from portfolio.providers.base import (
    ChainCapabilities,
    ProviderHealth,
    align_balances,
    chunk_addresses,
    decode_json,
    require_json_object,
)
from portfolio.providers.endpoints import FALLBACK, PRIMARY, EndpointSet
from portfolio.providers.errors import ProviderResponseError
from portfolio.providers.http import (
    ADDRESS_BALANCE,
    ADDRESS_BALANCES,
    ENDPOINT_EXTENSION,
    NODE_HEALTH,
)
from portfolio.providers.registry import register_chain_provider

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from portfolio.config import Settings
    from portfolio.domain.chains import ValidatedAddress
    from portfolio.providers.base import AddressBalance
    from portfolio.providers.endpoints import Endpoint

__all__ = [
    "ADDRESS_BALANCE_PATH",
    "BALANCES_PATH",
    "CAPABILITIES",
    "FALLBACK",
    "HEALTH_PATH",
    "KASPA_DECIMALS",
    "MAX_ADDRESSES_PER_CALL",
    "PRIMARY",
    "VENDOR",
    "KaspaProvider",
    "NodeHealth",
    "parse_address_balance",
    "parse_balances",
    "parse_health",
]

KASPA_DECIMALS: Final = 8
"""Sompi to KAS. Carried on every balance as well as on the capabilities, so a stored
reading stays interpretable without asking which provider produced it."""

MAX_ADDRESSES_PER_CALL: Final = 64
"""**A guess, and this docstring is where it says so.**

The OpenAPI document declares `addresses` as an array of strings with **no `maxItems`**,
and the operation description names no ceiling -- confirmed against the live document on
2026-09-22. Sixty-four is large enough that any realistic portfolio is one request and small
enough that a request body stays a few kilobytes.

`chunk_addresses` sizes every call from this, so correcting it is a change to a constant. A
batch the server refuses raises a `ProviderResponseError` naming **the size of the batch**
and never its contents, which is the number an operator can act on; the contents are the
owner's holdings. That refusal is the first real evidence anyone will have, which is why it
is written to carry the number.
"""

ADDRESS_BALANCE_PATH: Final = "/addresses/{address}/balance"
BALANCES_PATH: Final = "/addresses/balances"
HEALTH_PATH: Final = "/info/health"

VENDOR: Final = "Kaspa REST"
"""What this provider's upstream is called in an exhaustion message.

The software's name, never a host. It is rendered into a `ProviderError`, which reaches a
log and a traceback, and naming the deployment there is the disclosure `request_target`
exists to prevent.

`PRIMARY` and `FALLBACK` come from `providers/endpoints.py`, since every provider with a
fallback calls its positions the same two things.
"""

# The field names, written down once. A typo in one of these is a parser that refuses every
# well-formed response, which is a failure mode worth making greppable. The camel case is
# the vendor's and is transcribed rather than translated.
ADDRESS_FIELD: Final = "address"
BALANCE_FIELD: Final = "balance"
ADDRESSES_FIELD: Final = "addresses"
KASPAD_SERVERS: Final = "kaspadServers"
DATABASE_FIELD: Final = "database"
IS_SYNCED: Final = "isSynced"
IS_UTXO_INDEXED: Final = "isUtxoIndexed"

CAPABILITIES: Final = ChainCapabilities(
    chain_key=ChainKey.KASPA,
    decimals=KASPA_DECIMALS,
    max_addresses_per_call=MAX_ADDRESSES_PER_CALL,
)


@dataclass(frozen=True, slots=True)
class NodeHealth:
    """What `GET /info/health` said, reduced to the three facts a verdict needs.

    A named triple rather than a bare `bool`, because the counts are what `detail` is built
    from and a boolean would force the provider to re-read the body to say anything useful.

    **`kaspadHost`, `serverVersion` and `p2pId` are deliberately not here.** Each names the
    vendor's internal node topology, and everything on this object is eligible to reach
    `ProviderHealth.detail`, which is rendered in an operations view and reaches a log. The
    parser drops them rather than the provider remembering not to render them: a field that
    was never carried cannot be leaked by the next person who writes a helpful message.

    `usable_nodes` counts the entries that are **both** synced and UTXO-indexed, which is
    the only kind that can answer a balance query.
    """

    database_synced: bool
    usable_nodes: int
    nodes: int


def parse_address_balance(body: str | bytes, expected_address: str) -> int:
    """The sompi balance out of a single-address response, or a refusal.

    Hand-written rather than a pydantic model, for the reason the module docstring gives at
    length. **No message raised from here contains the address or any part of the body**; a
    message names a field and a type, which is the part anyone can act on.

    The refusals, and why each one is a refusal rather than a zero:

    | Condition | Why it is not survivable |
    |---|---|
    | the body is not JSON | an HTML holding page is not a balance |
    | the body is not an object | neither is a list or a number |
    | `address` is absent, mistyped, or a different address | see below |
    | `balance` absent, not an integer, or a `bool` | `1.0e8` out of `json.loads` is a float |
    | `balance` negative | an address cannot hold less than nothing |

    **The echoed address is checked against the one we asked about**, and the three ways it
    can be wrong are one refusal because they have one remedy. It catches a cache or a proxy
    answering about somebody else -- the correlation failure `align_balances` refuses for a
    batch, which a single-address API can produce just as easily, and which would otherwise
    be reported as somebody else's balance under this address's name. That is not
    hypothetical here: the balance endpoint is served through a CDN with an eight-second
    cache in front of it.

    A `bool` is refused with everything else for the reason `domain/money.py` gives: it is
    an `int` subclass, so `True` would pass an `isinstance(..., int)` check and be reported
    as a holding of one sompi.

    Args:
        body: the response body, as text or as bytes.
        expected_address: the canonical address this response is supposed to be about.

    Returns:
        The balance in sompi.

    Raises:
        ProviderResponseError: any row of the table above.
    """
    document = require_json_object(body)
    if document.get(ADDRESS_FIELD) != expected_address:
        message = (
            f"The response does not carry the {ADDRESS_FIELD!r} it was asked about, "
            "so it cannot be matched to the request."
        )
        raise ProviderResponseError(message)
    return _require_sompi(document.get(BALANCE_FIELD))


def parse_balances(body: str | bytes, requested: Collection[str]) -> dict[str, int]:
    """Every balance in a batch response, keyed by address, or a refusal.

    The array shape is where this parser earns its keep; see the module docstring's table.
    Three things are checked that the single-address parser has no way to need:

    * **the body is an array**, not an object -- the single read's shape returned by a proxy
      that rewrote the endpoint would otherwise be read as "no entries" and become zeros;
    * **every entry was asked about in this batch**, which catches a correlation failure
      `align_balances` structurally cannot see once several batches are merged;
    * **no address appears twice**, refused while both values still exist, because a `dict`
      would silently keep whichever the vendor happened to send second.

    An address that was requested and does not appear is **not** an error and is not
    represented here: `align_balances` reads its absence as a zero, which is what an unused
    address means on chain. That is criterion 3, and it is deliberately not a check in this
    function -- a parser that refused a short answer would turn an empty wallet into a
    failed sync.

    Every message names a count or a type. None names an address: which address a vendor
    mangled is the owner's holdings, and the count is the part anyone can act on.

    Args:
        body: the response body, as text or as bytes.
        requested: the addresses this particular batch asked about.

    Returns:
        `{canonical address: sompi}` for every entry the response carried.

    Raises:
        ProviderResponseError: the body is not a JSON array, an entry is not an object, an
            `address` is missing or is not a string, an address was not requested in this
            batch, an address appears twice, or a `balance` is missing, not a whole number
            of sompi, or negative.
    """
    entries = _require_array(body)
    asked = set(requested)
    found: dict[str, int] = {}
    unrequested = 0
    for entry in entries:
        if not isinstance(entry, dict):
            message = (
                f"The response carried an entry that is a {type(entry).__name__} rather "
                "than the JSON object this endpoint documents."
            )
            raise ProviderResponseError(message)
        address = entry.get(ADDRESS_FIELD)
        if not isinstance(address, str):
            message = (
                f"The response carried an entry whose {ADDRESS_FIELD!r} is a "
                f"{type(address).__name__} rather than a string."
            )
            raise ProviderResponseError(message)
        if address not in asked:
            unrequested += 1
            continue
        if address in found:
            message = (
                "The response carried two entries for the same address, so which balance "
                "it means cannot be decided. Keeping either one would be a guess."
            )
            raise ProviderResponseError(message)
        found[address] = _require_sompi(entry.get(BALANCE_FIELD))

    if unrequested:
        message = (
            f"The response carried {unrequested} address(es) that this batch did not "
            "request, so it cannot be matched to the request."
        )
        raise ProviderResponseError(message)
    return found


def parse_health(body: str | bytes) -> NodeHealth:
    """The health report reduced to a synced flag and two counts, or a refusal.

    **Strict about the booleans on purpose.** A missing or mistyped `isSynced` is refused
    rather than read as `False`, because the two mean different things to an operator: a
    node reporting "not synced" is a vendor problem, and a body with no such field is a
    schema we no longer understand, which is a problem with this parser. `health()` turns
    the refusal into "unreadable health report", so the outcome is a false alarm rather
    than a false balance either way -- but only one of them sends a person to the right
    place.

    `kaspadHost`, `serverVersion` and `p2pId` are read past and never carried out. See
    `NodeHealth`.

    Raises:
        ProviderResponseError: the body is not a JSON object, `database` or `kaspadServers`
            is absent or the wrong shape, or a node's `isSynced`/`isUtxoIndexed` is absent
            or is not a boolean.
    """
    document = require_json_object(body)
    database = document.get(DATABASE_FIELD)
    if not isinstance(database, dict):
        message = (
            f"The health report's {DATABASE_FIELD!r} is a {type(database).__name__} rather "
            "than the object this endpoint documents."
        )
        raise ProviderResponseError(message)
    database_synced = _require_flag(database, DATABASE_FIELD, IS_SYNCED)

    servers = document.get(KASPAD_SERVERS)
    if not isinstance(servers, list):
        message = (
            f"The health report's {KASPAD_SERVERS!r} is a {type(servers).__name__} rather "
            "than the array this endpoint documents."
        )
        raise ProviderResponseError(message)

    usable = 0
    for server in servers:
        if not isinstance(server, dict):
            message = (
                f"The health report carried a node entry that is a "
                f"{type(server).__name__} rather than an object."
            )
            raise ProviderResponseError(message)
        synced = _require_flag(server, KASPAD_SERVERS, IS_SYNCED)
        indexed = _require_flag(server, KASPAD_SERVERS, IS_UTXO_INDEXED)
        if synced and indexed:
            usable += 1
    return NodeHealth(database_synced=database_synced, usable_nodes=usable, nodes=len(servers))


def _require_array(body: str | bytes) -> list[object]:
    """The body as a JSON array, or a refusal naming what it was instead.

    The counterpart to `require_json_object` for the one endpoint in this application whose
    documented top-level shape is a list. It is spelled out here rather than added to the
    shared helper as a "which shape did you want" argument, because that argument would be
    `isinstance` with a longer name and one more thing for a caller to get wrong.
    """
    document = decode_json(body)
    if not isinstance(document, list):
        message = (
            f"The response is a {type(document).__name__} rather than the JSON array "
            "this endpoint documents."
        )
        raise ProviderResponseError(message)
    return document


def _require_sompi(value: object) -> int:
    """One balance, refusing anything that is not a whole, non-negative number of sompi.

    **`json.loads` returns whatever the vendor sent**, and a declared type is a claim about
    what should arrive rather than a check that it did. A vendor rendering a balance as
    `1.0e8` produces a `float`, and a float that reached `AddressBalance.confirmed` would be
    money in binary floating point inside `providers/` -- where the AST ban in
    `backend/tests/security/test_no_float.py` cannot see it, because it reads source and
    this float has no literal and no `float` anywhere in this file.

    `bool` is refused with it: `True` is an `int` and would be read as one sompi.

    Negative is refused here rather than left to `align_balances`, which also refuses it.
    Two reasons, and the second is the one that matters: the single-address read never goes
    through `align_balances`'s `found` mapping with a negative in it without this, and a
    refusal raised where the field is named produces a message an operator can act on rather
    than a count.

    The message names the field and the type. It never names the value or the address,
    because both are figures about the owner's holdings.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        message = (
            f"The response field {BALANCE_FIELD!r} is a {type(value).__name__} rather than "
            "a whole number of sompi."
        )
        raise ProviderResponseError(message)
    if value < 0:
        message = f"The response reports a negative {BALANCE_FIELD!r}, which is not a balance."
        raise ProviderResponseError(message)
    return value


def _require_flag(document: Mapping[str, object], owner: str, name: str) -> bool:
    """One boolean out of a health document, refusing absent and mistyped alike.

    Absent and mistyped are one refusal deliberately: to a caller they are the same event --
    this report has no usable flag under that name -- and splitting them would produce two
    messages with one remedy. The type is named, so the message still says which happened.

    `isinstance(value, bool)` rather than truthiness, and that is the whole point. `"false"`
    is a non-empty string and therefore truthy, so a vendor that ever renders these as
    strings would make every node look synced and indexed -- a health check that answers yes
    while every balance read fails, which is the exact failure this endpoint exists to
    prevent.
    """
    value = document.get(name)
    if not isinstance(value, bool):
        message = (
            f"The health report's {owner}.{name} is a {type(value).__name__} rather than "
            "the boolean this endpoint documents."
        )
        raise ProviderResponseError(message)
    return value


@dataclass(frozen=True, slots=True)
class _Probe:
    """What one health probe concluded, and the one sentence an operator is told about it.

    The two travel together so they cannot disagree. `detail` is rendered in an operations
    view and reaches a log, so it carries a position and counts and **never** a
    `kaspadHost`, a URL, a body or an address.
    """

    healthy: bool
    detail: str


def _configured_candidates(settings: Settings) -> tuple[tuple[str, str], ...]:
    """The two configured URLs, in the order they should be tried, as `(position, url)`.

    All the blank-and-duplicate reasoning lives in `endpoints.configured_endpoints`. What
    stays here is which settings hold the URLs. The fallback ships blank because there is
    one well-known public kaspa-rest-server operator and no second one to name; a
    self-hoster fills it in.
    """
    return (
        (PRIMARY, settings.kaspa_api_url),
        (FALLBACK, settings.kaspa_api_fallback_url),
    )


@register_chain_provider(ChainKey.KASPA)
class KaspaProvider:
    """Reads Kaspa balances from a kaspa-rest-server instance, falling back to a second one.

    Satisfies `ChainProvider` structurally, checked by `mypy --strict` rather than by
    `isinstance`, and `ChainProviderFactory` by taking the shared client as its only
    positional argument -- which is what lets the registry build it with nothing but a
    client.
    """

    def __init__(self, client: httpx.AsyncClient, *, settings: Settings | None = None) -> None:
        """Bind to the shared client and read the configuration once.

        `settings` is a keyword with a default rather than a required argument, because
        `ChainProviderFactory` is `Callable[[httpx.AsyncClient], ChainProvider]` and a
        second required parameter would take this class out of that type. A test passes a
        `Settings` built in the test rather than monkeypatching the environment.

        The configuration is read **once, here**, rather than per request: a provider whose
        base URL could change between two batches of one call would produce a result set
        read from two different chains with nothing saying so.
        """
        resolved = settings if settings is not None else get_settings()
        self._client = client
        self._network = KaspaNetwork(resolved.kaspa_network)
        self._instances = EndpointSet.configured(
            client, _configured_candidates(resolved), vendor=VENDOR
        )

    @property
    def capabilities(self) -> ChainCapabilities:
        """Eight decimals, sixty-four addresses per call. Constant for this instance.

        Read off the module constant by name at call time, not copied onto the instance.
        `MAX_ADDRESSES_PER_CALL` is a guess with no vendor number behind it, and a test that
        wants to exercise the multi-batch path cannot assemble sixty-five real `kaspatest:`
        addresses -- rule 3 forbids minting more from the document's mainnet examples. So
        substituting a smaller declaration has to be possible, and it is exactly as possible
        as correcting the constant will be when the first refused batch says what the real
        ceiling is.
        """
        return CAPABILITIES

    def validate_address(self, raw: str) -> ValidatedAddress:
        """Decide whether `raw` is an address this instance can be asked about, offline.

        Two questions, and the second is the one that is new here. Whether the string is a
        Kaspa address at all is `domain.chains.validate_address`'s, and this delegates
        rather than reimplementing it -- #5 implemented the full 40-bit CashAddr checksum
        over the network prefix, verified against vectors from two unrelated publishers with
        an exhaustive single-character corruption sweep, so #8's "if the checksum variant
        proves ambiguous, degrade to prefix, charset and length" clause is satisfied by not
        being reached.

        Which *network* it is on is `kaspa_network_of`'s. A kaspa-rest-server instance
        serves exactly one network -- measured, not assumed: the public instance's own path
        validation hard-codes `kaspa:` -- so an address from another one gets a 422 at best.

        **The offline refusal is the stronger check and that is now measured.** The vendor
        validates prefix, charset and length and *not* the checksum, so a mistyped mainnet
        address matching its regex is answered with a balance of zero rather than refused --
        a wallet that reads empty forever and looks no different from an empty one.

        Synchronous, and it must stay synchronous: it opens no socket and reads no clock,
        which is what lets a caller tell a mistyped address from an unreachable API without
        a round trip.

        Raises:
            AddressInvalidError: not a Kaspa address, or an address on a Kaspa network this
                provider is not configured for. The rejection names a reason and never
                contains `raw`.
        """
        validated = validate_chain_address(ChainKey.KASPA.value, raw)
        if kaspa_network_of(validated.canonical) is not self._network:
            raise AddressInvalidError(AddressRejection.WRONG_NETWORK)
        return validated

    async def fetch_balances(self, addresses: Sequence[str]) -> Sequence[AddressBalance]:
        """Read every address, in order, batching as the capabilities allow.

        **Every address is validated before any URL or body is built, and that ordering is
        a security property rather than tidiness.** The address arrives from a database
        column; interpolating a database value into a URL path is the shape of a
        path-traversal bug, and the only thing standing between it and
        `GET /addresses/../../info/health` is that somebody validated it first. After
        `validate_address` the string is a Kaspa address -- a known prefix, a colon, and
        charset characters, with no slash, no dot and no percent-escape, by construction
        rather than by inspection.

        Validating the whole list up front rather than address by address is the other half
        of it: a bad address in position twelve costs no requests at all. The same applies
        to a duplicate, which `align_balances` would also refuse but only after every
        request had been made.

        **One address is a `GET`, more than one is a `POST`, and the split is per call
        rather than per request.** A batch of one is one address, and paying for a `POST` to
        ask about it buys nothing: the `GET` is retryable by default, cacheable by every
        intermediary, and carries no body to replay. So sixty-five addresses at a call size
        of sixty-four is one batch and one single read, not two batches.

        Sequential, never `gather`: a `gather` would hand the limiter every acquisition at
        once and turn a per-host floor into a queue whose depth nobody bounded. Sticky
        failover across the calls, by carrying the answering endpoint's index forward.

        Raises:
            AddressInvalidError: one of the addresses is not one this instance can read.
            ProviderRateLimitedError: every instance answered 429, last one included.
            ProviderUnavailableError: no instance answered.
            ProviderResponseError: an instance refused the request, or answered with
                something that cannot be trusted. A refused **batch** names the size of the
                batch, which is the only number an operator can act on.
            ValueError: the same address was requested twice.
        """
        canonical = [self.validate_address(raw).canonical for raw in addresses]
        distinct = set(canonical)
        if len(distinct) != len(canonical):
            # Deliberately the same shape of message `align_balances` raises, because it is
            # the same mistake; what differs is only that this one costs no requests.
            message = (
                f"fetch_balances was given {len(canonical)} addresses "
                f"of which only {len(distinct)} are distinct"
            )
            raise ValueError(message)

        found: dict[str, int] = {}
        # Sticky within this call, and only within it: the index the next call starts from,
        # which is the one that last answered.
        start = 0
        for chunk in chunk_addresses(canonical, self.capabilities):
            if len(chunk) == 1:
                address = chunk[0]
                body, start = await self._instances.read(
                    ADDRESS_BALANCE_PATH.format(address=address), ADDRESS_BALANCE, start
                )
                found[address] = parse_address_balance(body, address)
                continue
            body, start = await self._read_batch(chunk, start)
            # A plain `update` and not a merge that re-checks for collisions: `parse_balances`
            # refuses an entry this batch did not request, and the chunks partition a list
            # already proved distinct, so no two batches can answer about one address.
            found.update(parse_balances(body, chunk))

        # `pending` is not passed at all, which is the short spelling of "this chain cannot
        # answer that". Every balance therefore carries `None`, never a zero. See the module
        # docstring: zero would be a different statement, and the wrong one.
        return align_balances(canonical, found, decimals=KASPA_DECIMALS)

    async def health(self) -> ProviderHealth:
        """Whether either instance is answering *usefully*, without reading any address.

        Not a ping. `GET /info/health` is the vendor's own verdict on its nodes and its
        database, and this reads it: a synced database and at least one node that is both
        synced and UTXO-indexed. A node without the UTXO index passes a ping and cannot
        answer a single balance.

        **This does not raise**, so that an operations view can report a broken vendor
        without having to catch anything. `detail` carries which position answered and how
        many nodes were usable -- **never** a `kaspadHost`, a URL, a body or an address. A
        base URL that could not be requested at all is refused upstream, at startup, by
        `config.provider_url_violation`, so nothing here catches an exception that cannot
        arrive.
        """
        outcome = _Probe(healthy=False, detail="no endpoint configured")
        for instance in self._instances.endpoints:
            outcome = await self._probe(instance)
            if outcome.healthy:
                break
        return ProviderHealth(
            chain_key=ChainKey.KASPA, healthy=outcome.healthy, detail=outcome.detail
        )

    async def _probe(self, instance: Endpoint) -> _Probe:
        """Ask one instance for its health report: is it usable, and what do we say about it.

        A record rather than an exception, because the caller's job is to try the next one
        and then report -- and because every string built here is rendered to an operator,
        so they are built in one place where the no-host, no-URL, no-body, no-address rule
        is visible rather than assembled from an exception somewhere else.

        A record rather than `str | None` as Bitcoin's probe uses, because this vendor's
        healthy answer carries information -- how many nodes could actually answer -- and
        `None` has nowhere to put it. Two return values would have to be stitched back
        together by the caller, which is the shape `_Failure` in `endpoints.py` exists to
        stop.
        """
        try:
            response = await self._client.get(
                instance.url(HEALTH_PATH),
                extensions={ENDPOINT_EXTENSION: NODE_HEALTH},
            )
        except httpx.TransportError as error:
            # The class name, not `str(error)`: `httpx` puts the request's URL into some of
            # its messages, and the URL carries the deployment.
            return _Probe(healthy=False, detail=f"{instance.position}: {type(error).__name__}")
        if response.status_code != HTTPStatus.OK:
            # The vendor documents 503 for "the database lags by around ten minutes, or no
            # node is synced", which is exactly this branch and needs no special case.
            return _Probe(healthy=False, detail=f"{instance.position}: HTTP {response.status_code}")
        try:
            report = parse_health(response.text)
        except ProviderResponseError:
            return _Probe(healthy=False, detail=f"{instance.position}: unreadable health report")
        if not report.database_synced:
            return _Probe(
                healthy=False,
                detail=f"{instance.position}: the index database is not synced",
            )
        if report.usable_nodes == 0:
            return _Probe(
                healthy=False,
                detail=(
                    f"{instance.position}: no node is synced and UTXO-indexed "
                    f"({report.nodes} reporting)"
                ),
            )
        return _Probe(
            healthy=True,
            detail=(
                f"{instance.position}: {report.usable_nodes} of {report.nodes} "
                "node(s) synced and indexed"
            ),
        )

    async def _read_batch(self, chunk: Sequence[str], start: int) -> tuple[str, int]:
        """Post one batch, re-raising a refusal as one that names the batch's size.

        `MAX_ADDRESSES_PER_CALL` is a guess -- the OpenAPI document declares no `maxItems`
        and the operation description names no ceiling -- so the first real evidence anyone
        will have is a server refusing a batch. The size is the number that makes that
        evidence actionable, and it is the only thing added: the contents are the owner's
        holdings.

        **Only a refusal is wrapped.** A 5xx stays a `ProviderUnavailableError` untouched,
        because an outage is not evidence that sixty-four is too large, and a 429 stays a
        `ProviderRateLimitedError` for the same reason. Wrapping those would send an operator
        to correct a constant that was never wrong.

        There is deliberately **no fallback to single reads.** A batch the server refuses is
        a configured batch size that is too large, which is a value to correct rather than a
        path to code around -- and a silent fallback would turn sixty-four requests' worth of
        pacing into a number nobody chose, at a vendor whose limit is unpublished.
        """
        try:
            return await self._instances.post(
                BALANCES_PATH, ADDRESS_BALANCES, start, json={ADDRESSES_FIELD: list(chunk)}
            )
        except ProviderResponseError as refusal:
            message = (
                f"A batch of {len(chunk)} addresses was refused. {refusal} If that is this "
                "server's ceiling on a batch, lower max_addresses_per_call: the OpenAPI "
                "document declares no maximum, so the shipped value is a guess."
            )
            raise ProviderResponseError(message) from refusal
