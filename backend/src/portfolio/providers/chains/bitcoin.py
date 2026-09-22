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

## Two instances, and what is worth moving on for

A transport error, a 5xx or a 429 that outlived the shared transport's retries moves to
the next instance. **Any other 4xx, and any 200 whose body does not parse, stops.** The
second instance runs the same software against the same chain, so it produces the same
refusal -- and if it does not, then two instances disagree about one request, which is a
fact worth surfacing rather than papering over with whichever answer came second.

Failover is **sticky within a single `fetch_balances` call**: once an instance fails, the
remaining addresses in that call start at the next one. Reading twenty addresses against an
instance that just refused the first is how a soft throttle becomes the ban mempool.space
warns about. It resets between calls, because an instance throttled five minutes ago is the
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

import json
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
from portfolio.providers.base import ChainCapabilities, ProviderHealth, align_balances
from portfolio.providers.errors import (
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from portfolio.providers.http import ADDRESS_BALANCE, BLOCK_TIP_HEIGHT, ENDPOINT_EXTENSION
from portfolio.providers.registry import register_chain_provider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from portfolio.config import Settings
    from portfolio.domain.chains import ValidatedAddress
    from portfolio.providers.base import AddressBalance

__all__ = [
    "ADDRESS_PATH",
    "BITCOIN_DECIMALS",
    "CAPABILITIES",
    "FALLBACK",
    "PRIMARY",
    "TIP_HEIGHT_PATH",
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

PRIMARY: Final = "primary"
FALLBACK: Final = "fallback"
"""What an instance is called in a `ProviderHealth.detail`.

A position rather than a URL, because `detail` is rendered in an operations view and
reaches a log, and a URL there would name the deployment. "the fallback answered" is the
whole of what an operator needs and the whole of what they are told.
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


@dataclass(frozen=True, slots=True)
class _Instance:
    """One configured Esplora instance: where it is, and what to call it in a log.

    `base_url` has already had its trailing slashes removed, so joining is concatenation
    and cannot produce a double slash -- which some reverse proxies answer with a 404 and
    some with a redirect, and this client does not follow redirects.
    """

    position: str
    base_url: str

    def url(self, path: str) -> str:
        """The absolute URL for `path`, which always begins with a slash."""
        return f"{self.base_url}{path}"


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
    document = _require_object(body)
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
    `_require_object`'s decoder rather than parsing digits by hand -- `json.loads` already
    rejects the Unicode digits that `str.isdigit` accepts and `int` then reads as a number.

    **A non-negative `int` specifically**, so that an instance answering with an HTML
    holding page, a JSON error object or `-1` is reported as unhealthy rather than as
    healthy-and-wrong. A `bool` is refused with everything else for the reason
    `domain/money.py` gives: it is an `int` subclass, so `True` would pass an
    `isinstance(..., int)` check and be read as height 1.

    Raises:
        ProviderResponseError: the body is not JSON, or is not a non-negative whole number.
    """
    height = _decode(body)
    if isinstance(height, bool) or not isinstance(height, int) or height < 0:
        message = (
            "The tip height is not a non-negative whole number; the body parsed as "
            f"{type(height).__name__}."
        )
        raise ProviderResponseError(message)
    return height


def _decode(body: str | bytes) -> object:
    """`json.loads`, with its failure translated into this package's vocabulary.

    `UnicodeDecodeError` is caught alongside `JSONDecodeError` because `json.loads` raises
    it for bytes that are not UTF-8, and a vendor answering with a mis-encoded body is the
    same class of event as one answering with an HTML page.

    The message says the body did not parse and **never shows it**. A parser error that
    quotes the offending text is the shape of defect this module was written to avoid.
    """
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        message = "The response body is not JSON."
        raise ProviderResponseError(message) from error


def _require_object(body: str | bytes) -> Mapping[str, object]:
    """The body as a JSON object, or a refusal naming what it was instead.

    Status is decided before this is ever called -- see `EsploraProvider._read`. A 502
    carrying an HTML error page is an unavailable upstream, not a schema error, and
    deciding that from the body would file it under "needs a human" forever.
    """
    document = _decode(body)
    if not isinstance(document, dict):
        message = (
            f"The response is a {type(document).__name__} rather than the JSON object "
            "this endpoint documents."
        )
        raise ProviderResponseError(message)
    return document


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


def _configured_instances(settings: Settings) -> tuple[_Instance, ...]:
    """The instances to try, in order, dropping any that is configured blank.

    A blank fallback means "one instance only", which is what a self-hoster running their
    own index sets. Both blank yields no instances at all: `fetch_balances` then raises
    `ProviderUnavailableError` and `health` reports unhealthy, which is what an operator
    who has configured no index should be told, rather than a zero balance.
    """
    candidates = (
        (PRIMARY, settings.bitcoin_esplora_url),
        (FALLBACK, settings.bitcoin_esplora_fallback_url),
    )
    return tuple(
        _Instance(position=position, base_url=url.strip().rstrip("/"))
        for position, url in candidates
        if url.strip()
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
        self._instances = _configured_instances(resolved)

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

        A duplicated address is refused by `align_balances`, as a `ValueError` and after
        the reads -- the caller's mistake rather than the vendor's, and one no caller in
        this application makes, because addresses come out of a unique index.

        Raises:
            AddressInvalidError: one of the addresses is not one this instance can read.
            ProviderRateLimitedError: every instance answered 429, last one included.
            ProviderUnavailableError: no instance answered.
            ProviderResponseError: an instance refused the request, or answered with
                something that cannot be trusted.
            ValueError: the same address was requested twice.
        """
        canonical = [self.validate_address(raw).canonical for raw in addresses]

        confirmed: dict[str, int] = {}
        pending: dict[str, int] = {}
        # Sticky within this call, and only within it: the index the next address starts
        # from, which is the one that last answered.
        start = 0
        for address in canonical:
            body, start = await self._read(
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

        The one residual: a base URL that `httpx` cannot parse at all raises
        `httpx.InvalidURL` from here. That is a configuration error rather than a vendor
        failure, it is the same on every request this provider makes, and it should be
        loud rather than reported as an unhealthy chain.
        """
        reason = "no endpoint configured"
        for instance in self._instances:
            failure = await self._probe(instance)
            if failure is None:
                return ProviderHealth(
                    chain_key=ChainKey.BITCOIN, healthy=True, detail=instance.position
                )
            reason = failure
        return ProviderHealth(chain_key=ChainKey.BITCOIN, healthy=False, detail=reason)

    async def _probe(self, instance: _Instance) -> str | None:
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

    async def _read(self, path: str, label: str, start: int) -> tuple[str, int]:
        """Read `path` from the first instance that answers, starting at `start`.

        Returns the body and the index of the instance that produced it, which the caller
        carries into the next address as `start`. That is the whole of the sticky-failover
        mechanism: an instance that failed is never asked again within one call, and
        nothing has to remember to skip it.

        The status mapping, and each row is a decision the hierarchy in `providers/errors`
        exists to keep apart:

        | Status | What happens |
        |---|---|
        | a transport error | the next instance; this one said nothing at all |
        | 429 | the next instance; it survived the retries, so our interval is too short |
        | 5xx | the next instance; the vendor is broken, not us |
        | any other non-200 | **stop**: it understood and refused, and the other instance will too |

        A 3xx lands in the last row with the 4xx. The shared client does not follow
        redirects -- a balance API that starts redirecting is a change to notice rather
        than absorb -- so a `Location` is a refusal to answer here, not a hop.

        Raises:
            ProviderResponseError: an instance refused the request.
            ProviderRateLimitedError: every instance was tried and the last said 429.
            ProviderUnavailableError: every instance was tried and none answered.
        """
        throttled = False
        cause: BaseException | None = None
        for index in range(start, len(self._instances)):
            instance = self._instances[index]
            try:
                response = await self._client.get(
                    instance.url(path), extensions={ENDPOINT_EXTENSION: label}
                )
            except httpx.TransportError as error:
                throttled, cause = False, error
                continue

            status = response.status_code
            if status == HTTPStatus.TOO_MANY_REQUESTS or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                throttled = status == HTTPStatus.TOO_MANY_REQUESTS
                continue
            if status != HTTPStatus.OK:
                message = f"An Esplora instance refused the request with HTTP {status}."
                raise ProviderResponseError(message)
            return response.text, index

        # Which error depends on the *last* failure, not on whether a 429 appeared at all:
        # a 429 followed by a 5xx is a broken vendor, and telling an operator to lengthen
        # an interval would send them after the wrong thing.
        if throttled:
            message = "Every Esplora instance is throttling us."
            raise ProviderRateLimitedError(message) from cause
        message = "No Esplora instance answered."
        raise ProviderUnavailableError(message) from cause
