"""Bitcoin balances from an Esplora instance, with a second instance behind the first.

The first provider to travel through the seam #6 built. It reads
`GET /address/:address`, derives the confirmed balance from `chain_stats` and the pending
delta from `mempool_stats`, and answers `GET /blocks/tip/height` for health.

## What was confirmed against the vendors' documentation, and when

Read on **2026-09-22**, and separated from what was assumed because the next person cannot
tell the difference otherwise and will trust both equally.

**Confirmed**, from Blockstream's published `API.md` and mempool.space's REST
documentation:

* `GET /address/:address` returns `address`, `chain_stats` and `mempool_stats`, each stat
  object carrying `tx_count`, `funded_txo_count`, `funded_txo_sum`, `spent_txo_count` and
  `spent_txo_sum`. The sums are in **satoshis**.
* `GET /blocks/tip/height` returns the height of the last block, as a plain integer body.
* The public base URLs are `https://blockstream.info/api` (with `/testnet/api` and
  `/signet/api` for the other networks) and `https://mempool.space/api` (with
  `https://mempool.space/testnet/api`).
* mempool.space states that exceeding its limits returns HTTP 429 and that repeatedly
  exceeding them may result in a ban. It publishes **no numbers**. Blockstream documents
  no rate limit at all.

**Assumed, because neither vendor documents it:**

* **What either instance answers for an address it considers invalid.** Neither documents
  an error body or even a status for that case, which is why every mapping below is
  written against the *status code* alone -- the part both vendors do have to get right --
  and why a wrong-network address is refused here, offline, rather than by asking.
* That `mempool_stats` is always present in practice. Its absence is read as "this
  instance cannot tell you", not as a zero.
* Anything about pagination or retention. Neither matters for a balance read; both will
  matter for transaction history, and neither has been checked.

`docs/providers.md` carries the same split, and the date, for a reader who never opens
this file.

## The parser is hand-written, and that is a disclosure decision rather than a taste

A pydantic model would be shorter. Its `ValidationError` **renders the input that
failed**, and the input here is a response body containing the owner's address -- which
then travels into a log the moment anything calls `logger.exception`. That is #44 arriving
through a different door, and the same defect #5 found in `services/wallets.py`.

So every refusal below is raised by hand, and every message names **a field and a type**
and never a value. No rejection in this module contains an address or any part of a body.

## Two instances, and where the failover rule now lives

**`providers/endpoints.py` owns it**, and this module owns nothing of it but the two
settings it reads and the vendor name that appears in an exhaustion message. #7 wrote the
loop here and review corrected it here; #8 added a second provider that needs the same
rule, and a rule corrected once in review must not exist twice. Read `endpoints.py` for the
argument in full. The three sentences that matter to a reader of this file:

* **Every failure to answer moves to the next instance** -- a transport error, a 5xx, a
  429, a 403, a 401, a 404, a 3xx. The rule it replaced stopped on any 4xx, which sounds
  right and made the fallback unreachable in exactly the cases a fallback exists for: a ban
  mempool.space does not document the status of, a self-hosted Esplora behind an auth proxy
  returning 401, a base URL that forgot its `/api` returning 404 forever.
* **A 200 whose body will not parse still stops the call**, and that asymmetry is the
  point. A non-200 is one instance declining to answer; a 200 we cannot read is a statement
  about our parser or the vendor's schema, and a second opinion would either repeat it or
  hide it behind a number. That decision is here, in `fetch_balances`, because only this
  module knows what a body means.
* **Failover is sticky within one `fetch_balances` call and resets between calls.** Reading
  twenty addresses against an instance that just refused the first is how a soft throttle
  becomes the ban mempool.space warns about; an instance throttled five minutes ago is the
  one we would rather be using now.

The reads are sequential. `max_addresses_per_call` is 1, so twenty addresses is twenty
calls spaced by `HostRateLimiter`; a `gather` would hand the limiter twenty simultaneous
acquisitions and turn a floor into a queue whose depth nobody bounded.

## Nothing here logs

Not one call. The shared transport logs `"{scheme}://{host}/{label}"` and nothing else,
which is the only log contract in this package that is enforced rather than remembered. A
log line written here would bypass all of it, and both vendors put the address in the path.
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
    BitcoinNetwork,
    bitcoin_network_of,
)
from portfolio.domain.chains import ChainKey
from portfolio.domain.chains import validate_address as validate_chain_address
from portfolio.providers.base import (
    ChainCapabilities,
    ProviderHealth,
    align_balances,
    decode_json,
    require_json_object,
)
from portfolio.providers.endpoints import FALLBACK, PRIMARY, EndpointSet
from portfolio.providers.errors import ProviderResponseError
from portfolio.providers.http import ADDRESS_BALANCE, BLOCK_TIP_HEIGHT, ENDPOINT_EXTENSION
from portfolio.providers.registry import register_chain_provider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from portfolio.config import Settings
    from portfolio.domain.chains import ValidatedAddress
    from portfolio.providers.base import AddressBalance
    from portfolio.providers.endpoints import Endpoint

__all__ = [
    "ADDRESS_PATH",
    "BITCOIN_DECIMALS",
    "CAPABILITIES",
    "FALLBACK",
    "PRIMARY",
    "TIP_HEIGHT_PATH",
    "VENDOR",
    "AddressStats",
    "EsploraProvider",
    "parse_address_response",
    "parse_tip_height",
]

BITCOIN_DECIMALS: Final = 8
"""Satoshis to bitcoin. Carried on every balance as well as on the capabilities, so a
stored reading stays interpretable without asking which provider produced it."""

MAX_ADDRESSES_PER_CALL: Final = 1
"""Esplora documents a single-address balance endpoint and no batch endpoint at all."""

ADDRESS_PATH: Final = "/address/{address}"
TIP_HEIGHT_PATH: Final = "/blocks/tip/height"

VENDOR: Final = "Esplora"
"""What this provider's upstream is called in an exhaustion message.

