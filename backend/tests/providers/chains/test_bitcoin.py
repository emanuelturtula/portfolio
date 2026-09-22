"""The Esplora Bitcoin provider: criteria 1, 2, 3, 4, 5 and 10 of #7.

Three things in this file carry more weight than the rest, and they are the three the spec
names.

**The failover tests count requests per host, not only the balances they got back.** A
provider that asked a throttled primary twenty times and then succeeded on the fallback
returns *exactly* the same balances as one that moved on after the first refusal. Every
assertion about the result passes for both. Only `fake.counts` and `fake.hosts_in_order`
tell them apart, and the difference between them is the difference between a slow sync and
an application banned from a free public index -- a failure that outlives the sync that
caused it.

**`test_no_parser_rejection_names_the_address` drives every rejection arm**, each with a
body built around a real testnet vector, and asserts the address appears in neither
`str(exc)` nor `exc.args`. A rejection whose message quotes the body it refused is the #44
shape, and it is easiest to introduce while writing a *helpful* error message.

**The parser is exercised directly as well as through the transport.** Nine refusal arms
reached only through a mock transport would be nine tests that also depend on the retry
loop, the limiter and the URL builder; a failure anywhere in that chain would be reported
as a parsing bug. The transport-level tests then check the thing the direct ones cannot:
that the provider actually calls the parser, and what it raises when it does.

Every address is testnet, signet or regtest. Rule 3, proved over this file by
`tests/security/test_address_logging.py::test_fixtures_contain_no_mainnet_address`.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import httpx
import pytest

from portfolio.config import Settings
from portfolio.domain.addresses import AddressInvalidError, AddressRejection
from portfolio.domain.chains import ChainKey
from portfolio.providers.chains.bitcoin import (
    ADDRESS_PATH,
    BITCOIN_DECIMALS,
    FALLBACK,
    PRIMARY,
    TIP_HEIGHT_PATH,
    AddressStats,
    EsploraProvider,
    parse_address_response,
    parse_tip_height,
)
from portfolio.providers.errors import (
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from portfolio.providers.http import (
    ADDRESS_BALANCE,
    BLOCK_TIP_HEIGHT,
    ENDPOINT_EXTENSION,
    HostRateLimiter,
    RetryPolicy,
    build_http_client,
)
from portfolio.providers.registry import CHAIN_PROVIDERS
from tests.address_vectors import (
    BECH32_CHARSET,
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WSH,
    BIP350_TESTNET_V1,
    BIP350_UNKNOWN_HRP,
    BITCOIN_VECTORS,
    CORE_REGTEST_P2SH,
    CORE_REGTEST_P2WPKH,
    CORE_SIGNET_P2PKH,
    CORE_TESTNET4_P2SH,
    KASPA_TESTNET_V0,
    NAMED_CORRUPTIONS,
    SYNTHETIC_TPUB,
    Vector,
    corruptions_of,
)
from tests.providers.chains.harness import (
    FALLBACK_HOST,
    FALLBACK_URL,
    PRIMARY_HOST,
    PRIMARY_URL,
    TIP_HEIGHT,
    EsploraFake,
    Reply,
    ScriptedInstance,
    balance_body,
    esplora_client,
    esplora_provider,
    esplora_settings,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from portfolio.providers.base import AddressBalance, ChainProvider

#: A whole coin in satoshis, and a smaller odd number. Neither is a round power of ten in
#: the other's units, so a dropped exponent or a swapped field is visible in the assertion
#: rather than plausible.
ONE_COIN: Final = 100_000_000
DUST: Final = 54_321

#: Four script types, one per acceptance criterion 1 row: P2PKH and P2SH in base58,
#: P2WPKH in bech32 and P2TR (witness version 1) in bech32m. Every one is testnet, signet
#: or regtest.
SCRIPT_TYPE_VECTORS: Final[tuple[tuple[str, str], ...]] = (
    ("p2pkh", CORE_SIGNET_P2PKH),
    ("p2sh", CORE_TESTNET4_P2SH),
    ("p2wpkh", BIP173_TESTNET_P2WPKH),
    ("p2tr", BIP350_TESTNET_V1),
)


def balances_by_address(balances: Sequence[AddressBalance]) -> dict[str, AddressBalance]:
    return {balance.address: balance for balance in balances}


async def _no_sleep(_milliseconds: int) -> None:
    """The injected sleep for the one test that builds its own client.

    Everything else goes through `tests/providers/chains/harness.py`, which owns the
    wiring. This exists because `test_a_provider_built_with_no_settings_uses_the_shipped_ones`
    must pass **no settings**, which is the one thing the harness cannot do.
    """
    return


# --------------------------------------------------------------------------------------
# Wiring: the provider is registered, declares what it can do, and satisfies the protocol
# --------------------------------------------------------------------------------------


def test_the_provider_is_what_the_registry_builds_for_the_bitcoin_key() -> None:
    """The registry's answer for `bitcoin` is this class, built from the shared client.

    Asserted through `CHAIN_PROVIDERS.create` rather than by reading the dictionary, so
    the factory signature -- one positional client and nothing else -- is exercised. That
    is the path #10 will use, and it is also the construction in which `settings` defaults
    to `get_settings()`; a keyword-only argument that had become required would fail here
    and nowhere else in this file.
    """
    import portfolio.providers.chains  # noqa: F401 - imported for its registration effect

    fake = EsploraFake()
    client = esplora_client(fake)

    provider = CHAIN_PROVIDERS.create(ChainKey.BITCOIN, client)

    assert isinstance(provider, EsploraProvider)
    assert provider.capabilities.chain_key is ChainKey.BITCOIN


def test_the_provider_declares_that_it_cannot_batch() -> None:
    """Esplora documents a single-address balance endpoint and no batch endpoint at all.

    `max_addresses_per_call = 1` is the declaration a caller sizes its work from, and
    `can_batch` is derived from it rather than stored beside it. Pinned as the integer,
    because `chunk_addresses` reads the integer and a boolean would not tell #10 how many
    addresses the *other* chain accepts.
    """
    fake = EsploraFake()
    provider, _client = esplora_provider(fake)

    capabilities = provider.capabilities

    assert capabilities.chain_key is ChainKey.BITCOIN
    assert capabilities.decimals == BITCOIN_DECIMALS == 8
    assert capabilities.max_addresses_per_call == 1
    assert capabilities.can_batch is False


# --------------------------------------------------------------------------------------
# Criterion 3: validation is offline, delegates to the domain, and precedes every URL
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vector", [pytest.param(vector, id=vector.id) for vector in BITCOIN_VECTORS]
)
def test_validate_address_accepts_every_published_vector(vector: Vector) -> None:
    """Bech32, bech32m and base58check, every published non-mainnet vector in the suite.

    The provider delegates to `domain.chains.validate_address` rather than reimplementing
    a codec -- two copies of a checksum rule is how they drift -- so this asserts the
    delegation produces the domain's own canonical and display forms, not merely that it
    did not raise.

    **Each vector is driven against a provider configured for its own network**, because
    one Esplora instance serves exactly one network and no single provider can accept both
    `tb1` and `bcrt1`. The network is derived from the vector's *spelling* rather than from
    `bitcoin_network_of`: deriving it from the function would make this test assert the
    function against itself, which is the shape that proves only that the code agrees with
    itself.

    Note which vectors are testnet under that rule. `CORE_REGTEST_P2SH` and
    `CORE_REGTEST_P2PKH` are base58 addresses on regtest and they read as **testnet**,
    because regtest shares version bytes 0x6F and 0xC4 with it and the string does not
    carry the fact. That looks like a bug until you know it, which is why it is written
    here and asserted in `tests/domain/test_bitcoin_network.py`.
    """
    network = "regtest" if vector.address.startswith("bcrt1") else "testnet"
    fake = EsploraFake()
    provider, _client = esplora_provider(fake, network=network)

    validated = provider.validate_address(vector.address)

    assert validated.canonical == vector.canonical
    assert validated.display == vector.display
    assert fake.counts == {PRIMARY_HOST: 0, FALLBACK_HOST: 0}


@pytest.mark.parametrize(
    ("name", "corrupted"),
    [pytest.param(name, corrupted, id=name) for name, _valid, corrupted in NAMED_CORRUPTIONS],
)
def test_a_one_character_corruption_is_refused(name: str, corrupted: str) -> None:
    """A validator that checked the prefix, the length and the alphabet would accept these.

    One wrong character in a stored address is a wallet that reports zero forever and
    looks no different from an empty one, which is the failure the checksum exists to
    prevent and the reason this is a named criterion rather than a nicety.
    """
    del name  # In the parameter id, where a failure can read it.
    fake = EsploraFake()
    provider, _client = esplora_provider(fake, network="testnet")

    with pytest.raises(AddressInvalidError):
        provider.validate_address(corrupted)

    assert fake.counts == {PRIMARY_HOST: 0, FALLBACK_HOST: 0}


def test_every_single_character_substitution_of_one_vector_is_refused() -> None:
    """The exhaustive sweep, not the hand-picked one.

    Bech32 guarantees detection of up to four substitutions, so *every* single-character
    substitution must be refused, not merely the one somebody wrote down. A validator that
    passed the named corruption above and failed here would be one that checks a shape.
    """
    fake = EsploraFake()
    provider, _client = esplora_provider(fake)
    survivors: list[tuple[int, str]] = []

    for position, corrupted in corruptions_of(BIP173_TESTNET_P2WPKH, BECH32_CHARSET):
        try:
            provider.validate_address(corrupted)
        except AddressInvalidError:
            continue
        survivors.append((position, corrupted))

    assert survivors == []


@pytest.mark.parametrize(
    ("network", "address", "id_"),
    [
        pytest.param("regtest", BIP173_TESTNET_P2WPKH, "testnet address on a regtest instance"),
        pytest.param("testnet", CORE_REGTEST_P2WPKH, "regtest address on a testnet instance"),
        pytest.param("regtest", CORE_REGTEST_P2SH, "base58 regtest, which reads as testnet"),
    ],
    ids=lambda value: value if isinstance(value, str) else repr(value),
)
def test_an_address_from_another_network_is_refused_without_a_request(
    network: str, address: str, id_: str
) -> None:
    """Criterion 3's new half, and the one with a cost attached to getting it wrong.

    Esplora's published API documents **no** error response for an invalid address, so
    "the instance will return 400" is an assumption about unspecified behaviour -- and the
    failure it would hide is the expensive one: a balance read from the wrong chain is a
    *number*, not an error, and nothing downstream can tell it from a right one.

    So the refusal is offline and the assertion is in two parts. The reason must be
    `WRONG_NETWORK`, and **the mock transport must have recorded zero requests** -- which
    is the half that says the address was never interpolated into a URL. The third row is
    the residual: a base58 regtest address is indistinguishable from a testnet one, so a
    regtest-configured provider refuses it rather than guessing, which is the conservative
    direction.
    """
    del id_  # In the parameter id, where a failure can read it.
    fake = EsploraFake()
    provider, _client = esplora_provider(fake, network=network)

    with pytest.raises(AddressInvalidError) as caught:
        provider.validate_address(address)

    assert caught.value.reason is AddressRejection.WRONG_NETWORK
    assert fake.counts == {PRIMARY_HOST: 0, FALLBACK_HOST: 0}
    assert fake.requests == []


def test_an_address_on_the_configured_network_is_accepted() -> None:
    """The control. A provider that refused everything would pass the test above.

    Without this, `WRONG_NETWORK` for every address at all would satisfy the refusal
    assertions and make the provider useless in a way no other test in this file would
    notice, because they all script their own instances.
    """
    fake = EsploraFake()
    testnet, _a = esplora_provider(fake, network="testnet")
    regtest, _b = esplora_provider(fake, network="regtest")

    assert testnet.validate_address(BIP173_TESTNET_P2WPKH).canonical == BIP173_TESTNET_P2WPKH
    assert regtest.validate_address(CORE_REGTEST_P2WPKH).canonical == CORE_REGTEST_P2WPKH


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(KASPA_TESTNET_V0, id="an address on another chain"),
        pytest.param(BIP350_UNKNOWN_HRP, id="an unknown human-readable prefix"),
        pytest.param(SYNTHETIC_TPUB, id="an extended public key, which is not an address"),
        pytest.param("", id="empty"),
        pytest.param("../../blocks/tip/height", id="a path traversal attempt"),
        pytest.param("tb1qw508d6q/../../blocks", id="a vector prefix with a traversal glued on"),
    ],
)
async def test_fetch_balances_refuses_a_bad_address_before_it_builds_a_url(raw: str) -> None:
    """The reason validation happens first, stated as the two path-traversal rows.

    The address arrives from a database column, and interpolating a database value into a
    URL path is the shape of a path-traversal bug: the only thing between it and
    `GET /address/../../blocks/tip/height` is that somebody validated it first. After
    validation the string is bech32 or base58check -- alphanumeric, no slash, no dot, no
    percent-escape -- by construction rather than by inspection.

    Asserted on both halves again: the typed error, and zero requests.
    """
    fake = EsploraFake()
    provider, client = esplora_provider(fake)

    async with client:
        with pytest.raises(AddressInvalidError):
            await provider.fetch_balances([raw])

    assert fake.requests == []


# --------------------------------------------------------------------------------------
# Criterion 1: the balances, and where they come from
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("script_type", "address"),
    [pytest.param(name, address, id=name) for name, address in SCRIPT_TYPE_VECTORS],
)
async def test_each_script_type_reads_its_confirmed_balance(script_type: str, address: str) -> None:
    """P2PKH, P2SH, P2WPKH and P2TR, one test each, as criterion 1 words it.

    The four differ only in how the address is *encoded*, which is exactly why a provider
    can pass for one and fail for another: the URL is built from the canonical form, and
    base58 is case sensitive while bech32 is not. A single-vector test would not see a
    provider that lower-cased everything on its way into the path.
    """
    del script_type  # In the parameter id.
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(funded=ONE_COIN + DUST, spent=DUST)),
    )
    provider, client = esplora_provider(fake)

    async with client:
        balances = await provider.fetch_balances([address])

    assert len(balances) == 1
    assert balances[0].address == address
    assert balances[0].confirmed == ONE_COIN
    assert balances[0].decimals == BITCOIN_DECIMALS
    assert balances[0].amount() == Decimal("1")
    # The address reached the instance exactly as stored, not lower-cased or re-encoded.
    assert fake.addresses_asked_of(PRIMARY_HOST) == [address]


async def test_confirmed_is_the_difference_of_the_chain_sums() -> None:
    """Esplora reports a balance as a derivation, never as a number.

    `chain_stats.funded_txo_sum - chain_stats.spent_txo_sum`. A provider that read
    `funded_txo_sum` alone reports the total ever received, which for a wallet that has
    ever spent anything is a number that is too large and entirely plausible -- the worst
    shape of wrong answer available in this file.

    The two sums are deliberately far apart, so reading the wrong field is visible in the
    assertion rather than arithmetically close.
    """
    fake = EsploraFake(primary=ScriptedInstance(Reply(funded=3 * ONE_COIN, spent=2 * ONE_COIN)))
    provider, client = esplora_provider(fake)

    async with client:
        balances = await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert balances[0].confirmed == ONE_COIN


async def test_every_requested_address_comes_back_in_order() -> None:
    """Same length, same order, each entry carrying the address it is about.

    Asserted as the whole tuple rather than as a length and a set: a length assertion
    passes for a reordering and a set assertion passes for a permutation -- and a
    permutation reports one wallet's balance against another wallet's address while every
    total stays plausible.

    Three addresses, because two cannot tell "in order" from "reversed", and the sums are
    distinct so a provider that answered the right addresses with the wrong balances fails
    here rather than passing on shape.
    """
    requested = (CORE_SIGNET_P2PKH, BIP350_TESTNET_V1, BIP173_TESTNET_P2WSH)
    sums = {CORE_SIGNET_P2PKH: 1, BIP350_TESTNET_V1: 2, BIP173_TESTNET_P2WSH: 3}

    def by_request(request: httpx.Request) -> httpx.Response:
        address = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=balance_body(address, funded=sums[address]))

    fake = EsploraFake()
    fake.primary.answer = by_request  # type: ignore[method-assign]
    provider, client = esplora_provider(fake)

    async with client:
        balances = await provider.fetch_balances(requested)

    assert tuple(balance.address for balance in balances) == requested
    assert tuple(balance.confirmed for balance in balances) == (1, 2, 3)


async def test_the_calls_are_sequential_and_not_gathered() -> None:
    """One address per call, in order, because the limiter is a floor and not a queue.

    Esplora declares `max_addresses_per_call = 1`, so twenty addresses is twenty calls
    spaced by the limiter. A `gather` would hand the limiter twenty simultaneous
    acquisitions and turn a floor into a queue whose depth nobody bounded -- and against
    a vendor whose documentation warns about bans, that is the difference between slow and
    blocked.

    Asserted as the order the instance was asked, which is what a `gather` would scramble.
    """
    requested = (CORE_SIGNET_P2PKH, BIP350_TESTNET_V1, BIP173_TESTNET_P2WSH, CORE_TESTNET4_P2SH)
    fake = EsploraFake()
    provider, client = esplora_provider(fake)

    async with client:
        await provider.fetch_balances(requested)

    assert fake.addresses_asked_of(PRIMARY_HOST) == list(requested)
    assert fake.counts == {PRIMARY_HOST: len(requested), FALLBACK_HOST: 0}


async def test_no_addresses_makes_no_requests_at_all() -> None:
    """An empty wallet table is not a reason to call a public index."""
    fake = EsploraFake()
    provider, client = esplora_provider(fake)

    async with client:
        balances = await provider.fetch_balances([])

    assert balances == ()
    assert fake.requests == []


async def test_the_same_address_requested_twice_is_the_callers_mistake() -> None:
    """A `ValueError`, not a `ProviderResponseError`: the caller made this mistake.

    `align_balances` decides it, and the provider must not swallow it into a vendor-shaped
    error on the way past -- the two deserve different blame and a caller catching
    `ProviderError` would otherwise see its own bug reported as the chain's.
    """
    fake = EsploraFake()
    provider, client = esplora_provider(fake)

    async with client:
        with pytest.raises(ValueError, match=r"distinct") as caught:
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH, BIP173_TESTNET_P2WPKH])

    assert not isinstance(caught.value, ProviderResponseError)
    # **Refused before any request, not after all of them.** `align_balances` catches the
    # duplicate, and it runs at the *end* of `fetch_balances` -- so a naive implementation
    # makes the full set of calls to a public index and then throws the answers away. The
    # `fetch_balances` docstring two paragraphs above the loop says validation happens
    # first; this is the assertion that makes that true rather than aspirational, and the
    # count is the only thing that can see the difference.
    assert fake.requests == []
    assert fake.counts == {PRIMARY_HOST: 0, FALLBACK_HOST: 0}


async def test_a_duplicate_in_a_long_request_costs_no_requests_at_all() -> None:
    """The same rule at the length where it costs something, and against a paced host.

    Twenty addresses with one repeated is a realistic wallet table after a careless
    import. Refusing after the fact means twenty requests, spaced by a one-second limiter,
    to a vendor that bans for volume -- twenty seconds of a public index's goodwill spent
    on an answer that is discarded.
    """
    requested = [BIP173_TESTNET_P2WPKH, BIP350_TESTNET_V1, BIP173_TESTNET_P2WSH] * 3
    fake = EsploraFake()
    provider, client = esplora_provider(fake)

    async with client:
        with pytest.raises(ValueError, match=r"distinct"):
            await provider.fetch_balances(requested)

    assert fake.requests == []


async def test_the_request_carries_the_documented_endpoint_label_and_path() -> None:
    """The path is Esplora's documented one and the label is the allowlisted constant.

    Both halves verified from outside the provider, off the request the fake received.
    The label is what `request_target` renders into a log, and a label that is not on
    `ENDPOINT_LABELS` renders `<unlabelled>` -- so a provider that misspelled it would be
    correct, quiet, and impossible to find in a production log.
    """
    fake = EsploraFake()
    provider, client = esplora_provider(fake)

    async with client:
        await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    request = fake.primary.requests[0]
    assert request.method == "GET"
    assert request.url.path == f"/api{ADDRESS_PATH.format(address=BIP173_TESTNET_P2WPKH)}"
    assert request.url.query == b""
    assert request.extensions.get(ENDPOINT_EXTENSION) == ADDRESS_BALANCE


# --------------------------------------------------------------------------------------
# Criterion 2: an unfunded address is a zero
# --------------------------------------------------------------------------------------


async def test_an_address_with_no_history_reads_zero_not_an_error() -> None:
    """Zero is what an unused address holds. That is what it means on chain.

    The failure this rules out is the one the whole error hierarchy exists for in reverse:
    an address with no history is not an absence, not a 404 to be reported as unavailable,
    and not an omission from the result. It is a balance of nothing.
    """
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(funded=0, spent=0, mempool_funded=0, mempool_spent=0))
    )
    provider, client = esplora_provider(fake)

    async with client:
        balances = await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert balances[0].confirmed == 0
    assert balances[0].pending == 0
    assert balances[0].amount() == Decimal("0")


# --------------------------------------------------------------------------------------
# Criterion 10: pending is a signed net delta, and its None means something
# --------------------------------------------------------------------------------------


async def test_pending_is_the_signed_net_mempool_delta() -> None:
    """`mempool_stats.funded_txo_sum - spent_txo_sum`, not the funded sum alone.

    An incoming payment in the mempool funds an output and spends none, so it reads
    positive; the confirmed balance is untouched by it, which is the other half of the
    claim and is asserted here so a provider that added the mempool into `confirmed`
    fails.
    """
    fake = EsploraFake(
        primary=ScriptedInstance(
            Reply(funded=ONE_COIN, spent=0, mempool_funded=DUST, mempool_spent=0)
        )
    )
    provider, client = esplora_provider(fake)

    async with client:
        balances = await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert balances[0].confirmed == ONE_COIN
    assert balances[0].pending == DUST


async def test_a_spend_in_the_mempool_reads_as_a_negative_pending() -> None:
    """The sign is the point, and a naive "balances cannot be negative" guard destroys it.

    An outgoing payment sitting in the mempool spends a confirmed output and funds
    nothing, so the net delta is negative -- which is exactly right. Spendable is
    `confirmed + pending`; #11 computes it and gets a sign that already works.
    """
    fake = EsploraFake(
        primary=ScriptedInstance(
            Reply(funded=ONE_COIN, spent=0, mempool_funded=0, mempool_spent=DUST)
        )
    )
    provider, client = esplora_provider(fake)

    async with client:
        balances = await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert balances[0].pending == -DUST
    assert balances[0].confirmed + (balances[0].pending or 0) == ONE_COIN - DUST


async def test_a_response_without_mempool_figures_reports_pending_as_unknown() -> None:
    """The condition #6 set for the field existing at all, met rather than overridden.

    An instance that does not report a mempool is one that **cannot answer**, not one
    answering zero. `None` says so; a zero would make the field mean two things and would
    be indistinguishable from an address with nothing pending -- which is the ambiguity #6
    refused the field over.
    """
    fake = EsploraFake(primary=ScriptedInstance(Reply(funded=ONE_COIN, mempool_funded=None)))
    provider, client = esplora_provider(fake)

    async with client:
        balances = await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert balances[0].pending is None
    assert balances[0].confirmed == ONE_COIN


async def test_pending_is_reported_per_address_and_not_shared_across_the_batch() -> None:
    """One address answering with no mempool must not blank the others.

    The provider reads addresses one at a time and builds one `pending` mapping for the
    whole call, so an entry left out for one address is exactly the place a zero-fill or a
    last-value-wins bug lives -- and both produce a plausible number for the wrong wallet.
    """
    with_mempool = BIP173_TESTNET_P2WPKH
    without_mempool = BIP350_TESTNET_V1

    def by_request(request: httpx.Request) -> httpx.Response:
        address = request.url.path.rsplit("/", 1)[-1]
        if address == without_mempool:
            return httpx.Response(200, content=balance_body(address, funded=1, mempool_funded=None))
        return httpx.Response(200, content=balance_body(address, funded=2, mempool_funded=DUST))

    fake = EsploraFake()
    fake.primary.answer = by_request  # type: ignore[method-assign]
    provider, client = esplora_provider(fake)

    async with client:
        balances = balances_by_address(
            await provider.fetch_balances([with_mempool, without_mempool])
        )

    assert balances[with_mempool].pending == DUST
    assert balances[without_mempool].pending is None


# --------------------------------------------------------------------------------------
# Criterion 4: failover, counted per host
# --------------------------------------------------------------------------------------


async def test_a_throttled_primary_is_retried_and_then_falls_over_to_the_fallback() -> None:
    """The two mechanisms compose: the transport retries first, then the provider moves on.

    Backoff is #6's and is already tested there; what this change owns is that **only a
    429 which survived the retry budget** reaches the fallback. So the counts are the
    assertion: three attempts against the primary, because `max_attempts=3`, and then one
    against the fallback. A provider that failed over on the first 429 would show
    `{primary: 1, fallback: 1}` and would throw away the retry policy; one that never
    failed over would show `{primary: 3, fallback: 0}` and would raise.
    """
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(status=429)),
        fallback=ScriptedInstance(Reply(funded=ONE_COIN)),
    )
    provider, client = esplora_provider(fake, max_attempts=3)

    async with client:
        balances = await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert balances[0].confirmed == ONE_COIN
    assert fake.counts == {PRIMARY_HOST: 3, FALLBACK_HOST: 1}
    assert fake.hosts_in_order == [PRIMARY_HOST] * 3 + [FALLBACK_HOST]


async def test_a_failed_primary_is_not_asked_again_for_the_rest_of_the_call() -> None:
    """Stickiness, which is the whole ban-avoidance argument in one assertion.

    Reading twenty addresses against an instance that just refused the first one is how a
    soft throttle becomes the ban mempool.space's documentation warns about. So once an
    endpoint fails, the remaining addresses in **this call** start at the next one.

    Four addresses and `max_attempts=2`: the primary is asked twice for the first address
    and never again, and the fallback answers four times. A provider without stickiness
    would show eight primary requests -- the same four balances, four times the ban risk.
    """
    requested = (BIP173_TESTNET_P2WPKH, BIP350_TESTNET_V1, BIP173_TESTNET_P2WSH, CORE_SIGNET_P2PKH)
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(status=429)),
        fallback=ScriptedInstance(Reply(funded=DUST)),
    )
    provider, client = esplora_provider(fake, max_attempts=2)

    async with client:
        balances = await provider.fetch_balances(requested)

    assert tuple(balance.address for balance in balances) == requested
    assert all(balance.confirmed == DUST for balance in balances)
    assert fake.counts == {PRIMARY_HOST: 2, FALLBACK_HOST: 4}
    # Twice, not once: `addresses_asked_of` is one entry per *request*, so the two
    # attempts the retry budget spends on the first address show up as two entries. That
    # is the fact this assertion is the only place to see -- a helper that de-duplicated
    # would hide the retry count behind a tidier list.
    assert fake.addresses_asked_of(PRIMARY_HOST) == [BIP173_TESTNET_P2WPKH] * 2
    assert fake.addresses_asked_of(FALLBACK_HOST) == list(requested)


async def test_the_next_call_starts_at_the_primary_again() -> None:
    """Stickiness resets between calls, deliberately.

    An instance that was throttled five minutes ago is the one we would rather be using
    now -- it is the primary because it is preferred, not because it happened to work
    once. A provider that remembered the failure on the instance would pin every later
    sync to the fallback for the life of the process, and nothing would ever say so.

    The second call is driven against a primary that has recovered, so "it asked the
    primary again" is visible as a balance as well as a count.
    """
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(status=429), Reply(status=429), Reply(funded=ONE_COIN)),
        fallback=ScriptedInstance(Reply(funded=DUST)),
    )
    provider, client = esplora_provider(fake, max_attempts=2)

    async with client:
        first = await provider.fetch_balances([BIP173_TESTNET_P2WPKH])
        second = await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert first[0].confirmed == DUST
    assert second[0].confirmed == ONE_COIN
    assert fake.counts == {PRIMARY_HOST: 3, FALLBACK_HOST: 1}
    assert fake.hosts_in_order == [PRIMARY_HOST, PRIMARY_HOST, FALLBACK_HOST, PRIMARY_HOST]


async def test_both_instances_throttled_raises_the_rate_limited_error() -> None:
    """Every endpoint exhausted, and the last failure was a 429, so the remedy is stated.

    `ProviderRateLimitedError` rather than the plain unavailable error, because the two
    have different remedies: an unavailable host is waited out, while being throttled
    means our own interval is too short for this vendor and the fix is a configuration
    change. It is a subclass, so a caller that only cares about "try later" catches the
    parent and does not have to enumerate both.

    The counts are asserted too: exhausting the budget on both is the correct amount of
    trying, and more than that is the ban.
    """
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(status=429)),
        fallback=ScriptedInstance(Reply(status=429)),
    )
    provider, client = esplora_provider(fake, max_attempts=2)

    async with client:
        with pytest.raises(ProviderRateLimitedError) as caught:
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert isinstance(caught.value, ProviderUnavailableError)
    assert fake.counts == {PRIMARY_HOST: 2, FALLBACK_HOST: 2}
    assert BIP173_TESTNET_P2WPKH not in str(caught.value)


async def test_a_server_error_on_both_instances_is_unavailable_rather_than_throttled() -> None:
    """The other arm of the same rule: the last failure decides which error is raised.

    Reporting a 503 as `ProviderRateLimitedError` would tell an operator to lengthen an
    interval that was never the problem, and reporting a 429 as plain unavailability
    throws away the only actionable fact in it. Both directions are wrong and only a test
    that drives both tells them apart.
    """
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(status=503)),
        fallback=ScriptedInstance(Reply(status=503)),
    )
    provider, client = esplora_provider(fake, max_attempts=2)

    async with client:
        with pytest.raises(ProviderUnavailableError) as caught:
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert not isinstance(caught.value, ProviderRateLimitedError)
    assert fake.counts == {PRIMARY_HOST: 2, FALLBACK_HOST: 2}


async def test_the_last_failure_decides_which_error_is_raised_not_the_worst_one() -> None:
    """A throttled primary and a broken fallback is unavailability, not throttling.

    The rule is "the last failure", and the alternative reading -- "a 429 appeared
    anywhere, so report throttling" -- sends an operator to lengthen an interval while the
    vendor that actually failed is returning 500s. Asserted in both orders, because a rule
    written as `if any(...)` passes one of them and a rule written as "the last" passes
    both.
    """
    throttle_then_break = EsploraFake(
        primary=ScriptedInstance(Reply(status=429)),
        fallback=ScriptedInstance(Reply(status=500)),
    )
    provider, client = esplora_provider(throttle_then_break, max_attempts=1)
    async with client:
        with pytest.raises(ProviderUnavailableError) as unavailable:
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])
    assert not isinstance(unavailable.value, ProviderRateLimitedError)

    break_then_throttle = EsploraFake(
        primary=ScriptedInstance(Reply(status=500)),
        fallback=ScriptedInstance(Reply(status=429)),
    )
    provider, client = esplora_provider(break_then_throttle, max_attempts=1)
    async with client:
        with pytest.raises(ProviderRateLimitedError):
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])


async def test_the_attached_cause_is_the_last_failure_and_not_an_earlier_one() -> None:
    """`raise ... from` has to name the instance that actually failed last.

    A primary that refuses the connection and a fallback that answers 429 must raise
    `ProviderRateLimitedError` **from** something describing the 429 -- not from the
    primary's `ConnectError`. The type and the cause would otherwise tell an operator two
    different stories about one sync: "you are being throttled", caused by "the connection
    was refused", which sends them to check a host that was never the problem.

    Asserted on `__cause__` and not only on the type, because **every existing assertion
    in this file passes either way**. That is the property that lets this stay wrong
    forever: the exception class is right, the message is right, and only the traceback --
    which nobody reads until an outage -- says something false.

    A `ConnectError` cause is asserted absent rather than a specific right answer being
    demanded, because "what a 429 with no exception behind it should be caused by" is the
    implementation's call: `None` is a perfectly good answer.
    """
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(error=httpx.ConnectError("connection refused"))),
        fallback=ScriptedInstance(Reply(status=429)),
    )
    provider, client = esplora_provider(fake, max_attempts=1)

    async with client:
        with pytest.raises(ProviderRateLimitedError) as caught:
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert not isinstance(caught.value.__cause__, httpx.ConnectError), (
        "the error is classified from the fallback's 429 but blamed on the primary's "
        "transport failure, so the type and the traceback disagree"
    )


async def test_the_cause_survives_when_the_last_failure_really_was_a_transport_error() -> None:
    """The control. A provider that simply stopped attaching a cause would pass the test
    above and throw away the one piece of information an outage leaves behind.

    Two transport errors: the cause must be the **second**, which is the one that decided
    the outcome.
    """
    last = httpx.ConnectTimeout("timed out")
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(error=httpx.ConnectError("connection refused"))),
        fallback=ScriptedInstance(Reply(error=last)),
    )
    provider, client = esplora_provider(fake, max_attempts=1)

    async with client:
        with pytest.raises(ProviderUnavailableError) as caught:
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert caught.value.__cause__ is last


async def test_a_transport_error_falls_over_to_the_fallback() -> None:
    """A connection that never opened is a reason to try the other instance.

    `httpx.ConnectError` propagates out of the transport as itself -- the transport owes
    `httpx.AsyncBaseTransport` its own exception types -- so the provider is the layer
    that has to catch it. A provider that only caught failing *responses* would let this
    escape as a raw `httpx` error into a service that must never import `httpx`.
    """
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(error=httpx.ConnectError("connection refused"))),
        fallback=ScriptedInstance(Reply(funded=ONE_COIN)),
    )
    provider, client = esplora_provider(fake, max_attempts=2)

    async with client:
        balances = await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert balances[0].confirmed == ONE_COIN
    assert fake.counts == {PRIMARY_HOST: 2, FALLBACK_HOST: 1}


async def test_a_transport_error_on_both_instances_becomes_a_typed_provider_error() -> None:
    """No `httpx` exception may reach a caller, and the message names no host.

    `ProviderUnavailableError` with the original attached through `raise ... from`, so the
    cause survives for a traceback while the typed error is what a service branches on.
    """
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(error=httpx.ConnectTimeout("timed out"))),
        fallback=ScriptedInstance(Reply(error=httpx.ConnectError("refused"))),
    )
    provider, client = esplora_provider(fake, max_attempts=1)

    async with client:
        with pytest.raises(ProviderUnavailableError) as caught:
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert not isinstance(caught.value, httpx.HTTPError)
    assert BIP173_TESTNET_P2WPKH not in str(caught.value)
    assert PRIMARY_HOST not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "why"),
    [
        pytest.param(403, "a ban spelled 403, which is what a public index actually sends"),
        pytest.param(429, "the documented throttle"),
        pytest.param(451, "a legal block, which is per-jurisdiction and so per-instance"),
        pytest.param(404, "a route that moved, or an API version retired"),
        pytest.param(401, "an auth proxy in front of one instance and not the other"),
        pytest.param(400, "a refusal of the request itself"),
        pytest.param(418, "a status nobody planned for"),
        pytest.param(301, "a redirect, which the shared client does not follow"),
        pytest.param(302, "a captive portal's redirect to a login page"),
        pytest.param(503, "the ordinary outage"),
    ],
)
async def test_any_refusal_at_all_moves_to_the_fallback(status: int, why: str) -> None:
    """The failover rule as `03ee9d1` restated it: **every** failure to answer moves on.

    This replaces `test_a_client_error_is_not_retried_against_the_fallback`, which
    asserted the opposite and was named after the old table. The old reasoning was that
    both instances run the same software against the same chain, so a 4xx is reproduced
    rather than repaired. That reasoning is wrong about the case the two-instance design
    exists for, and the 403 row is why.

    **A public index bans by returning 403, not 429.** mempool.space's documentation warns
    about a ban and says nothing about the status it arrives as; a ban, an auth proxy, a
    per-jurisdiction block and a retired API version are all per-*instance* facts, and
    every one of them presents as a non-429 refusal. Stopping the call on those leaves a
    healthy fallback unasked at the exact moment it is the only thing that would work --
    which is the single failure a second endpoint is for.

    Only a 200 whose body will not parse still stops, and that is a different claim: there
    the instance *answered*, and the answer is unusable in a way the other instance would
    reproduce.

    The counts are the assertion. A provider that still stopped would show
    `{primary: 1, fallback: 0}` and raise, and no assertion about the returned balance
    could tell the two apart.
    """
    del why  # In the parameter id, where a failure can read it.
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(status=status, body="nope")),
        fallback=ScriptedInstance(Reply(funded=ONE_COIN)),
    )
    provider, client = esplora_provider(fake, max_attempts=1)

    async with client:
        balances = await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert balances[0].confirmed == ONE_COIN
    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 1}
    assert fake.hosts_in_order == [PRIMARY_HOST, FALLBACK_HOST]


@pytest.mark.parametrize(
    ("status", "expected", "why"),
    [
        pytest.param(429, ProviderRateLimitedError, "a throttle names its own remedy"),
        pytest.param(403, ProviderResponseError, "a ban is not an outage and not a throttle"),
        pytest.param(401, ProviderResponseError, "a credential, which waiting will not fix"),
        pytest.param(404, ProviderResponseError, "a route that is gone"),
        pytest.param(301, ProviderResponseError, "a redirect nobody followed"),
        pytest.param(503, ProviderUnavailableError, "an outage, which waiting does fix"),
        pytest.param(500, ProviderUnavailableError, "the other outage"),
    ],
)
async def test_the_exhausted_error_is_classified_by_the_last_failure(
    status: int, expected: type[Exception], why: str
) -> None:
    """With every endpoint exhausted, the *last* failure decides the type. Three outcomes.

    The three have different remedies and that is the whole reason they are different
    classes: a 429 means our own interval is too short and the fix is configuration; a
    5xx or a transport error means wait; anything else means a person has to look, and
    retrying it produces the same answer.

    Both instances answer the same status here, so "the last failure" and "any failure"
    agree -- which is deliberate, because this test is about the *mapping*.
    `test_the_last_failure_decides_which_error_is_raised_not_the_worst_one` drives the
    case where they disagree.
    """
    del why  # In the parameter id.
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(status=status, body="nope")),
        fallback=ScriptedInstance(Reply(status=status, body="nope")),
    )
    provider, client = esplora_provider(fake, max_attempts=1)

    async with client:
        with pytest.raises(expected) as caught:
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert type(caught.value) is expected, (
        f"HTTP {status} raised {type(caught.value).__name__}; a subclass is not the same "
        "answer, because a caller branching on the remedy reads the exact type"
    )
    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 1}
    assert BIP173_TESTNET_P2WPKH not in str(caught.value)
    assert "nope" not in str(caught.value)


async def test_a_ban_on_the_primary_still_reads_the_fallback_for_every_address() -> None:
    """The scenario the rule change exists for, at the length a real sync has.

    A public index that has decided it has had enough of us returns 403 on every request.
    Under the old rule the first address raised and the remaining nineteen were never
    attempted, against a fallback that was working the whole time -- a portfolio reporting
    nothing, with a healthy endpoint sitting unused.

    Stickiness still applies, so the primary is asked exactly once and the fallback
    answers the rest. That pairing is the point: fail over on everything, but do not go
    back to an instance that just refused.
    """
    requested = (BIP173_TESTNET_P2WPKH, BIP350_TESTNET_V1, BIP173_TESTNET_P2WSH, CORE_SIGNET_P2PKH)
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(status=403, body="banned")),
        fallback=ScriptedInstance(Reply(funded=DUST)),
    )
    provider, client = esplora_provider(fake, max_attempts=1)

    async with client:
        balances = await provider.fetch_balances(requested)

    assert tuple(balance.address for balance in balances) == requested
    assert all(balance.confirmed == DUST for balance in balances)
    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 4}
    assert fake.addresses_asked_of(PRIMARY_HOST) == [BIP173_TESTNET_P2WPKH]


async def test_a_malformed_body_does_not_fall_over_either() -> None:
    """Same argument as the 4xx, and the same counting assertion.

    A body the parser cannot read is a change at the vendor or a bug in the parser. The
    fallback runs the same software and would produce the same unreadable answer; asking
    it is one more request against a public index for no information at all.
    """
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(body="{not json")),
        fallback=ScriptedInstance(Reply(funded=ONE_COIN)),
    )
    provider, client = esplora_provider(fake)

    async with client:
        with pytest.raises(ProviderResponseError):
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 0}


async def test_a_blank_fallback_url_means_one_instance_and_says_so_when_it_fails() -> None:
    """An empty `PORTFOLIO_BITCOIN_ESPLORA_FALLBACK_URL` is "one instance only".

    A self-hoster pointing at their own index and blanking the other should not have to
    learn a syntax, and the blank must not become a request to the empty string -- which
    `httpx` would refuse as a relative URL, from inside the provider, as something other
    than a `ProviderError`.
    """
    fake = EsploraFake(primary=ScriptedInstance(Reply(status=503)))
    provider, client = esplora_provider(fake, fallback_url="", max_attempts=2)

    async with client:
        with pytest.raises(ProviderUnavailableError):
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert fake.counts == {PRIMARY_HOST: 2, FALLBACK_HOST: 0}


async def test_two_identical_base_urls_are_one_instance_and_not_two() -> None:
    """A "fallback" pointing at the same host is not a fallback, and it doubles the cost.

    This is the self-hoster's configuration: one Esplora on the Pi, and both variables set
    to it because leaving the second blank looked like turning something off. The result
    under a naive implementation is two entries in the instance list, so a 429 from that
    host is followed immediately by a second request **to the same host** -- double the
    requests to an index that has just said stop, which is precisely the behaviour the
    per-host limiter and the whole failover design exist to avoid.

    `test_the_two_shipped_endpoints_are_different_instances` only inspects the shipped
    defaults, so nothing in the suite notices the configured case. This is that test.

    The count is the assertion, and it is the only thing that can be: the balances, the
    error type and the message are identical whether the host is asked once or twice.
    """
    fake = EsploraFake(primary=ScriptedInstance(Reply(status=429)))
    provider, client = esplora_provider(
        fake, primary_url=PRIMARY_URL, fallback_url=PRIMARY_URL, max_attempts=1
    )

    async with client:
        with pytest.raises(ProviderRateLimitedError):
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 0}


async def test_two_urls_that_differ_only_in_spelling_are_still_one_instance() -> None:
    """The same host written two ways is still one host, and a trailing slash is spelling.

    A de-duplication on exact string equality passes the test above and fails here, which
    is the realistic version: an operator copies the URL into the second variable and the
    editor, or the shell, adds a slash.
    """
    fake = EsploraFake(primary=ScriptedInstance(Reply(status=429)))
    provider, client = esplora_provider(
        fake, primary_url=PRIMARY_URL, fallback_url=f"{PRIMARY_URL}/", max_attempts=1
    )

    async with client:
        with pytest.raises(ProviderRateLimitedError):
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 0}


async def test_two_genuinely_different_urls_are_still_two_instances() -> None:
    """The control. De-duplication that collapsed everything would disable failover.

    Without this, "two identical URLs are one instance" could ship as "there is only ever
    one instance", which passes both tests above and silently removes the fallback from
    every deployment.
    """
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(status=429)),
        fallback=ScriptedInstance(Reply(funded=ONE_COIN)),
    )
    provider, client = esplora_provider(fake, max_attempts=1)

    async with client:
        balances = await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert balances[0].confirmed == ONE_COIN
    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 1}


async def test_a_blank_fallback_url_is_not_consulted_on_the_happy_path_either() -> None:
    """The control for the test above: one instance still reads a balance."""
    fake = EsploraFake(primary=ScriptedInstance(Reply(funded=DUST)))
    provider, client = esplora_provider(fake, fallback_url="")

    async with client:
        balances = await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert balances[0].confirmed == DUST
    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 0}


# --------------------------------------------------------------------------------------
# Criterion 5: the parser, driven directly through every refusal arm
# --------------------------------------------------------------------------------------
#
# Direct, because nine refusal arms reached only through a mock transport would be nine
# tests that also depend on the retry loop, the limiter and the URL builder -- and a break
# anywhere in that chain would be reported as a parsing bug.


#: A JSON integer of 5000 digits. CPython refuses to convert an integer string longer than
#: 4300 digits -- a denial-of-service mitigation added in 3.11 -- and `json.loads` does the
#: conversion, so this raises `ValueError` from *inside* the decoder rather than from any
#: check the parser performs. It is not a hypothetical: a compromised or confused upstream
#: returning a long digit run is exactly the input the CPython limit exists for.
HUGE_INTEGER: Final = "9" * 5000

#: 5000 nested arrays. `json.loads` recurses, so this raises `RecursionError`, which is a
#: `RuntimeError` and therefore outside anything `except (UnicodeDecodeError, JSONDecodeError)`
#: catches.
DEEPLY_NESTED: Final = "[" * 5000 + "]" * 5000


def body_with_sum(raw_sum: str) -> str:
    """An otherwise well-formed address body whose `funded_txo_sum` is `raw_sum` verbatim.

    Built as text rather than through `json.dumps`, because the inputs under test are ones
    `json` cannot round-trip: a 5000-digit integer is not something a Python `int` will
    survive being written back out as.
    """
    return (
        f'{{"address": "{BIP173_TESTNET_P2WPKH}", '
        f'"chain_stats": {{"funded_txo_sum": {raw_sum}, "spent_txo_sum": 0}}}}'
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(body_with_sum(HUGE_INTEGER), id="a sum of 5000 digits: ValueError"),
        pytest.param(DEEPLY_NESTED, id="5000 nested arrays: RecursionError"),
    ],
)
def test_a_body_that_breaks_the_decoder_itself_still_raises_the_typed_error(body: str) -> None:
    """Criterion 5 covers *every* malformed body, including the two `json` raises itself.

    `_decode` catches `UnicodeDecodeError` and `JSONDecodeError`, which is the set a
    reader expects `json.loads` to raise. It is not the set it actually raises. Measured
    against the shipped parser:

        funded_txo_sum of 5000 digits -> ValueError: Exceeds the limit (4300 digits)
        5000 nested arrays            -> RecursionError

    Both escape `parse_address_response` untyped. Criterion 5 says a malformed body raises
    a typed schema error rather than propagating a parse error, and for these two it does
    not -- so a service told to catch `ProviderError` meets a bare `ValueError` instead,
    from inside a package it is forbidden from importing the exceptions of.

    The `RecursionError` row is the worse of the two. It is a `RuntimeError`, so it is
    outside every `except Exception`-adjacent habit as well, and it arrives having already
    consumed most of the stack -- so whatever handles it runs with very little left.
    """
    with pytest.raises(ProviderResponseError) as caught:
        parse_address_response(body, BIP173_TESTNET_P2WPKH)

    assert BIP173_TESTNET_P2WPKH not in str(caught.value)
    # No fragment of the body either: a message quoting what it refused would carry the
    # address in the first row and 5000 characters of nothing in the second.
    assert HUGE_INTEGER[:40] not in str(caught.value)
    assert "[[[[" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(HUGE_INTEGER, id="an integer of 5000 digits"),
        pytest.param(DEEPLY_NESTED, id="5000 nested arrays"),
    ],
)
def test_a_tip_height_that_breaks_the_decoder_still_raises_the_typed_error(body: str) -> None:
    """The same two inputs at the health endpoint, which has the same hole.

    `parse_tip_height` decodes the same way, so it inherits both escapes -- and this one
    matters more, because `health()` promises it never raises and is what an operations
    view calls on a schedule.
    """
    with pytest.raises(ProviderResponseError):
        parse_tip_height(body)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(HUGE_INTEGER, id="an integer of 5000 digits"),
        pytest.param(DEEPLY_NESTED, id="5000 nested arrays"),
    ],
)
async def test_health_still_returns_rather_than_raising_on_a_body_that_breaks_the_decoder(
    body: str,
) -> None:
    """ "`health()` never raises" has to survive the inputs the decoder itself raises on.

    The promise is not a nicety: an operations view calls this to tell a broken vendor
    from a broken sync, and a health check that raises takes down the page that was
    supposed to explain the outage. Asserted as an unhealthy *answer*, with the endpoint
    position in the detail and nothing quoted from the body.
    """
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(body=body)),
        fallback=ScriptedInstance(Reply(body=body)),
    )
    provider, client = esplora_provider(fake, max_attempts=1)

    async with client:
        health = await provider.health()

    assert health.healthy is False
    assert health.detail is not None
    assert health.detail.startswith((PRIMARY, FALLBACK))
    assert body[:40] not in health.detail


async def test_a_body_that_breaks_the_decoder_does_not_escape_fetch_balances_either() -> None:
    """The same hole reached through the path a sync actually takes.

    The parser tests above drive the function directly. This drives it through the
    transport, so a `ValueError` that escaped would escape into whatever #10's scheduler
    does with an unhandled exception -- which is the failure the typed hierarchy exists to
    make impossible.
    """
    fake = EsploraFake(
        primary=ScriptedInstance(
            Reply(
                body=f'{{"address": "{BIP173_TESTNET_P2WPKH}", "chain_stats": '
                f'{{"funded_txo_sum": {HUGE_INTEGER}, "spent_txo_sum": 0}}}}'
            )
        )
    )
    provider, client = esplora_provider(fake, fallback_url="")

    async with client:
        with pytest.raises(ProviderResponseError):
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])


def test_the_parser_reads_the_documented_shape() -> None:
    """The control for everything below: the happy path produces the pair it should.

    A parser that raised on everything would satisfy every refusal test in this section,
    which is why this is first rather than implied.
    """
    body = balance_body(BIP173_TESTNET_P2WPKH, funded=ONE_COIN, spent=DUST, mempool_funded=7)

    stats = parse_address_response(body, BIP173_TESTNET_P2WPKH)

    assert stats == AddressStats(confirmed=ONE_COIN - DUST, pending=7)


def test_an_unexpected_extra_field_is_not_a_reason_to_refuse() -> None:
    """A vendor adding a field must not break every sync overnight.

    The parser refuses shapes it cannot *read*, not shapes it did not expect. Esplora
    already returns `funded_txo_count`, `tx_count` and more; a strict-by-default model
    would turn the next added field into an outage, which is the opposite of the failure
    mode this change chose.
    """
    body = json.loads(balance_body(BIP173_TESTNET_P2WPKH, funded=ONE_COIN))
    body["a_field_from_next_year"] = {"nested": [1, 2, 3]}
    body["chain_stats"]["another_new_one"] = "hello"

    stats = parse_address_response(json.dumps(body), BIP173_TESTNET_P2WPKH)

    assert stats.confirmed == ONE_COIN


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("<html><body>502 Bad Gateway</body></html>", id="an html error page"),
        pytest.param('{"address": "tb1', id="truncated mid-string"),
        pytest.param('{"chain_stats": {"funded_txo_sum": 1,', id="truncated mid-object"),
        pytest.param("null", id="a json null, which parses but is not an object"),
        pytest.param("[]", id="a json array"),
        pytest.param('"a string"', id="a json string"),
        pytest.param("12345", id="a bare number"),
    ],
)
def test_a_body_that_is_not_json_raises_the_typed_error(body: str) -> None:
    """Criterion 5: a typed schema error, never a `json.JSONDecodeError` propagating.

    A `JSONDecodeError` escaping the provider is an unhandled exception in whatever
    scheduler called it, and it carries `doc` -- the whole response body -- into any
    traceback that renders it. The truncated rows are the realistic ones: a connection cut
    mid-response produces exactly them, and only them, in production.
    """
    with pytest.raises(ProviderResponseError) as caught:
        parse_address_response(body, BIP173_TESTNET_P2WPKH)

    assert not isinstance(caught.value, json.JSONDecodeError)


@pytest.mark.parametrize(
    ("mutate", "id_"),
    [
        pytest.param(lambda body: body.pop("chain_stats"), "chain_stats missing"),
        pytest.param(lambda body: body.update(chain_stats=None), "chain_stats is null"),
        pytest.param(lambda body: body.update(chain_stats=[1, 2]), "chain_stats is an array"),
        pytest.param(lambda body: body.update(chain_stats="1"), "chain_stats is a string"),
        pytest.param(
            lambda body: body["chain_stats"].pop("funded_txo_sum"), "funded_txo_sum missing"
        ),
        pytest.param(
            lambda body: body["chain_stats"].pop("spent_txo_sum"), "spent_txo_sum missing"
        ),
        pytest.param(
            lambda body: body["chain_stats"].update(funded_txo_sum=None), "funded_txo_sum is null"
        ),
        pytest.param(
            lambda body: body["chain_stats"].update(funded_txo_sum="100"),
            "funded_txo_sum is a string of digits",
        ),
        pytest.param(
            lambda body: body["chain_stats"].update(spent_txo_sum=[]), "spent_txo_sum is an array"
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_a_response_missing_its_sums_raises_the_typed_error(mutate: object, id_: str) -> None:
    """Every missing-or-mistyped arm, including the string of digits.

    `"100"` is the one worth calling out. A parser that coerced with `int(value)` would
    accept it and be right by luck, until a vendor renders a balance as `"1.0e8"` -- at
    which point the coercion raises from inside the provider as a `ValueError`, or worse
    succeeds via a float. The rule is that a sum is an `int` in the JSON or it is a
    refusal.
    """
    del id_  # In the parameter id.
    body = json.loads(balance_body(BIP173_TESTNET_P2WPKH, funded=ONE_COIN, spent=0))
    mutate(body)  # type: ignore[operator]

    with pytest.raises(ProviderResponseError):
        parse_address_response(json.dumps(body), BIP173_TESTNET_P2WPKH)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1.0, id="a float that is a whole number"),
        pytest.param(100_000_000.0, id="a float rendered from a balance"),
        pytest.param(0.5, id="a fractional float"),
        pytest.param(True, id="a bool, which is an int subclass"),
        pytest.param(False, id="false, which would read as zero"),
    ],
)
def test_a_sum_that_is_not_a_whole_number_is_refused(value: object) -> None:
    """Criterion 5's float arm, and rule 2 arriving through the one door it can.

    `json.loads` is typed `Any`, so a vendor rendering `100000000.0` puts a `float` inside
    `providers/` -- where the AST ban in `tests/security/test_no_float.py` cannot see it,
    because it reads source and this float has no literal. Anything that sums or compares
    `confirmed` afterwards has done float arithmetic on money.

    The `bool` rows are not padding: `True` passes `isinstance(value, int)` and would be
    reported as a holding of one satoshi.
    """
    body = json.loads(balance_body(BIP173_TESTNET_P2WPKH, funded=ONE_COIN))
    body["chain_stats"]["funded_txo_sum"] = value

    with pytest.raises(ProviderResponseError):
        parse_address_response(json.dumps(body), BIP173_TESTNET_P2WPKH)


def test_a_negative_confirmed_balance_is_refused() -> None:
    """`spent_txo_sum > funded_txo_sum` cannot happen on a chain that is telling the truth.

    It is the shape a truncated, reordered or cached response produces, and the resulting
    negative balance is a number a caller would happily sum into a total. Refusing it here
    is cheaper than discovering it in a portfolio that says minus four bitcoin.
    """
    body = balance_body(BIP173_TESTNET_P2WPKH, funded=DUST, spent=DUST + 1)

    with pytest.raises(ProviderResponseError):
        parse_address_response(body, BIP173_TESTNET_P2WPKH)


def test_a_zero_confirmed_balance_is_not_refused_alongside_the_negative() -> None:
    """The boundary. `spent == funded` is an address that spent everything it received."""
    body = balance_body(BIP173_TESTNET_P2WPKH, funded=ONE_COIN, spent=ONE_COIN)

    assert parse_address_response(body, BIP173_TESTNET_P2WPKH).confirmed == 0


def test_a_negative_pending_is_not_refused_because_it_is_a_delta() -> None:
    """The asymmetry, asserted at the parser as well as at `align_balances`.

    `mempool_stats` spending more than it funds is an outgoing payment, which is normal
    and reads negative. A parser that copied the confirmed guard onto the mempool figures
    would refuse every wallet that has ever sent anything.
    """
    body = balance_body(BIP173_TESTNET_P2WPKH, funded=ONE_COIN, mempool_funded=0, mempool_spent=99)

    assert parse_address_response(body, BIP173_TESTNET_P2WPKH).pending == -99


@pytest.mark.parametrize(
    ("echoed", "id_"),
    [
        pytest.param(None, "the address field is absent"),
        pytest.param(BIP350_TESTNET_V1, "a different address entirely"),
        pytest.param(BIP173_TESTNET_P2WPKH.upper(), "the same address in another case"),
        pytest.param(f"{BIP173_TESTNET_P2WPKH} ", "the same address with trailing space"),
        pytest.param("", "an empty address field"),
        pytest.param(12345, "an address field that is not a string"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_an_answer_echoing_a_different_address_is_refused(echoed: object, id_: str) -> None:
    """A cache or a proxy answering about somebody else, which a single-address API can do.

    `align_balances` already refuses this for a batch; a one-address-per-call API produces
    the same correlation failure just as easily, and there is no batch for the shared rule
    to catch it in. The uppercase row is the interesting one: bech32 is case insensitive
    as an *address*, but the provider asked with the canonical form and an instance
    answering in another spelling is an instance that did not echo what it was given.
    """
    del id_  # In the parameter id.
    body = json.loads(balance_body(BIP173_TESTNET_P2WPKH, funded=ONE_COIN))
    if echoed is None:
        body.pop("address")
    else:
        body["address"] = echoed

    with pytest.raises(ProviderResponseError):
        parse_address_response(json.dumps(body), BIP173_TESTNET_P2WPKH)


def test_no_parser_rejection_names_the_address() -> None:
    """Every rejection arm, one address, and the assertion the spec calls load-bearing.

    **A pydantic `ValidationError` renders the input that failed**, and the input here is
    a response body containing the owner's address. That exception would travel into a log
    through `logger.exception` -- which is #44 all over again, the same defect #5 found in
    `services/wallets.py`, arriving by a different door. It is why this parser is written
    by hand.

    Both `str(exc)` and `exc.args` are checked, because a message built carefully and an
    argument tuple built carelessly are two different mistakes and only one of them is
    visible in a rendered message. The twenty-character prefix is checked too: that much
    of a bech32 address is unique on chain and is enough to search an explorer with.
    """
    address = BIP173_TESTNET_P2WPKH
    good = json.loads(balance_body(address, funded=ONE_COIN, spent=0, mempool_funded=0))

    bodies: list[str] = [
        # Not JSON at all -- the body is echoed by every naive decoder error.
        f'{{"address": "{address}", "chain_st',
        f"<html>could not read {address}</html>",
        # Not an object.
        json.dumps([good]),
        json.dumps(address),
        # chain_stats missing or the wrong type.
        json.dumps({"address": address}),
        json.dumps({"address": address, "chain_stats": address}),
        # A sum missing, mistyped, a float, a bool.
        json.dumps({"address": address, "chain_stats": {"spent_txo_sum": 0}}),
        json.dumps({"address": address, "chain_stats": {"funded_txo_sum": 1}}),
        json.dumps(
            {"address": address, "chain_stats": {"funded_txo_sum": 1.5, "spent_txo_sum": 0}}
        ),
        json.dumps(
            {"address": address, "chain_stats": {"funded_txo_sum": True, "spent_txo_sum": 0}}
        ),
        # spent exceeding funded.
        balance_body(address, funded=1, spent=2),
        # mempool_stats present and unreadable.
        json.dumps({**good, "mempool_stats": address}),
        json.dumps({**good, "mempool_stats": {"funded_txo_sum": "1", "spent_txo_sum": 0}}),
        # The address field missing, and echoing somebody else's.
        json.dumps({key: value for key, value in good.items() if key != "address"}),
        json.dumps({**good, "address": BIP350_TESTNET_V1}),
    ]

    refusals: list[ProviderResponseError] = []
    for body in bodies:
        with pytest.raises(ProviderResponseError) as caught:
            parse_address_response(body, address)
        refusals.append(caught.value)

    # Every arm was actually reached. Without this the loop is satisfied by a parser that
    # refuses on the first branch and never sees the rest.
    assert len(refusals) == len(bodies)

    for body, error in zip(bodies, refusals, strict=True):
        rendered = str(error)
        arguments = " ".join(str(argument) for argument in error.args)
        for form in (address, address.lower(), address.upper(), address[:20], address[-20:]):
            assert form not in rendered, f"the message quoted the address, for body {body[:60]!r}"
            assert form not in arguments, f"an argument quoted the address: {arguments[:120]!r}"
        # The other address too. A message that echoed the offending *value* rather than
        # naming the field would carry whichever address the body happened to hold, and
        # the echoed-address row holds a different one from the one asked about.
        assert BIP350_TESTNET_V1 not in rendered
        assert BIP350_TESTNET_V1 not in arguments
        # Naming the field is allowed and is the whole point of a useful message; quoting
        # the body is not. The body is JSON, so a message that contained it would contain
        # its punctuation -- which nothing legitimate here has any reason to.
        assert '"' not in rendered, f"the message looks like it quotes JSON: {rendered!r}"
        assert "{" not in rendered, rendered
        assert "}" not in rendered, rendered


def test_the_address_absence_assertion_would_notice_a_leak() -> None:
    """The control: the check above can fail, so a green run means it looked.

    Without this, `test_no_parser_rejection_names_the_address` proves only that some
    exception was raised -- and an assertion that never fails is the sixth costume of the
    failure this project keeps finding.
    """
    address = BIP173_TESTNET_P2WPKH
    leaked = ProviderResponseError(f"could not parse the body for {address}")

    assert address in str(leaked)
    assert any(address in str(argument) for argument in leaked.args)


# --------------------------------------------------------------------------------------
# Criterion 5: status first, body second
# --------------------------------------------------------------------------------------


async def test_an_html_error_page_behind_a_5xx_is_unavailable_not_malformed() -> None:
    """Order matters, and getting it backwards files an outage under "needs a human".

    A 502 carrying an HTML error page is an unavailable upstream. Deciding that from the
    *body* makes it a schema error -- not retryable, by design -- so every reverse proxy
    hiccup would be reported as a parser bug forever, and the sync would stop trying.
    """
    page = "<html><head><title>502 Bad Gateway</title></head><body>nginx</body></html>"
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(status=502, body=page)),
        fallback=ScriptedInstance(Reply(status=502, body=page)),
    )
    provider, client = esplora_provider(fake, max_attempts=1)

    async with client:
        with pytest.raises(ProviderUnavailableError) as caught:
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert not isinstance(caught.value, ProviderResponseError)
    assert "nginx" not in str(caught.value)
    assert "<html>" not in str(caught.value)


async def test_a_200_carrying_an_html_page_is_a_schema_error_and_not_unavailability() -> None:
    """The other side of the same rule. A captive portal answers 200 with a login page.

    Status first means a 200 is trusted as an answer and the *body* then decides, so this
    is a `ProviderResponseError` -- something has changed at the vendor and a human has to
    look. Retrying it would produce the same page three times.
    """
    fake = EsploraFake(primary=ScriptedInstance(Reply(body="<html>sign in</html>")))
    provider, client = esplora_provider(fake)

    async with client:
        with pytest.raises(ProviderResponseError) as caught:
            await provider.fetch_balances([BIP173_TESTNET_P2WPKH])

    assert not isinstance(caught.value, ProviderUnavailableError)


# --------------------------------------------------------------------------------------
# Health: the tip height, what it may say, and that it never raises
# --------------------------------------------------------------------------------------


async def test_health_reports_healthy_when_an_instance_answers() -> None:
    """`GET /blocks/tip/height`: documented, cheap, and it names no address.

    The endpoint is asserted from the request the fake received rather than assumed, and
    the label is the allowlisted `block_tip_height` -- so a health check that borrowed the
    balance label, or none at all, fails here rather than logging under the wrong name.
    """
    fake = EsploraFake(primary=ScriptedInstance(Reply(tip=TIP_HEIGHT)))
    provider, client = esplora_provider(fake)

    async with client:
        health = await provider.health()

    assert health.healthy is True
    assert health.chain_key is ChainKey.BITCOIN
    assert health.detail == PRIMARY
    request = fake.primary.requests[0]
    assert request.url.path == f"/api{TIP_HEIGHT_PATH}"
    assert request.extensions.get(ENDPOINT_EXTENSION) == BLOCK_TIP_HEIGHT
    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 0}


async def test_health_tries_the_fallback_when_the_primary_is_down() -> None:
    """Healthy if *either* answers, and the detail says which -- position, never a URL."""
    fake = EsploraFake(
        primary=ScriptedInstance(Reply(status=503)),
        fallback=ScriptedInstance(Reply(tip=TIP_HEIGHT)),
    )
    provider, client = esplora_provider(fake, max_attempts=1)

    async with client:
        health = await provider.health()

    assert health.healthy is True
    assert health.detail == FALLBACK
    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 1}


@pytest.mark.parametrize(
    ("primary", "fallback", "id_"),
    [
        pytest.param(Reply(status=503), Reply(status=503), "both refuse"),
        pytest.param(
            Reply(error=httpx.ConnectError("refused")),
            Reply(error=httpx.ConnectTimeout("timed out")),
            "neither connects",
        ),
        pytest.param(
            Reply(body="<html>holding page</html>"),
            Reply(body="<html>holding page</html>"),
            "both answer 200 with a holding page",
        ),
        pytest.param(Reply(body="-1"), Reply(body="1.5"), "a tip height that is not a height"),
        pytest.param(Reply(body=""), Reply(body="   "), "an empty tip height"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
async def test_health_reports_unhealthy_without_raising_and_without_an_address(
    primary: Reply, fallback: Reply, id_: str
) -> None:
    """`health()` never raises, and its detail carries a position and a reason only.

    The holding-page and bad-height rows are why the tip height has to *parse*: an
    instance returning HTML with a 200 is unhealthy, not healthy-and-wrong, and a health
    check that only looked at the status would report a captive portal as a working chain.

    The detail is asserted as short, as starting with a position, and as carrying no URL,
    no host and no address -- it is rendered in an operations view and it reaches a log,
    which are both places the owner's holdings must not be.
    """
    del id_  # In the parameter id.
    fake = EsploraFake(ScriptedInstance(primary), ScriptedInstance(fallback))
    provider, client = esplora_provider(fake, max_attempts=1)

    async with client:
        health = await provider.health()

    assert health.healthy is False
    assert health.detail is not None
    assert health.detail.startswith(PRIMARY) or health.detail.startswith(FALLBACK)
    assert len(health.detail) <= 120
    for forbidden in (PRIMARY_HOST, FALLBACK_HOST, PRIMARY_URL, FALLBACK_URL, "html", "://"):
        assert forbidden not in health.detail


async def test_health_says_so_when_no_endpoint_is_configured() -> None:
    """Both URLs blank is a misconfiguration, and it is not an exception.

    `health()` is what an operator reads to tell a broken vendor from a broken sync, so
    the one case where there is nothing to ask has to come back as an unhealthy answer
    rather than as a traceback in whatever rendered the page.
    """
    fake = EsploraFake()
    provider, client = esplora_provider(fake, primary_url="", fallback_url="")

    async with client:
        health = await provider.health()

    assert health.healthy is False
    assert health.detail == "no endpoint configured"
    assert fake.requests == []


async def test_health_reports_no_address_because_it_never_reads_one() -> None:
    """A failing health check must not itself disclose what is being watched.

    Separate from `fetch_balances` precisely so an operations view can say "the chain is
    down" without naming a wallet. Asserted as the request log: no address-shaped path was
    ever requested.
    """
    fake = EsploraFake(primary=ScriptedInstance(Reply(status=500)))
    provider, client = esplora_provider(fake, fallback_url="", max_attempts=1)

    async with client:
        health = await provider.health()

    assert health.healthy is False
    assert all(request.url.path.endswith(TIP_HEIGHT_PATH) for request in fake.requests)
    assert all("/address/" not in str(request.url) for request in fake.requests)


# --------------------------------------------------------------------------------------
# The tip-height parser, on its own
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("0", id="the genesis block"),
        pytest.param("2873119", id="a plausible testnet height"),
        pytest.param("2873119\n", id="a trailing newline, which curl-style servers send"),
        pytest.param(" 2873119 ", id="surrounding whitespace"),
    ],
)
def test_a_plain_integer_body_is_a_tip_height(body: str) -> None:
    """The happy path, including the whitespace an HTTP server adds without asking."""
    assert parse_tip_height(body) >= 0


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace only"),
        pytest.param("-1", id="a negative height"),
        pytest.param("1.5", id="a decimal point"),
        pytest.param("1e6", id="scientific notation"),
        pytest.param("0x10", id="hexadecimal"),
        pytest.param("<html>503</html>", id="an html holding page"),
        pytest.param('{"height": 1}', id="a json object"),
        pytest.param("٣٤٥", id="arabic-indic digits, which str.isdigit accepts"),
        pytest.param("²", id="a superscript two, which str.isdigit also accepts"),
        pytest.param("+5", id="an explicit plus, which int() accepts and a height is not"),
    ],
)
def test_a_tip_height_that_is_not_a_plain_non_negative_integer_is_refused(body: str) -> None:
    """The non-ASCII digit rows are the ones a hand-rolled check gets wrong.

    `"٣٤٥".isdigit()` is `True` and `int("٣٤٥")` is `345`, so a parser built on either
    accepts a body no Esplora instance ever sends -- which means it would also accept
    whatever else arrives from a compromised or misconfigured middlebox. A height is ASCII
    digits and nothing else.
    """
    with pytest.raises(ProviderResponseError):
        parse_tip_height(body)


def test_no_tip_height_rejection_quotes_the_body() -> None:
    """The same #44 rule as the balance parser, at the endpoint that names no address.

    It still matters: a holding page from a misconfigured reverse proxy can carry an
    internal hostname, and rule 3 keeps those out of the repository for the same reason it
    keeps them out of a log.
    """
    page = "<html>internal-service-name unreachable</html>"

    with pytest.raises(ProviderResponseError) as caught:
        parse_tip_height(page)

    assert "internal-service-name" not in str(caught.value)
    assert all("internal-service-name" not in str(argument) for argument in caught.value.args)


# --------------------------------------------------------------------------------------
# The shipped settings, observed rather than injected
# --------------------------------------------------------------------------------------
#
# Every test above builds its own `Settings`, which is the discipline that keeps them
# deterministic and is exactly what leaves the shipped defaults unobserved -- the #6
# lesson, where `DEFAULT_RETRY_POLICY.max_attempts` could have shipped as 1 with 1102
# tests green. So these tests pass no arguments at all.


def test_the_shipped_default_endpoints_are_the_two_public_instances() -> None:
    """Pinned as literals, because these are the URLs a fresh deployment actually calls.

    Not derived from the field defaults -- `Settings().bitcoin_esplora_url ==
    Settings().bitcoin_esplora_url` is true of any value at all, including an empty
    string, which would silently make a fresh deployment "one instance only" with no
    instance.
    """
    settings = Settings()

    assert settings.bitcoin_esplora_url == "https://mempool.space/api"
    assert settings.bitcoin_esplora_fallback_url == "https://blockstream.info/api"
    assert settings.bitcoin_network == "mainnet"


def test_the_two_shipped_endpoints_are_different_instances() -> None:
    """A fallback equal to the primary is a fallback that fails for the same reason.

    The whole point of a second endpoint is that it is operated by somebody else; two
    URLs that resolve to one vendor would pass every failover test in this file -- they
    are scripted against fictional hosts -- and buy nothing in production.
    """
    settings = Settings()

    assert settings.bitcoin_esplora_url != settings.bitcoin_esplora_fallback_url
    assert settings.bitcoin_esplora_url.startswith("https://")
    assert settings.bitcoin_esplora_fallback_url.startswith("https://")


async def test_a_provider_built_with_no_settings_uses_the_shipped_ones() -> None:
    """`EsploraProvider(client)` -- the registry's own construction -- reads `get_settings()`.

    Every other test in this file hands over a `Settings` it built, which is the
    discipline that keeps them deterministic and is exactly what leaves the `settings is
    None` fallback observed only as a *covered line*. Coverage says the branch ran; it
    says nothing about whether the object it produced is the one production gets. A
    provider that quietly constructed its own defaults would be fully covered and wrong.

    Both halves are observed from outside, and neither needs a mainnet address.

    The **network** is observed through a refusal: the shipped default is `mainnet`, so a
    provider built this way must refuse the testnet vector with `WRONG_NETWORK`. If the
    fallback ever produced a testnet-configured provider, this address would be accepted.

    The **URL** is observed through `health()`, which reads `/blocks/tip/height` and names
    no address at all. The host is compared against the setting rather than against a
    literal on purpose: the literal lives in
    `test_the_shipped_default_endpoints_are_the_two_public_instances`, and the pair says
    the number is both intended and applied -- the same shape as the four timeouts in
    `tests/providers/test_http.py`.
    """
    shipped = Settings()
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, content=str(TIP_HEIGHT))

    client = build_http_client(
        transport=httpx.MockTransport(handler),
        policy=RetryPolicy(max_attempts=1, base_backoff_ms=0, max_backoff_ms=0),
        limiter=HostRateLimiter(min_interval_ms=0, clock=lambda: 0, sleep=_no_sleep),
        sleep=_no_sleep,
    )
    provider = EsploraProvider(client)

    with pytest.raises(AddressInvalidError) as caught:
        provider.validate_address(BIP173_TESTNET_P2WPKH)

    assert caught.value.reason is AddressRejection.WRONG_NETWORK
    assert shipped.bitcoin_network == "mainnet"

    async with client:
        health = await provider.health()

    assert health.healthy is True
    assert recorded[0].url.host == httpx.URL(shipped.bitcoin_esplora_url).host
    assert recorded[0].url.path == f"{httpx.URL(shipped.bitcoin_esplora_url).path}{TIP_HEIGHT_PATH}"


@pytest.mark.parametrize("network", ["mainnet", "testnet", "regtest"])
def test_every_network_name_is_accepted_by_the_setting(network: str) -> None:
    """The three the enum defines, and nothing else, so a typo fails at startup.

    A free-form string would let `PORTFOLIO_BITCOIN_NETWORK=testnet3` build a `Settings`
    and then compare unequal to every member, which presents as a provider refusing every
    address with `WRONG_NETWORK` and no clue as to why.
    """
    assert esplora_settings(network=network).bitcoin_network == network


def test_a_network_that_does_not_exist_is_refused_at_construction() -> None:
    """The control for the row above."""
    with pytest.raises(ValueError, match=r"bitcoin_network"):
        Settings(bitcoin_network="testnet3")


_CONFORMS: ChainProvider = EsploraProvider(httpx.AsyncClient(), settings=esplora_settings())
"""Criterion 7 of #6, now applied to a real provider rather than to a fake.

`mypy --strict` deciding assignability is the assertion; there is no `isinstance` here and
there must not be one. `ChainProvider` is deliberately not `@runtime_checkable`, so an
`isinstance` check would compare four attribute names and say nothing about whether
`fetch_balances` takes a sequence or whether `health` is a coroutine function.

The client is never used -- nothing is awaited on this instance -- so no connection pool
is opened by importing this module.
"""