The software's name, never a host. It is rendered into a `ProviderError`, which reaches a
log and a traceback, and naming the deployment there is the disclosure `request_target`
exists to prevent.

`PRIMARY` and `FALLBACK` are re-exported from `providers/endpoints.py` rather than defined
here, since every provider with a fallback calls its positions the same two things.
"""

# The field names, written down once. A typo in one of these is a parser that refuses
# every well-formed response, which is a failure mode worth making greppable.
ADDRESS_FIELD: Final = "address"
CHAIN_STATS: Final = "chain_stats"
MEMPOOL_STATS: Final = "mempool_stats"
FUNDED_SUM: Final = "funded_txo_sum"
SPENT_SUM: Final = "spent_txo_sum"

CAPABILITIES: Final = ChainCapabilities(
    chain_key=ChainKey.BITCOIN,
    decimals=BITCOIN_DECIMALS,
    max_addresses_per_call=MAX_ADDRESSES_PER_CALL,
)


@dataclass(frozen=True, slots=True)
class AddressStats:
    """One address's two numbers, as the parser read them out of a response.

    A named pair rather than a `tuple[int, int | None]`, because `stats.pending` at a call
    site says what `parsed[1]` does not -- and because the second element is the one whose
    `None` carries meaning, which is exactly the element a positional tuple hides.

    `confirmed` is `chain_stats.funded_txo_sum - spent_txo_sum` and cannot be negative.
    `pending` is the same difference over `mempool_stats`, is **signed**, and is `None`
    when the response carried no mempool figures at all.
    """

    confirmed: int
    pending: int | None


def parse_address_response(body: str | bytes, expected_address: str) -> AddressStats:
    """Read the two balances out of an Esplora address response, or refuse it.

    Hand-written rather than a pydantic model, for the reason the module docstring gives
    at length: a `ValidationError` renders the input, and the input contains the owner's
    address. **No message raised from here contains the address or any part of the body**;
    each names a field and a type, which is the part anyone can act on.

    The refusals, and why each one is a refusal rather than a zero:

    | Condition | Why it is not survivable |
    |---|---|
    | the body is not JSON | an HTML holding page is not a balance |
    | the body is not an object | neither is a list or a number |
    | `address` is absent, mistyped, or a different address | see below |
    | `chain_stats` absent or not an object | the confirmed balance has nowhere to come from |
    | a sum absent, not an integer, or a `bool` | `1.0e8` out of `json.loads` is a float |
    | `spent_txo_sum` over `funded_txo_sum` | an address cannot spend what it never received |
    | `mempool_stats` present but not an object | mistyped is an error; absent is not |

    **The echoed address is checked against the one we asked about**, and the three ways
    it can be wrong are one refusal because they have one remedy. It catches a cache or a
    proxy answering about somebody else -- the correlation failure `align_balances` already
    refuses for a batch, which a single-address API can produce just as easily and which
    would otherwise be reported as somebody else's balance under this address's name.

    **An absent `mempool_stats` is not an error.** It yields `pending=None`, because an
    instance that does not report a mempool is one that cannot answer rather than one
    answering zero. That distinction is the entire reason `AddressBalance.pending` is
    `int | None`.

    Args:
        body: the response body, as text or as bytes.
        expected_address: the canonical address this response is supposed to be about.

    Returns:
        The confirmed balance in satoshis, and the signed mempool delta or `None`.

    Raises:
        ProviderResponseError: any row of the table above.
    """
    document = require_json_object(body)
    if document.get(ADDRESS_FIELD) != expected_address:
        message = (
            "The response does not carry the 'address' it was asked about, "
            "so it cannot be matched to the request."
        )
        raise ProviderResponseError(message)

    confirmed = _require_stats_delta(document, CHAIN_STATS)
    if confirmed < 0:
        message = (
            f"The response reports a larger {CHAIN_STATS}.{SPENT_SUM} than "
            f"{CHAIN_STATS}.{FUNDED_SUM}, which would make the confirmed balance negative."
        )
        raise ProviderResponseError(message)

    # `in` rather than `.get(...) is None`, so that an explicit null is a mistyped field
    # and reaches the refusal below rather than being read as "no mempool figures".
    pending = _require_stats_delta(document, MEMPOOL_STATS) if MEMPOOL_STATS in document else None
    return AddressStats(confirmed=confirmed, pending=pending)


def parse_tip_height(body: str | bytes) -> int:
    """The chain tip height out of `GET /blocks/tip/height`, or a refusal.

    The documented body is a plain integer, which is also valid JSON, so this shares
    `require_json_object`'s decoder rather than parsing digits by hand -- `json.loads` already
    rejects the Unicode digits that `str.isdigit` accepts and `int` then reads as a number.

    **A non-negative `int` specifically**, so that an instance answering with an HTML
    holding page, a JSON error object or `-1` is reported as unhealthy rather than as
    healthy-and-wrong. A `bool` is refused with everything else for the reason
    `domain/money.py` gives: it is an `int` subclass, so `True` would pass an
    `isinstance(..., int)` check and be read as height 1.

    Raises:
        ProviderResponseError: the body is not JSON, or is not a non-negative whole number.
    """
    height = decode_json(body)
    if isinstance(height, bool) or not isinstance(height, int) or height < 0:
        message = (
            "The tip height is not a non-negative whole number; the body parsed as "
            f"{type(height).__name__}."
        )
        raise ProviderResponseError(message)
    return height


def _require_stats_delta(document: Mapping[str, object], field: str) -> int:
    """`funded_txo_sum - spent_txo_sum` out of one stats object, refusing anything else.

    Absent and mistyped are one refusal deliberately: to a caller they are the same event
    -- this response has no usable figures under that name -- and splitting them would
    produce two messages with one remedy. The type is named, so the message still says
    which of the two happened.

    Raises:
        ProviderResponseError: the field is missing, or is not an object, or either sum is
            missing or is not a whole number of satoshis.
    """
    stats = document.get(field)
    if not isinstance(stats, dict):
        message = (
            f"The response field {field!r} is a {type(stats).__name__} rather than the "
            "object this endpoint documents."
        )
        raise ProviderResponseError(message)
    return _require_sum(stats, field, FUNDED_SUM) - _require_sum(stats, field, SPENT_SUM)


def _require_sum(stats: Mapping[str, object], field: str, name: str) -> int:
    """One satoshi sum, refusing anything that is not a whole number of them.

    **`json.loads` returns whatever the vendor sent**, and the annotation above is a claim
    about what should arrive rather than a check that it did. A vendor rendering a balance
    as `1.0e8` produces a `float`, and a float that reached `AddressBalance.confirmed`
    would be money in binary floating point inside `providers/` -- where the AST ban in
    `backend/tests/security/test_no_float.py` cannot see it, because it reads source and
    this float has no literal.

    `bool` is refused with it: `True` is an `int` and would be read as one satoshi.

    The message names the field and the type. It never names the value, because the value
    is a figure about the owner's address.
    """
    value = stats.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        message = (
            f"The response field {field}.{name} is a {type(value).__name__} rather than "
            "a whole number of satoshis."
        )
        raise ProviderResponseError(message)
    return value


def _configured_candidates(settings: Settings) -> tuple[tuple[str, str], ...]:
    """The two configured URLs, in the order they should be tried, as `(position, url)`.

    All the blank-and-duplicate reasoning lives in `endpoints.configured_endpoints`, which
    is where the second provider needs it too: a blank fallback means "one instance only",
    both blank means no instance is configured at all, and two URLs that are the same after
    trimming are one instance rather than a fallback onto the host that just refused us.

    What stays here is the one thing that is genuinely Bitcoin's: *which* settings hold the
    URLs, and in which order.
    """
    return (
        (PRIMARY, settings.bitcoin_esplora_url),
        (FALLBACK, settings.bitcoin_esplora_fallback_url),
    )


@register_chain_provider(ChainKey.BITCOIN)
class EsploraProvider:
    """Reads Bitcoin balances from an Esplora instance, falling back to a second one.

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
        `Settings` built in the test rather than monkeypatching the environment, which is
        also how it gets a fallback URL it can assert against.

        The configuration is read **once, here**, rather than per request: a provider
        whose base URL could change between two addresses of one call would produce a
        result set read from two different chains with nothing saying so.
        """
        resolved = settings if settings is not None else get_settings()
        self._client = client
        self._network = BitcoinNetwork(resolved.bitcoin_network)
        self._instances = EndpointSet.configured(
            client, _configured_candidates(resolved), vendor=VENDOR
        )

    @property
    def capabilities(self) -> ChainCapabilities:
        """Eight decimals, one address per call. Constant for the life of the instance."""
        return CAPABILITIES

    def validate_address(self, raw: str) -> ValidatedAddress:
        """Decide whether `raw` is an address this instance can be asked about, offline.

        Two questions, and the second is the one that is new here. Whether the string is a
        Bitcoin address at all is `domain.chains.validate_address`'s, and this delegates
        rather than reimplementing it. Which *network* it is on is `bitcoin_network_of`'s,
        and it matters because an Esplora instance serves exactly one network: an address
        from another one either gets an undocumented error -- neither vendor says which --
        or, far worse, a balance read from the wrong chain, which is a number rather than
        an error and which nothing downstream can tell from a right one.

        Synchronous, and it must stay synchronous: it opens no socket and reads no clock,
        which is what lets a caller tell a mistyped address from an unreachable API
        without a round trip.

        Raises:
            AddressInvalidError: not a Bitcoin address, or an address on a Bitcoin network
                this provider is not configured for. The rejection names a reason and
                never contains `raw`.
        """
        validated = validate_chain_address(ChainKey.BITCOIN.value, raw)
        if bitcoin_network_of(validated.canonical) is not self._network:
            raise AddressInvalidError(AddressRejection.WRONG_NETWORK)
        return validated

    async def fetch_balances(self, addresses: Sequence[str]) -> Sequence[AddressBalance]:
        """Read every address, in order, one request each.

        **Every address is validated before any URL is built, and that ordering is a
        security property rather than tidiness.** The address arrives from a database
        column; interpolating a database value into a URL path is the shape of a
        path-traversal bug, and the only thing standing between it and
        `GET /address/../../blocks/tip/height` is that somebody validated it first. After
        `validate_address` the string is bech32 or base58check -- alphanumeric, with no
        slash, no dot and no percent-escape, by construction rather than by inspection --
        so `ADDRESS_PATH.format` cannot produce a path that leaves the endpoint.

        Validating the whole list up front rather than address by address is the other
        half of it: a bad address in position twelve costs no requests at all, rather than
        eleven reads at a vendor whose rate limit is unpublished and enforced by ban.

        Sequential, never `gather`: see the module docstring. Sticky failover, also there.

        **A duplicate is refused here, before any request, and not only by
        `align_balances`.** `align_balances` does refuse it, and remains the enforcement
        point for every other caller -- but it runs last, so leaving it as the only check
        meant twenty duplicated addresses cost twenty requests at a vendor whose limit is
        unpublished and enforced by a ban, and then raised. That also contradicted the
        paragraph above it, which promises that a bad address costs no requests at all. A
        promise that holds for one kind of bad address is not the promise it appears to be.

        Raises:
            AddressInvalidError: one of the addresses is not one this instance can read.
            ProviderRateLimitedError: every instance answered 429, last one included.
            ProviderUnavailableError: no instance answered.
            ProviderResponseError: an instance refused the request, or answered with
                something that cannot be trusted.
            ValueError: the same address was requested twice.
        """
        canonical = [self.validate_address(raw).canonical for raw in addresses]
        distinct = set(canonical)
        if len(distinct) != len(canonical):
            # Deliberately the same shape of message `align_balances` raises, because it
            # is the same mistake; what differs is only that this one costs no requests.
            message = (
                f"fetch_balances was given {len(canonical)} addresses "
                f"of which only {len(distinct)} are distinct"
            )
            raise ValueError(message)

        confirmed: dict[str, int] = {}
        pending: dict[str, int] = {}
        # Sticky within this call, and only within it: the index the next address starts
        # from, which is the one that last answered.
        start = 0
        for address in canonical:
            body, start = await self._instances.read(
                ADDRESS_PATH.format(address=address), ADDRESS_BALANCE, start
            )
            stats = parse_address_response(body, address)
            confirmed[address] = stats.confirmed
            if stats.pending is not None:
                pending[address] = stats.pending

        # `pending` is handed over even when it is empty: `align_balances` reads a missing
        # address as "this chain did not say", which is exactly what an empty mapping
        # means here and is not the same statement as a zero.
        return align_balances(canonical, confirmed, decimals=BITCOIN_DECIMALS, pending=pending)

    async def health(self) -> ProviderHealth:
        """Whether either instance is answering, without reading any address.

        `GET /blocks/tip/height` is documented, cheap, and names nothing. The height is
        parsed rather than merely received, so an instance serving an HTML holding page
        with a 200 is unhealthy rather than healthy-and-wrong.

        **This does not raise**, so that an operations view can report a broken vendor
        without having to catch anything, and `detail` carries a reason and at most which
        position answered -- never a URL, never a body, never an address.

        That statement used to carry a residual, and the residual turned out to be a
        defect rather than a footnote. A base URL with no scheme or no host reaches
        `client.get` as a bare `ValueError` out of `urllib` -- not an `httpx` exception at
        all, so nothing here could have caught it by type, and "never raises" was simply
        untrue for three plausible typos. It is closed upstream instead:
        `config.provider_url_violation` refuses such a URL at startup, so no running
        application holds one. Nothing is caught here, because catching an exception that
        cannot arrive is a branch no test can reach and a claim no reader can check.
        """
        reason = "no endpoint configured"
        for instance in self._instances.endpoints:
            failure = await self._probe(instance)
            if failure is None:
                return ProviderHealth(
                    chain_key=ChainKey.BITCOIN, healthy=True, detail=instance.position
                )
            reason = failure
        return ProviderHealth(chain_key=ChainKey.BITCOIN, healthy=False, detail=reason)

    async def _probe(self, instance: Endpoint) -> str | None:
        """Ask one instance for the tip height. `None` if it answered, else why not.

        A string rather than an exception, because the caller's job is to try the next one
        and then report -- and because every one of these strings is rendered to an
        operator, so they are built here where the no-URL, no-body, no-address rule is
        visible rather than assembled from an exception somewhere else.
        """
        try:
            response = await self._client.get(
                instance.url(TIP_HEIGHT_PATH),
                extensions={ENDPOINT_EXTENSION: BLOCK_TIP_HEIGHT},
            )
        except httpx.TransportError as error:
            # The class name, not `str(error)`: `httpx` puts the request's URL into some
            # of its messages, and the URL carries the deployment.
            return f"{instance.position}: {type(error).__name__}"
        if response.status_code != HTTPStatus.OK:
            return f"{instance.position}: HTTP {response.status_code}"
        try:
            parse_tip_height(response.text)
        except ProviderResponseError:
            return f"{instance.position}: unreadable tip height"
        return None
