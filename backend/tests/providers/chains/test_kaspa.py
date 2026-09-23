"""The Kaspa REST balance provider: every acceptance criterion of #8 that has a behaviour.

This is the first provider that **batches**, the first whose read is a `POST`, and the
first to meet a vendor that documents its failures. Four things in this file carry more
weight than the rest.

**The duplicate-entry test asserts a refusal, not a resolution.** The batch response is an
array, so two entries for one address are possible. A parser that keeps the first and one
that keeps the last are both wrong and both plausible, and a test asserting either one pins
a coin flip. It is refused at the point where both values still exist, because
`align_balances` receives a `dict` and a `dict` keeps the last value silently -- no
assertion downstream could ever see the other one.

**The `pending` tests assert `None` and never zero.** The issue's own text asks for zero;
#7 settled the question the other way and this change follows #7. A Kaspa balance of zero
pending and a Bitcoin balance of zero pending would render identically while meaning
different things -- one says "nothing is in the mempool", the other says "nobody asked the
mempool" -- and a dashboard cannot honour a distinction it was never given.

**The failover tests count requests per host.** A provider that hammered a throttled
primary and then succeeded on the fallback returns exactly the same balances as one that
moved on after the first refusal; only `fake.counts` and `fake.hosts_in_order` tell them
apart, and the difference between them is the difference between a slow sync and an
application banned from a free public index.

**Every exhaustion test asserts `__cause__`, not only the exception type.** That is #7's
fourth closing lesson: thirty-seven planned tests, all of them asserting a type, and a
`ProviderRateLimitedError` chained to the *other* endpoint's `ConnectError` survived every
one of them -- while sending whoever read the traceback after the wrong host.

## What this file cannot do, stated rather than worked around

`tests/address_vectors.py` holds **four** published `kaspatest:` vectors, and the vendor's
own OpenAPI document uses real mainnet addresses as its example values, which rule 3
forbids this repository from holding at all. So a 65-address request cannot be assembled
out of real addresses. The shipped `max_addresses_per_call` is therefore pinned two ways
that need no addresses -- as a literal on `CAPABILITIES`, and through `chunk_addresses`
over synthetic strings -- and the multi-batch path is driven with the call size
monkeypatched down to the vectors available. Both halves are needed: the patched one proves
the provider splits, the pinned one proves it splits at the number production ships.

Every address is `kaspatest:`. Rule 3, proved over this file by
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
from portfolio.providers.base import ChainCapabilities, chunk_addresses
from portfolio.providers.chains import kaspa as kaspa_module
from portfolio.providers.chains.kaspa import (
    ADDRESS_BALANCE_PATH,
    BALANCES_PATH,
    CAPABILITIES,
    HEALTH_PATH,
    KASPA_DECIMALS,
    MAX_ADDRESSES_PER_CALL,
    KaspaProvider,
    parse_address_balance,
    parse_balances,
    parse_health,
)
from portfolio.providers.endpoints import FALLBACK, PRIMARY
from portfolio.providers.errors import (
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from portfolio.providers.http import (
    ADDRESS_BALANCE,
    ADDRESS_BALANCES,
    ENDPOINT_EXTENSION,
    IDEMPOTENT_EXTENSION,
    NODE_HEALTH,
    HostRateLimiter,
    RetryPolicy,
    build_http_client,
)
from portfolio.providers.registry import CHAIN_PROVIDERS
from tests.address_vectors import (
    BECH32_CHARSET,
    BIP173_TESTNET_P2WPKH,
    KASPA_NAMED_CORRUPTIONS,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V0_ASPECTRON,
    KASPA_TESTNET_V1_KEY,
    KASPA_TESTNET_V1_ZERO,
    KASPA_UNKNOWN_PREFIX,
    KASPA_VECTORS,
    SYNTHETIC_TPUB,
    Vector,
    corruptions_of,
)
from tests.providers.chains.kaspa_harness import (
    BLUE_SCORE,
    DUST,
    FALLBACK_HOST,
    FALLBACK_URL,
    KASPAD_HOST,
    ONE_COIN,
    PRIMARY_HOST,
    PRIMARY_URL,
    KaspaFake,
    Reply,
    ScriptedInstance,
    balance_body,
    batch_body,
    health_body,
    kaspa_client,
    kaspa_provider,
    kaspa_settings,
    posted_addresses,
)
from tests.providers.harness import RecordingSleep

if TYPE_CHECKING:
    from collections.abc import Sequence

    from portfolio.providers.base import AddressBalance, ChainProvider

#: The four published vectors, in a fixed order, so "in order" means something.
ALL_FOUR: Final[tuple[str, ...]] = (
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V1_ZERO,
    KASPA_TESTNET_V1_KEY,
    KASPA_TESTNET_V0_ASPECTRON,
)

#: Three of them, for the tests that want more than one batch at a call size of two.
THREE: Final[tuple[str, ...]] = ALL_FOUR[:3]

#: Two of them, which is the smallest request that takes the batch endpoint.
TWO: Final[tuple[str, ...]] = ALL_FOUR[:2]


def balances_by_address(balances: Sequence[AddressBalance]) -> dict[str, AddressBalance]:
    return {balance.address: balance for balance in balances}


def with_call_size(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    """Shrink the declared batch size, **before the provider is built**.

    The honest alternative would be sixty-five real `kaspatest:` addresses, and there are
    four. Patching the declaration rather than the splitting code means the provider's own
    `chunk_addresses` call is what is being exercised, and calling this before construction
    works whether the provider reads the module global per call or copies it onto the
    instance in `__init__`.

    The shipped number is pinned separately, twice, because a test that injects a value can
    no longer observe the value production uses -- #6's lesson, which cost a retry
    subsystem that could have shipped dead.
    """
    monkeypatch.setattr(
        kaspa_module,
        "CAPABILITIES",
        ChainCapabilities(
            chain_key=ChainKey.KASPA,
            decimals=KASPA_DECIMALS,
            max_addresses_per_call=size,
        ),
    )


async def _no_sleep(_milliseconds: int) -> None:
    """The injected sleep for the two tests that build their own client."""
    return


# --------------------------------------------------------------------------------------
# Wiring: registered, declares what it can do, and satisfies the protocol
# --------------------------------------------------------------------------------------


def test_the_provider_is_what_the_registry_builds_for_the_kaspa_key() -> None:
    """The registry's answer for `kaspa` is this class, built from the shared client.

    Asserted through `CHAIN_PROVIDERS.create` rather than by reading the dictionary, so the
    factory signature -- one positional client and nothing else -- is exercised. That is
    the path #10 will use, and it is also the construction in which `settings` defaults to
    `get_settings()`.
    """
    import portfolio.providers.chains  # noqa: F401 - imported for its registration effect

    fake = KaspaFake()
    client = kaspa_client(fake)

    provider = CHAIN_PROVIDERS.create(ChainKey.KASPA, client)

    assert isinstance(provider, KaspaProvider)
    assert provider.capabilities.chain_key is ChainKey.KASPA


def test_the_provider_declares_that_it_can_batch_and_says_how_widely() -> None:
    """`max_addresses_per_call` is the integer a caller sizes its work from.

    Pinned as the integer rather than as `can_batch`, because the boolean is derivable from
    the integer and the integer is not derivable from the boolean -- a caller that only
    knew "can batch: true" would still have to guess how many addresses fit.

    **64 is a guess and the spec says so.** The OpenAPI document declares `addresses` as an
    array of strings with no `maxItems` and the operation description names no ceiling,
    confirmed against the live document on 2026-09-22. The first real evidence will be a
    refused batch in production, which is why the refusal names the size -- see
    `test_a_refused_batch_names_its_size_and_no_address`.
    """
    fake = KaspaFake()
    provider, _client = kaspa_provider(fake)

    capabilities = provider.capabilities

    assert capabilities.chain_key is ChainKey.KASPA
    assert capabilities.decimals == KASPA_DECIMALS == 8
    assert capabilities.max_addresses_per_call == MAX_ADDRESSES_PER_CALL == 64
    assert capabilities.can_batch is True


def test_the_shipped_call_size_is_what_chunk_addresses_actually_splits_at() -> None:
    """The declared 64 observed as the split it produces, with no addresses involved.

    `chunk_addresses` is pure and takes any strings, so the shipped ceiling is verifiable
    at its real value even though this repository may hold only four Kaspa addresses. The
    literal above says the number is intended; this says it is applied. A declaration
    nothing consumes is decoration, and it drifts out of date without anything noticing.
    """
    synthetic = [f"address-{index}" for index in range(130)]

    chunks = chunk_addresses(synthetic, CAPABILITIES)

    assert [len(chunk) for chunk in chunks] == [64, 64, 2]
    assert [address for chunk in chunks for address in chunk] == synthetic


def test_a_call_size_that_could_never_advance_is_refused_at_declaration() -> None:
    """The batching equivalent of a cursor that stops advancing.

    `chunk_addresses` steps by `max_addresses_per_call`, so a declaration of zero produces
    a loop that never terminates -- a sync that hangs rather than fails, which on a
    Raspberry Pi is a container that has to be noticed by a human. It is refused where the
    wrong number is, at construction, rather than where it would be felt.
    """
    with pytest.raises(ValueError, match=r"max_addresses_per_call"):
        ChainCapabilities(
            chain_key=ChainKey.KASPA, decimals=KASPA_DECIMALS, max_addresses_per_call=0
        )


# --------------------------------------------------------------------------------------
# Criterion 6: validation is offline, delegates to the domain, and precedes every URL
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("vector", [pytest.param(vector, id=vector.id) for vector in KASPA_VECTORS])
def test_validate_address_accepts_every_published_vector(vector: Vector) -> None:
    """All four vectors, from two unrelated publishers, through the provider's own method.

    The provider delegates to `domain.chains.validate_address` rather than reimplementing a
    codec -- two copies of a checksum rule is how they drift -- so this asserts the
    delegation produces the domain's own canonical and display forms, not merely that it
    did not raise.

    Criterion 6's fallback clause is satisfied **by not being reached**: #5 implemented the
    full 40-bit CashAddr checksum over the network prefix, verified against vectors from
    two unrelated publishers with an exhaustive single-character corruption sweep, so no
    degradation to prefix-and-length validation is needed and none is done.
    """
    fake = KaspaFake()
    provider, _client = kaspa_provider(fake, network="testnet")

    validated = provider.validate_address(vector.address)

    assert validated.canonical == vector.canonical
    assert validated.display == vector.display
    assert fake.requests == []


@pytest.mark.parametrize(
    ("name", "corrupted"),
    [pytest.param(name, corrupted, id=name) for name, _valid, corrupted in KASPA_NAMED_CORRUPTIONS],
)
def test_a_one_character_corruption_is_refused(name: str, corrupted: str) -> None:
    """A validator that checked the prefix, the length and the alphabet would accept these.

    **Measured on 2026-09-23, and it is the vendor that does exactly that.** Sending one of
    our own published `kaspatest:` vectors to the live service returns 422 with the
    server's own rule quoted: the path must match `^kaspa:[a-z0-9]{61,63}$`. Prefix,
    charset and length -- and *not* the checksum. A mistyped mainnet address that still
    matches that regex is accepted and answered with a balance, which for a wallet that
    does not exist is `0`: a typo that reports an empty wallet forever and looks no
    different from an empty one. Our offline validation is strictly stronger than the
    vendor's, and that is now a measurement rather than a preference.
    """
    del name  # In the parameter id, where a failure can read it.
    fake = KaspaFake()
    provider, _client = kaspa_provider(fake, network="testnet")

    with pytest.raises(AddressInvalidError):
        provider.validate_address(corrupted)

    assert fake.requests == []


def test_every_single_character_substitution_of_one_vector_is_refused() -> None:
    """The exhaustive sweep, not the hand-picked one.

    The 40-bit BCH checksum detects any run of four or fewer errors, so *every*
    single-character substitution must be refused, not merely the one somebody wrote down.
    A validator that passed the named corruption above and failed here would be one that
    checks a shape -- which is precisely what the vendor's own regex does.

    Driven over the payload's own charset, because substituting a character from *outside*
    the alphabet is a weaker test: it can be rejected on the alphabet alone without a
    checksum ever being computed.
    """
    fake = KaspaFake()
    provider, _client = kaspa_provider(fake)
    payload = KASPA_TESTNET_V0.split(":", 1)[1]
    survivors: list[tuple[int, str]] = []

    for position, corrupted_payload in corruptions_of(payload, BECH32_CHARSET):
        corrupted = f"kaspatest:{corrupted_payload}"
        try:
            provider.validate_address(corrupted)
        except AddressInvalidError:
            continue
        survivors.append((position, corrupted))

    assert survivors == []


@pytest.mark.parametrize(
    ("network", "why"),
    [
        pytest.param("mainnet", "the shipped default, which is the realistic mistake"),
        pytest.param("devnet", "the third network, so the refusal is not a mainnet special case"),
    ],
)
def test_an_address_from_another_network_is_refused_without_a_request(
    network: str, why: str
) -> None:
    """One Kaspa REST instance serves exactly one network, and the vendor proves it.

    **Measured on 2026-09-23:** the vendor's path validation is mainnet-only by
    construction -- the prefix is a literal in its regex -- which confirms that a
    configured-network refusal describes something real rather than something imagined.

    The refusal is offline and the assertion is in two parts. The reason must be
    `WRONG_NETWORK`, and **the mock transport must have recorded zero requests** -- which
    is the half that says the address was never interpolated into a URL. A balance read
    from the wrong chain is a *number*, not an error, and nothing downstream can tell it
    from a right one.

    Unlike Bitcoin, there is no residual here. `kaspa`, `kaspatest` and `kaspadev` are
    three distinct prefixes, each folded into the 40-bit checksum, so the same payload
    checksums differently on each. The check is exact here and approximate there.
    """
    del why  # In the parameter id.
    fake = KaspaFake()
    provider, _client = kaspa_provider(fake, network=network)

    with pytest.raises(AddressInvalidError) as caught:
        provider.validate_address(KASPA_TESTNET_V0)

    assert caught.value.reason is AddressRejection.WRONG_NETWORK
    assert fake.counts == {PRIMARY_HOST: 0, FALLBACK_HOST: 0}
    assert fake.requests == []


def test_an_address_on_the_configured_network_is_accepted() -> None:
    """The control. A provider that refused everything would pass the test above.

    Without this, `WRONG_NETWORK` for every address at all would satisfy the refusal
    assertions and make the provider useless in a way no other test in this file would
    notice, because they all script their own instances.
    """
    fake = KaspaFake()
    provider, _client = kaspa_provider(fake, network="testnet")

    assert provider.validate_address(KASPA_TESTNET_V0).canonical == KASPA_TESTNET_V0


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(BIP173_TESTNET_P2WPKH, id="an address on another chain"),
        pytest.param(KASPA_UNKNOWN_PREFIX, id="a prefix this application does not accept"),
        pytest.param(SYNTHETIC_TPUB, id="an extended public key, which is not an address"),
        pytest.param("", id="empty"),
        pytest.param("../../info/health", id="a path traversal attempt"),
        pytest.param("kaspatest:qq/../../info/health", id="a vector prefix with a traversal"),
    ],
)
async def test_fetch_balances_refuses_a_bad_address_before_it_builds_a_url(raw: str) -> None:
    """The reason validation happens first, stated as the two path-traversal rows.

    The address arrives from a database column, and interpolating a database value into a
    URL path is the shape of a path-traversal bug: the only thing between it and
    `GET /addresses/../../info/health` is that somebody validated it first. After
    validation the string is a prefix, a colon and charset characters -- no slash, no dot,
    no percent-escape -- by construction rather than by inspection.

    Asserted on both halves: the typed error, and zero requests.
    """
    fake = KaspaFake()
    provider, client = kaspa_provider(fake)

    async with client:
        with pytest.raises(AddressInvalidError):
            await provider.fetch_balances([raw])

    assert fake.requests == []


# --------------------------------------------------------------------------------------
# Criterion 1: sompi to KAS, exactly, through integer base units
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sompi", "expected"),
    [
        pytest.param(0, "0", id="nothing"),
        pytest.param(1, "0.00000001", id="one sompi, the smallest unit there is"),
        pytest.param(ONE_COIN, "1", id="a whole coin"),
        pytest.param(DUST, "0.00054321", id="an odd fraction"),
        pytest.param(123_456_789, "1.23456789", id="every digit position occupied"),
        pytest.param(2_100_000_000 * ONE_COIN, "2100000000", id="more than the whole supply"),
    ],
)
async def test_sompi_converts_to_kas_through_the_domain_rule(sompi: int, expected: str) -> None:
    """Criterion 1: exact, via integer base units, and never through a float.

    The chain counts in sompi and the API answers in sompi, so there is nothing to round
    here -- `AddressBalance.confirmed` stays an integer and `amount()` converts on demand
    through `domain.money.from_base_units`, which is the one module that owns the rule.

    `Decimal` compared against a string literal, not against a float: `Decimal("1.23456789")
    == 1.23456789` is *false*, and a test written with the float would fail for a reason
    that has nothing to do with the provider. The last row is larger than the whole supply
    on purpose -- an implementation that went through a double would lose the low digits
    there and nowhere else in this table.
    """
    fake = KaspaFake(primary=ScriptedInstance(Reply(balance=sompi)))
    provider, client = kaspa_provider(fake)

    async with client:
        balances = await provider.fetch_balances([KASPA_TESTNET_V0])

    assert balances[0].confirmed == sompi
    assert balances[0].decimals == KASPA_DECIMALS
    assert balances[0].amount() == Decimal(expected)


def test_the_parser_hands_back_the_integer_the_chain_counts_in() -> None:
    """Driven directly, so a failure is reported as a parsing bug rather than a transport one.

    The number stays an `int` all the way through. A provider that converted to `Decimal`
    here would introduce a rounding decision at a boundary that has nothing to round, and a
    `float` would be money in binary floating point inside `providers/` -- where the AST
    ban in `tests/security/test_no_float.py` cannot see it, because it reads source and a
    value out of `json.loads` has no literal.
    """
    parsed = parse_address_balance(balance_body(KASPA_TESTNET_V0, ONE_COIN), KASPA_TESTNET_V0)

    assert parsed == ONE_COIN
    assert isinstance(parsed, int)
    assert not isinstance(parsed, bool)


# --------------------------------------------------------------------------------------
# Criterion 2: one address is a GET, more than one is the batch POST
# --------------------------------------------------------------------------------------


async def test_a_single_address_uses_the_single_address_endpoint() -> None:
    """A `GET` where a `GET` exists, and criterion 2 read literally.

    Paying for a `POST` to ask about one address buys nothing and costs both halves of what
    a `GET` gets for free: it is retryable by default and cacheable by every intermediary.
    The vendor's balance endpoint is `Cache-Control: public, max-age=8` in front of a
    Cloudflare cache, which the batch `POST` cannot use at all.

    The endpoint, the method and the label are all asserted from the request the fake
    received rather than assumed. The label is what `request_target` renders into a log, so
    a provider that misspelled it would be correct, quiet and impossible to find in a
    production log.
    """
    fake = KaspaFake(primary=ScriptedInstance(Reply(balance=DUST)))
    provider, client = kaspa_provider(fake)

    async with client:
        balances = await provider.fetch_balances([KASPA_TESTNET_V0])

    assert balances[0].confirmed == DUST
    request = fake.primary.requests[0]
    assert request.method == "GET"
    assert request.url.path == ADDRESS_BALANCE_PATH.format(address=KASPA_TESTNET_V0)
    assert request.url.query == b""
    assert request.extensions.get(ENDPOINT_EXTENSION) == ADDRESS_BALANCE
    assert request.extensions.get(IDEMPOTENT_EXTENSION) is None, (
        "a GET is already retryable by method; declaring it idempotent as well would make "
        "the extension mean 'every read' rather than 'this POST is safe to repeat'"
    )


async def test_more_than_one_address_uses_the_batch_endpoint() -> None:
    """Two addresses is one `POST /addresses/balances`, not two `GET`s.

    The count is the assertion, because the balances come back identically either way. Two
    single reads against a vendor whose rate limit is unpublished is twice the request
    budget spent for the same answer -- and the whole reason this provider declares a batch
    size is that the caller should never have to know which chain batches.
    """
    fake = KaspaFake(primary=ScriptedInstance(Reply(balances={TWO[0]: ONE_COIN, TWO[1]: DUST})))
    provider, client = kaspa_provider(fake)

    async with client:
        balances = await provider.fetch_balances(TWO)

    assert tuple(balance.confirmed for balance in balances) == (ONE_COIN, DUST)
    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 0}
    request = fake.primary.requests[0]
    assert request.method == "POST"
    assert request.url.path == BALANCES_PATH
    assert request.extensions.get(ENDPOINT_EXTENSION) == ADDRESS_BALANCES
    assert json.loads(request.content) == {"addresses": list(TWO)}


async def test_the_batch_request_declares_itself_idempotent_at_the_call_site() -> None:
    """Criterion 10's other half: the opt-in is per request and visible where it is made.

    #6 proposed widening `RetryPolicy.retry_methods` to include `POST`. The policy lives on
    the transport and the transport is process-wide by construction, so that would make
    **every** future `POST` retryable -- including an exchange request that places an
    order, where a retry after a transport error can double a trade. One provider's
    convenience would silently become another's duplicate.
    """
    fake = KaspaFake()
    provider, client = kaspa_provider(fake)

    async with client:
        await provider.fetch_balances(TWO)

    assert fake.primary.requests[0].extensions.get(IDEMPOTENT_EXTENSION) is True


async def test_a_batch_larger_than_the_call_size_is_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sized from the declaration, so the caller never has to know which chain batches.

    The call size is patched down to two because there are four published `kaspatest:`
    vectors and sixty-five would be needed at the shipped ceiling; the shipped ceiling is
    pinned in `test_the_shipped_call_size_is_what_chunk_addresses_actually_splits_at`.

    Asserted as the *shape* of the calls -- two calls of two -- and not as a request count
    alone: four addresses in four calls and four addresses in two calls both make "more
    than one request", and only one of them is batching.
    """
    with_call_size(monkeypatch, 2)
    fake = KaspaFake()
    provider, client = kaspa_provider(fake)

    async with client:
        await provider.fetch_balances(ALL_FOUR)

    assert fake.batches_asked_of(PRIMARY_HOST) == [list(ALL_FOUR[:2]), list(ALL_FOUR[2:])]
    assert fake.counts == {PRIMARY_HOST: 2, FALLBACK_HOST: 0}


async def test_every_requested_address_comes_back_in_order_across_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same length, same order, each entry carrying the address it is about.

    Asserted as the whole tuple rather than as a length and a set: a length assertion
    passes for a reordering and a set assertion passes for a permutation -- and a
    permutation reports one wallet's balance against another wallet's address while every
    total stays plausible.

    Across batches is where the ordering actually breaks. The balances are distinct so a
    provider that returned the right addresses with the wrong numbers fails here, and the
    vendor is scripted to answer its array in **reverse**, because a provider that relied
    on the array's order rather than on the address in each entry would pass against a
    cooperative server and corrupt a portfolio against a real one.
    """
    with_call_size(monkeypatch, 2)
    sums = dict(zip(ALL_FOUR, (1, 2, 3, 4), strict=True))

    def reversed_batches(request: httpx.Request) -> httpx.Response:
        addresses = posted_addresses(request)
        return httpx.Response(
            200, content=batch_body([(address, sums[address]) for address in reversed(addresses)])
        )

    fake = KaspaFake()
    fake.primary.answer = reversed_batches  # type: ignore[method-assign]
    provider, client = kaspa_provider(fake)

    async with client:
        balances = await provider.fetch_balances(ALL_FOUR)

    assert tuple(balance.address for balance in balances) == ALL_FOUR
    assert tuple(balance.confirmed for balance in balances) == (1, 2, 3, 4)


async def test_a_trailing_chunk_of_one_address_uses_the_single_address_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The split is per call, not per request, and the tail is where that shows.

    Three addresses at a call size of two is one batch of two and then **one address left
    over** -- and that one takes the `GET`, for exactly the reason a request of one does: a
    `POST` there buys nothing and loses both retry-by-default and every cache in front of
    the vendor, whose balance endpoint is `Cache-Control: public, max-age=8` behind a
    Cloudflare edge.

    At the shipped size of 64 this is sixty-five addresses, which is a realistic portfolio
    and not a corner. The method of each call is the assertion, because the balances come
    back identically either way.
    """
    with_call_size(monkeypatch, 2)
    fake = KaspaFake(primary=ScriptedInstance(Reply(balance=DUST)))
    provider, client = kaspa_provider(fake)

    async with client:
        balances = await provider.fetch_balances(THREE)

    assert tuple(balance.address for balance in balances) == THREE
    assert [request.method for request in fake.primary.requests] == ["POST", "GET"]
    assert fake.batches_asked_of(PRIMARY_HOST) == [list(THREE[:2])]
    assert fake.addresses_asked_of(PRIMARY_HOST) == [THREE[2]]


async def test_the_batches_are_sequential_and_not_gathered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limiter is a floor on spacing, not a queue whose depth nobody bounded.

    A `gather` would hand the limiter every batch's acquisition at once. Against a vendor
    with an unpublished limit that is the difference between slow and blocked, and the
    order the instance was asked is what a `gather` would scramble.
    """
    with_call_size(monkeypatch, 2)
    fake = KaspaFake()
    provider, client = kaspa_provider(fake)

    async with client:
        await provider.fetch_balances(ALL_FOUR)

    assert fake.batches_asked_of(PRIMARY_HOST) == [list(ALL_FOUR[:2]), list(ALL_FOUR[2:])]


async def test_no_addresses_makes_no_requests_at_all() -> None:
    """An empty wallet table is not a reason to call a public index.

    `chunk_addresses` yields no chunks at all for an empty sequence rather than one empty
    chunk, so this is the difference between no request and a `POST` asking about nothing.
    """
    fake = KaspaFake()
    provider, client = kaspa_provider(fake)

    async with client:
        balances = await provider.fetch_balances([])

    assert balances == ()
    assert fake.requests == []


async def test_the_same_address_requested_twice_is_the_callers_mistake() -> None:
    """A `ValueError`, not a `ProviderResponseError`: the caller made this mistake.

    The two deserve different blame, and a caller catching `ProviderError` would otherwise
    see its own bug reported as the chain's. Refused **before any request**, which is the
    half only the count can see -- a provider that left it to `align_balances` would post
    the whole batch first and then throw the answer away.
    """
    fake = KaspaFake()
    provider, client = kaspa_provider(fake)

    async with client:
        with pytest.raises(ValueError, match=r"distinct") as caught:
            await provider.fetch_balances([KASPA_TESTNET_V0, KASPA_TESTNET_V0])

    assert not isinstance(caught.value, ProviderResponseError)
    assert fake.requests == []


# --------------------------------------------------------------------------------------
# Criterion 3: an unfunded address is a zero, not an error and not an omission
# --------------------------------------------------------------------------------------


async def test_an_unfunded_address_reads_zero_rather_than_an_error() -> None:
    """Zero is what an unused address holds. That is what it means on chain.

    The single-address arm: the vendor answers `{"balance": 0}` and that is a balance of
    nothing, not an absence, not a 404 to be reported as unavailable.
    """
    fake = KaspaFake(primary=ScriptedInstance(Reply(balance=0)))
    provider, client = kaspa_provider(fake)

    async with client:
        balances = await provider.fetch_balances([KASPA_TESTNET_V0])

    assert balances[0].confirmed == 0
    assert balances[0].amount() == Decimal("0")


async def test_an_address_the_batch_omits_reads_zero_not_an_error() -> None:
    """The batch arm, and the one the array shape makes possible.

    A vendor that simply leaves an unfunded address out of its array is answering "this
    address holds nothing", and `align_balances` turns the omission into a zero rather than
    into a short result -- which is what keeps the one-result-per-requested-address
    contract true by construction instead of by the implementer having remembered it.

    The funded address in the same batch is asserted too, so a provider that zeroed the
    whole call fails here.
    """
    funded, unfunded = TWO
    fake = KaspaFake(
        primary=ScriptedInstance(Reply(balances={funded: ONE_COIN}, omit=frozenset({unfunded})))
    )
    provider, client = kaspa_provider(fake)

    async with client:
        balances = balances_by_address(await provider.fetch_balances(TWO))

    assert balances[unfunded].confirmed == 0
    assert balances[funded].confirmed == ONE_COIN


async def test_an_empty_batch_answer_is_every_address_reading_zero() -> None:
    """The degenerate page: an array with nothing in it.

    A vendor answering `[]` for a batch of addresses that all happen to be unused is
    saying the same thing about each of them. A provider that treated an empty array as a
    failure would report an outage for a portfolio of fresh wallets, and one that returned
    a short tuple would break the length contract every caller relies on.
    """
    fake = KaspaFake(primary=ScriptedInstance(Reply(body="[]")))
    provider, client = kaspa_provider(fake)

    async with client:
        balances = await provider.fetch_balances(TWO)

    assert tuple(balance.address for balance in balances) == TWO
    assert all(balance.confirmed == 0 for balance in balances)


# --------------------------------------------------------------------------------------
# Criterion 9: pending is unknown, and never zero
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "id_"),
    [
        pytest.param([KASPA_TESTNET_V0], "the single-address read", id="single"),
        pytest.param(list(TWO), "the batch read", id="batch"),
    ],
)
async def test_pending_is_unknown_because_this_chain_cannot_answer_it(
    requested: list[str], id_: str
) -> None:
    """`None`, never zero, on both code paths -- and the issue's own text asks for zero.

    The issue was written before #7 existed and #7 settled the question the other way:
    `AddressBalance.pending` is `int | None`, and **`None` is what "this chain cannot tell
    you" means**. Zero would be indistinguishable from a Bitcoin address with nothing in
    the mempool, which is a different statement about a different fact, and a dashboard
    cannot honour a distinction it was never given.

    Driven on both paths because they build their result separately: a provider that passed
    no `pending` mapping to `align_balances` on one and an empty `dict` on the other would
    still be correct, but a provider that zero-filled one of them would not -- and only one
    of the two tests would notice.
    """
    del id_  # In the parameter id.
    fake = KaspaFake(primary=ScriptedInstance(Reply(balance=ONE_COIN)))
    provider, client = kaspa_provider(fake)

    async with client:
        balances = await provider.fetch_balances(requested)

    assert all(balance.pending is None for balance in balances)
    assert all(balance.confirmed == ONE_COIN for balance in balances)


async def test_an_unexpected_extra_field_does_not_become_a_pending_balance() -> None:
    """A field the vendor adds tomorrow must not be read as a mempool figure.

    The realistic version is a vendor extending its schema -- the OpenAPI document is not a
    contract anybody signed -- and the dangerous reading is a provider that picked up a
    key called `pending` and reported it. Kaspa's REST balance endpoint exposes nothing of
    the kind; a `pending` that appeared in a body would be some other vendor's field name
    arriving through a proxy, and answering it would turn "this chain cannot tell you" into
    a number.

    The extra field is otherwise ignored rather than refused: a parser that rejected every
    unknown key would break on the day the vendor adds a perfectly innocent one.
    """
    body = json.dumps(
        {"address": KASPA_TESTNET_V0, "balance": ONE_COIN, "pending": 999, "utxos": 3}
    )
    fake = KaspaFake(primary=ScriptedInstance(Reply(body=body)))
    provider, client = kaspa_provider(fake)

    async with client:
        balances = await provider.fetch_balances([KASPA_TESTNET_V0])

    assert balances[0].confirmed == ONE_COIN
    assert balances[0].pending is None


# --------------------------------------------------------------------------------------
# Criteria 4 and 5: throttles, outages, and the errors they become
# --------------------------------------------------------------------------------------


async def test_a_throttle_is_retried_and_then_moves_on() -> None:
    """The two mechanisms compose: the transport retries first, then the provider moves on.

    Backoff is #6's and is already tested there; what this change owns is that **only a 429
    which survived the retry budget** reaches the fallback. So the counts are the
    assertion: three attempts against the primary, because `max_attempts=3`, and then one
    against the fallback. A provider that failed over on the first 429 would show
    `{primary: 1, fallback: 1}` and would throw away the retry policy.
    """
    fake = KaspaFake(
        primary=ScriptedInstance(Reply(status=429)),
        fallback=ScriptedInstance(Reply(balance=ONE_COIN)),
    )
    provider, client = kaspa_provider(fake, max_attempts=3)

    async with client:
        balances = await provider.fetch_balances([KASPA_TESTNET_V0])

    assert balances[0].confirmed == ONE_COIN
    assert fake.counts == {PRIMARY_HOST: 3, FALLBACK_HOST: 1}
    assert fake.hosts_in_order == [PRIMARY_HOST] * 3 + [FALLBACK_HOST]


async def test_a_throttled_batch_is_retried_with_the_very_same_body() -> None:
    """Criterion 10, driven through the provider rather than through the transport alone.

    `tests/providers/test_http.py::test_a_retried_idempotent_post_sends_the_same_body_again`
    proves the transport replays a body. This proves the **provider** builds one that can
    be replayed -- that it passes `json=` rather than a stream.

    A streamed body is consumed on the first attempt and replays as empty, so the server
    answers about no addresses and the balances come back **wrong rather than missing**.
    Every other assertion in this file still passes in that world, because
    `align_balances` reads an address missing from the answer as a zero.

    **The headers are the assertion that actually discriminates here, and that is worth
    being exact about.** `httpx.MockTransport` calls `request.aread()` before handing the
    request to its handler, which materialises any stream and caches it on the request
    object -- so under this fake a body that could never have been replayed replays
    perfectly, and comparing the two recorded requests would prove nothing. What does
    prove something is the shape of the request the provider built: `httpx` sets
    `Content-Length` for a body it has as bytes and `Transfer-Encoding: chunked` for a
    stream whose length it cannot know, so those two headers say which one this is.

    The transport's own materialising is driven where it can actually be seen, against a
    transport that does not repair the request, in
    `tests/providers/test_http.py::test_a_streaming_body_is_materialised_so_the_retry_can
    _replay_it`.
    """
    fake = KaspaFake(
        primary=ScriptedInstance(
            Reply(status=429), Reply(balances={TWO[0]: ONE_COIN, TWO[1]: DUST})
        )
    )
    provider, client = kaspa_provider(fake, max_attempts=2)

    async with client:
        balances = await provider.fetch_balances(TWO)

    first, second = fake.primary.requests
    assert second.content == first.content
    assert posted_addresses(second) == list(TWO)
    assert tuple(balance.confirmed for balance in balances) == (ONE_COIN, DUST)
    assert first.headers.get("content-length") == str(len(first.content))
    assert "transfer-encoding" not in first.headers, (
        "the batch body is a stream, so a retry would replay it as empty and the server "
        "would answer about no addresses -- which reads as a zero for every wallet"
    )


async def test_a_throttle_carrying_retry_after_is_waited_out_before_the_next_attempt() -> None:
    """429 backs off, and it backs off for as long as the server asked.

    The sleep is recorded rather than taken, so the assertion is an exact millisecond value
    and not a measurement of the host. Ignoring a `Retry-After` is how a soft throttle
    becomes a ban, and this is the only test in this file that looks at the *duration*
    rather than at the request count.
    """
    sleep = RecordingSleep()
    fake = KaspaFake(primary=ScriptedInstance(Reply(status=429, headers={"Retry-After": "7"})))
    client = build_http_client(
        transport=httpx.MockTransport(fake.handler),
        policy=RetryPolicy(max_attempts=2, base_backoff_ms=0, max_backoff_ms=30_000),
        limiter=HostRateLimiter(min_interval_ms=0, clock=lambda: 0, sleep=sleep),
        jitter=lambda bound: bound,
        sleep=sleep,
    )
    provider = KaspaProvider(client, settings=kaspa_settings(fallback_url=""))

    async with client:
        with pytest.raises(ProviderRateLimitedError):
            await provider.fetch_balances([KASPA_TESTNET_V0])

    assert sleep.slept_ms == [7_000]


async def test_a_server_error_is_a_retryable_unavailability() -> None:
    """Criterion 5: a 5xx is "temporarily unavailable", never a permanent refusal.

    The distinction is the remedy. `ProviderUnavailableError` means the vendor is broken
    rather than us and the previous reading is still the best information available;
    `ProviderResponseError` means a person has to look and retrying changes nothing.
    Reporting an outage as the second files every reverse-proxy hiccup under "needs a
    human" forever and stops the sync from ever trying again.

    The cause is asserted too, and it is `None`: the endpoints *answered*, they simply
    answered with a refusal, so there is no exception to chain and a stale one would send
    an operator after the wrong host.
    """
    fake = KaspaFake(
        primary=ScriptedInstance(Reply(status=503, body="<html>502</html>")),
        fallback=ScriptedInstance(Reply(status=500, body="<html>500</html>")),
    )
    provider, client = kaspa_provider(fake, max_attempts=1)

    async with client:
        with pytest.raises(ProviderUnavailableError) as caught:
            await provider.fetch_balances([KASPA_TESTNET_V0])

    assert not isinstance(caught.value, ProviderResponseError)
    assert not isinstance(caught.value, ProviderRateLimitedError)
    assert caught.value.__cause__ is None
    assert "html" not in str(caught.value)
    assert KASPA_TESTNET_V0 not in str(caught.value)


async def test_the_documented_422_is_a_response_error_not_an_outage() -> None:
    """The one failure this vendor documents, and waiting will not fix it.

    Measured on 2026-09-23: the service answers 422 with its own rule quoted in the body
    for an address whose path does not match `^kaspa:[a-z0-9]{61,63}$`. Our offline
    validation means this provider should never produce one -- so a 422 that does arrive is
    a change at the vendor, which is exactly the "a person has to look" branch of the
    taxonomy rather than the "wait and it will pass" one.

    The body is asserted absent from the message as well. The vendor's 422 echoes the
    request, which is to say the address.
    """
    detail = json.dumps(
        {"detail": [{"loc": ["path", "kaspaAddress"], "msg": "string does not match"}]}
    )
    fake = KaspaFake(
        primary=ScriptedInstance(Reply(status=422, body=detail)),
        fallback=ScriptedInstance(Reply(status=422, body=detail)),
    )
    provider, client = kaspa_provider(fake, max_attempts=1)

    async with client:
        with pytest.raises(ProviderResponseError) as caught:
            await provider.fetch_balances([KASPA_TESTNET_V0])

    assert not isinstance(caught.value, ProviderUnavailableError)
    assert "kaspaAddress" not in str(caught.value)
    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 1}


@pytest.mark.parametrize(
    ("status", "why"),
    [
        pytest.param(401, "an auth proxy in front of one instance and not the other"),
        pytest.param(403, "a Cloudflare block, which is what this vendor's CDN sends"),
        pytest.param(404, "a base URL missing its suffix, or a route that moved"),
        pytest.param(429, "the throttle"),
        pytest.param(500, "the ordinary outage"),
        pytest.param(503, "the documented lagging-database answer"),
    ],
)
async def test_any_refusal_at_all_moves_to_the_fallback(status: int, why: str) -> None:
    """Every failure to answer moves on, which is the rule #7's review corrected into place.

    A refusal scoped to an *instance* -- a Cloudflare block spelled 403, an auth proxy, a
    base URL missing its path -- is exactly the case two instances exist for, and stopping
    on those leaves a healthy fallback unasked at the moment it is the only thing that
    would work.

    The counts are the assertion. A provider that stopped would show
    `{primary: 1, fallback: 0}` and raise, and no assertion about the returned balance
    could tell the two apart.
    """
    del why  # In the parameter id.
    fake = KaspaFake(
        primary=ScriptedInstance(Reply(status=status, body="nope")),
        fallback=ScriptedInstance(Reply(balance=ONE_COIN)),
    )
    provider, client = kaspa_provider(fake, max_attempts=1)

    async with client:
        balances = await provider.fetch_balances([KASPA_TESTNET_V0])

    assert balances[0].confirmed == ONE_COIN
    assert fake.hosts_in_order == [PRIMARY_HOST, FALLBACK_HOST]


async def test_a_transport_error_on_both_instances_becomes_a_typed_error_with_its_cause() -> None:
    """No `httpx` exception may reach a caller, and the cause must be the **last** failure.

    Asserted by identity rather than by type, because two `ConnectError`s satisfy an
    `isinstance` check whichever one was attached -- and which one is attached is the whole
    subject. An operator reads the traceback, not the class.
    """
    last = httpx.ConnectError("connection refused")
    fake = KaspaFake(
        primary=ScriptedInstance(Reply(error=httpx.ConnectTimeout("timed out"))),
        fallback=ScriptedInstance(Reply(error=last)),
    )
    provider, client = kaspa_provider(fake, max_attempts=1)

    async with client:
        with pytest.raises(ProviderUnavailableError) as caught:
            await provider.fetch_balances([KASPA_TESTNET_V0])

    assert not isinstance(caught.value, httpx.HTTPError)
    assert caught.value.__cause__ is last
    assert PRIMARY_HOST not in str(caught.value)


async def test_a_throttle_on_one_instance_is_not_chained_to_the_others_exception() -> None:
    """#7's fourth lesson, at the provider that inherited the extracted loop.

    A primary that refuses the connection and a fallback that answers 429 must raise
    `ProviderRateLimitedError` chained to nothing. The type and the cause would otherwise
    tell an operator two different stories about one sync -- "you are being throttled",
    caused by "the connection was refused" -- and send them to check a host that was never
    the problem. Every other assertion in this file passes either way, which is the
    property that lets this stay wrong forever.
    """
    fake = KaspaFake(
        primary=ScriptedInstance(Reply(error=httpx.ConnectError("connection refused"))),
        fallback=ScriptedInstance(Reply(status=429)),
    )
    provider, client = kaspa_provider(fake, max_attempts=1)

    async with client:
        with pytest.raises(ProviderRateLimitedError) as caught:
            await provider.fetch_balances([KASPA_TESTNET_V0])

    assert caught.value.__cause__ is None, (
        "the error is classified from the fallback's 429 but chained to the primary's "
        f"{type(caught.value.__cause__).__name__}, so the type and the traceback disagree"
    )


async def test_a_failed_primary_is_not_asked_again_for_the_rest_of_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stickiness, which is the ban-avoidance argument in one assertion.

    Once an instance fails, the remaining batches in **this call** start at the next one.
    Two batches and a primary that answers 403: the primary is asked once and the fallback
    answers both. A provider without stickiness would show two primary requests -- the same
    balances, twice the ban risk.
    """
    with_call_size(monkeypatch, 2)
    fake = KaspaFake(
        primary=ScriptedInstance(Reply(status=403, body="blocked")),
        fallback=ScriptedInstance(Reply(balance=DUST)),
    )
    provider, client = kaspa_provider(fake, max_attempts=1)

    async with client:
        balances = await provider.fetch_balances(ALL_FOUR)

    assert tuple(balance.address for balance in balances) == ALL_FOUR
    assert all(balance.confirmed == DUST for balance in balances)
    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 2}


async def test_a_malformed_body_does_not_fall_over_to_the_fallback() -> None:
    """A 200 we cannot read is a statement about our parser or the vendor's schema.

    The fallback runs the same software and would produce the same unreadable answer, or --
    worse -- a number that hides the fact that we no longer understand the first one.
    Asking it is one more request against a public index for no information at all.
    """
    fake = KaspaFake(
        primary=ScriptedInstance(Reply(body='{"address": "kaspatest:qq", "balance"')),
        fallback=ScriptedInstance(Reply(balance=ONE_COIN)),
    )
    provider, client = kaspa_provider(fake)

    async with client:
        with pytest.raises(ProviderResponseError):
            await provider.fetch_balances([KASPA_TESTNET_V0])

    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 0}


async def test_a_refused_batch_names_its_size_and_no_address() -> None:
    """The batch ceiling is a guess, so the refusal has to say what to correct it to.

    64 is not documented anywhere: the OpenAPI document declares `addresses` as an array of
    strings with no `maxItems` and the operation description names no ceiling, confirmed
    against the live document on 2026-09-22. The first real evidence will be a refused
    batch in production, and the number an operator can act on is **the size of the batch
    that was refused** -- the contents are the owner's holdings and must not appear.

    Deliberately not a fallback to single reads: a batch the server refuses is a configured
    batch size that is too large, which is a value to correct rather than a path to code
    around.
    """
    fake = KaspaFake(
        primary=ScriptedInstance(Reply(status=413, body="Payload Too Large")),
        fallback=ScriptedInstance(Reply(status=413, body="Payload Too Large")),
    )
    provider, client = kaspa_provider(fake, max_attempts=1)

    async with client:
        with pytest.raises(ProviderResponseError) as caught:
            await provider.fetch_balances(THREE)

    message = str(caught.value)
    assert str(len(THREE)) in message, (
        "a refused batch has to name the size that was refused; it is the only number an "
        "operator can act on, and 64 is a guess"
    )
    for address in THREE:
        assert address not in message
        assert address[:20] not in message


# --------------------------------------------------------------------------------------
# The parsers, driven directly through every refusal arm
# --------------------------------------------------------------------------------------
#
# Direct, because a dozen refusal arms reached only through a mock transport would be a
# dozen tests that also depend on the retry loop, the limiter and the URL builder -- and a
# break anywhere in that chain would be reported as a parsing bug.

#: A JSON integer of 5000 digits. CPython refuses to convert an integer string longer than
#: 4300 digits, and `json.loads` does the conversion, so this raises `ValueError` from
#: *inside* the decoder rather than from any check a parser performs.
HUGE_INTEGER: Final = "9" * 5000

#: 5000 nested arrays. `json.loads` recurses, so this raises `RecursionError`, which is a
#: `RuntimeError` and therefore outside anything `except (UnicodeDecodeError, JSONDecodeError)`
#: catches. Both of these escaped #7's parser untyped and were found in review.
DEEPLY_NESTED: Final = "[" * 5000 + "]" * 5000


def test_two_entries_for_one_address_are_refused() -> None:
    """The refusal, **not** a resolution, and this is the test the spec calls load-bearing.

    The batch response is an array, so two entries for one address are possible. The parser
    builds a `dict` for `align_balances`, and a `dict` keeps the last value silently -- so
    two entries with different balances would resolve to whichever the vendor happened to
    send second, and no assertion in `align_balances` could ever see it.

    A parser that keeps the first and one that keeps the last are **both wrong and both
    plausible**, and a test that asserted either one would pin a coin flip. The only
    defensible outcome is a refusal raised while both values are still visible, so this
    test asserts the raise and deliberately asserts nothing about which number survived.
    """
    body = batch_body([(TWO[0], ONE_COIN), (TWO[1], DUST), (TWO[0], 1)])

    with pytest.raises(ProviderResponseError):
        parse_balances(body, TWO)


async def test_a_duplicated_entry_is_refused_through_the_provider_as_well() -> None:
    """The same refusal on the path a real response takes, and it must not be swallowed.

    A provider that caught its own parser's refusal and fell back to the first entry would
    pass the direct test above and corrupt a balance in production. The balances differ by
    a whole coin, so a provider that resolved rather than refused would return a plausible
    number here.
    """
    fake = KaspaFake(
        primary=ScriptedInstance(
            Reply(balances={TWO[0]: ONE_COIN, TWO[1]: DUST}, duplicate=TWO[0], duplicate_balance=1)
        )
    )
    provider, client = kaspa_provider(fake)

    async with client:
        with pytest.raises(ProviderResponseError):
            await provider.fetch_balances(TWO)


def test_a_batch_answer_carrying_an_address_nobody_asked_about_is_refused() -> None:
    """A correlation bug, and silently dropping the entry would hide it.

    A paging mistake, an off-by-one in the request or a vendor echoing a cached batch all
    present this way, and a total that quietly ignored the extra entry would still look
    plausible.

    **The check is per batch, which is strictly stronger than `align_balances`'s.** Once
    several batches are merged into one mapping, an entry that belonged to batch two and
    arrived in batch one is an address that *was* requested somewhere and correlates
    wrongly anyway -- and nothing downstream can see it. The extra address here is one of
    this suite's own vectors, so it is a plausible member of the full request and not an
    obviously foreign string.
    """
    body = batch_body([(TWO[0], 1), (KASPA_TESTNET_V0_ASPECTRON, 2)])

    with pytest.raises(ProviderResponseError) as caught:
        parse_balances(body, [TWO[0]])

    assert KASPA_TESTNET_V0_ASPECTRON not in str(caught.value)


async def test_an_answer_about_an_address_that_was_not_requested_is_refused() -> None:
    """The same correlation failure, through the provider, where it is actually refused."""
    fake = KaspaFake(
        primary=ScriptedInstance(
            Reply(body=batch_body([(TWO[0], 1), (KASPA_TESTNET_V0_ASPECTRON, 2)]))
        )
    )
    provider, client = kaspa_provider(fake)

    async with client:
        with pytest.raises(ProviderResponseError):
            await provider.fetch_balances(TWO)


def test_an_answer_about_a_different_address_is_refused() -> None:
    """The single-address arm of the same rule: a cache or a proxy answering for somebody else.

    It would otherwise be reported as somebody else's balance under this address's name,
    which is the one shape of wrong answer that is indistinguishable from a right one.
    """
    body = balance_body(KASPA_TESTNET_V0_ASPECTRON, ONE_COIN)

    with pytest.raises(ProviderResponseError):
        parse_address_balance(body, KASPA_TESTNET_V0)


@pytest.mark.parametrize(
    ("body", "why"),
    [
        pytest.param("not json at all", "an HTML holding page is not a balance", id="not json"),
        pytest.param('{"address": "x", "balance"', "a truncated body", id="truncated"),
        pytest.param("[]", "an array where an object is documented", id="an array"),
        pytest.param("7", "a bare number", id="a number"),
        pytest.param("null", "a JSON null", id="null"),
        pytest.param('{"balance": 1}', "no address to correlate on", id="no address"),
        pytest.param('{"address": null, "balance": 1}', "a null address", id="null address"),
        pytest.param(f'{{"address": "{KASPA_TESTNET_V0}"}}', "no balance", id="no balance"),
        pytest.param(
            f'{{"address": "{KASPA_TESTNET_V0}", "balance": null}}', "a null balance", id="null bal"
        ),
        pytest.param(
            f'{{"address": "{KASPA_TESTNET_V0}", "balance": "100"}}', "a string", id="string bal"
        ),
        pytest.param(
            f'{{"address": "{KASPA_TESTNET_V0}", "balance": 1.0e8}}', "a float", id="float bal"
        ),
        pytest.param(
            f'{{"address": "{KASPA_TESTNET_V0}", "balance": true}}', "a bool", id="bool bal"
        ),
        pytest.param(
            f'{{"address": "{KASPA_TESTNET_V0}", "balance": -1}}', "negative", id="negative bal"
        ),
        pytest.param(
            f'{{"address": "{KASPA_TESTNET_V0}", "balance": {HUGE_INTEGER}}}',
            "5000 digits, which CPython's integer limit refuses inside json.loads",
            id="a sum of 5000 digits",
        ),
        pytest.param(DEEPLY_NESTED, "5000 nested arrays, which raise RecursionError", id="nested"),
    ],
)
def test_a_single_address_body_that_cannot_be_trusted_is_refused(body: str, why: str) -> None:
    """Every arm, and the last two are the ones a reader would not have written down.

    `_decode` has to catch `ValueError` and `RecursionError`, not `JSONDecodeError` and
    `UnicodeDecodeError`: a 5000-digit integer raises `ValueError` from CPython's 4300-digit
    limit *inside* `json.loads`, and 5000 nested arrays raise `RecursionError`, which is not
    a `ValueError` at all. Both escaped #7's parser untyped and reached `health()`, which is
    documented as never raising. A vendor that is broken or hostile sends the body nobody
    pictured, which is the only kind this boundary exists for.

    The float row is rule 2 arriving through a door the AST ban cannot see: `1.0e8` out of
    `json.loads` is a `float` with no literal anywhere in the source. The bool row is the
    one the static type cannot stand in for either -- `True` is an `int`, so it would be
    read as one sompi and reported as a holding.
    """
    del why  # In the parameter id, where a failure can read it.

    with pytest.raises(ProviderResponseError):
        parse_address_balance(body, KASPA_TESTNET_V0)


@pytest.mark.parametrize(
    ("body", "why"),
    [
        pytest.param("not json at all", "an HTML holding page", id="not json"),
        pytest.param('[{"address": "x", "balance"', "a truncated array", id="truncated"),
        pytest.param('{"balances": []}', "an object where an array is documented", id="an object"),
        pytest.param("7", "a bare number", id="a number"),
        pytest.param('["kaspatest:qq"]', "an array of strings, not of objects", id="strings"),
        pytest.param("[null]", "a null entry", id="null entry"),
        pytest.param('[{"balance": 1}]', "an entry with no address", id="no address"),
        pytest.param(
            f'[{{"address": "{KASPA_TESTNET_V0}"}}]', "an entry with no balance", id="no balance"
        ),
        pytest.param(
            f'[{{"address": "{KASPA_TESTNET_V0}", "balance": "100"}}]', "a string", id="string bal"
        ),
        pytest.param(
            f'[{{"address": "{KASPA_TESTNET_V0}", "balance": 1.0e8}}]', "a float", id="float bal"
        ),
        pytest.param(
            f'[{{"address": "{KASPA_TESTNET_V0}", "balance": true}}]', "a bool", id="bool bal"
        ),
        pytest.param(
            f'[{{"address": "{KASPA_TESTNET_V0}", "balance": -1}}]', "negative", id="negative bal"
        ),
        pytest.param(
            '[{"address": 7, "balance": 1}]', "an address that is not a string", id="int address"
        ),
        pytest.param(DEEPLY_NESTED, "5000 nested arrays", id="nested"),
    ],
)
def test_a_batch_body_that_cannot_be_trusted_is_refused(body: str, why: str) -> None:
    """The array shape, arm by arm. `[]` is deliberately **not** here: it is a valid answer.

    An empty array means every requested address holds nothing, which
    `test_an_empty_batch_answer_is_every_address_reading_zero` asserts. Refusing it would
    report an outage for a portfolio of fresh wallets.
    """
    del why  # In the parameter id, where a failure can read it.

    with pytest.raises(ProviderResponseError):
        parse_balances(body, [KASPA_TESTNET_V0])


def test_no_parser_rejection_names_the_address() -> None:
    """The #44 shape, driven through every rejection arm both parsers have.

    A hand-written parser rather than a pydantic model, because a `ValidationError`
    **renders the input that failed** -- and the input here is a response body containing
    the owner's address, which then travels into a log the moment anything calls
    `logger.exception`. So every message names a field and a type and never a value.

    Asserted over `str(exc)` and over `exc.args`, because a message built correctly and an
    argument tuple built carelessly are two different mistakes.
    """
    address = KASPA_TESTNET_V0
    bodies = (
        json.dumps({"address": address}),
        json.dumps({"address": address, "balance": "100"}),
        json.dumps({"address": address, "balance": -1}),
        json.dumps({"address": KASPA_TESTNET_V0_ASPECTRON, "balance": 1}),
    )

    for body in bodies:
        with pytest.raises(ProviderResponseError) as caught:
            parse_address_balance(body, address)

        assert address not in str(caught.value)
        assert address[:20] not in str(caught.value)
        assert all(address not in str(argument) for argument in caught.value.args)

    batch = batch_body([(address, ONE_COIN), (address, DUST)])
    with pytest.raises(ProviderResponseError) as duplicate:
        parse_balances(batch, [address])

    assert address not in str(duplicate.value)
    assert address[:20] not in str(duplicate.value)


def test_the_no_address_assertion_can_actually_fail() -> None:
    """The control. Without it the test above proves only that some exception was raised.

    An assertion that a string is absent from a message is satisfied by a message that says
    nothing, which is the vacuous pass this project keeps finding in new costumes.
    """
    leaked = ProviderResponseError(f"could not parse the balance for {KASPA_TESTNET_V0}")

    assert KASPA_TESTNET_V0 in str(leaked)
    assert any(KASPA_TESTNET_V0 in str(argument) for argument in leaked.args)


def test_a_batch_body_with_one_entry_is_a_page_of_one() -> None:
    """The single-entry page, which is what a two-address batch answers when one is unused.

    The control on the empty-array case: a parser that special-cased length zero and length
    many could still be wrong at one, and one is the length that arrives most often.
    """
    body = batch_body([(KASPA_TESTNET_V0, ONE_COIN)])

    assert parse_balances(body, TWO) == {KASPA_TESTNET_V0: ONE_COIN}


# --------------------------------------------------------------------------------------
# Health: synced, indexed, and saying nothing about which node
# --------------------------------------------------------------------------------------


#: Health bodies that are JSON, are objects, and are still not health reports. Each one is
#: a shape a proxy, a schema change or a vendor with a loose serialiser can produce, and
#: each reaches a different refusal in `parse_health`.
#:
#: **`FLAGS_AS_STRINGS` is the one that matters and it is not a corner case.** `"false"` is
#: a non-empty string and therefore *truthy*, so a health check written with `if
#: server.get("isSynced")` reads every node as synced and indexed -- and answers **healthy**
#: for a vendor whose nodes are all unusable. That is a wrong number rather than an error,
#: arriving from the one endpoint whose whole job is to prevent wrong numbers.
NODES_NOT_AN_ARRAY: Final = '{"kaspadServers": {"0": {}}, "database": {"isSynced": true}}'
NODE_NOT_AN_OBJECT: Final = '{"kaspadServers": ["not-an-object"], "database": {"isSynced": true}}'
FLAGS_AS_STRINGS: Final = (
    '{"kaspadServers": [{"isSynced": "false", "isUtxoIndexed": "false"}], '
    '"database": {"isSynced": true}}'
)
FLAGS_MISSING: Final = '{"kaspadServers": [{"blueScore": 1}], "database": {"isSynced": true}}'
DATABASE_NOT_AN_OBJECT: Final = '{"kaspadServers": [], "database": []}'


@pytest.mark.parametrize(
    ("body", "why"),
    [
        pytest.param(NODES_NOT_AN_ARRAY, "an object keyed by index", id="nodes not an array"),
        pytest.param(NODE_NOT_AN_OBJECT, "a node that is a bare string", id="node not an object"),
        pytest.param(FLAGS_AS_STRINGS, "the strings 'false', which are truthy", id="string flags"),
        pytest.param(FLAGS_MISSING, "a node carrying neither flag", id="flags missing"),
        pytest.param(DATABASE_NOT_AN_OBJECT, "database as an array", id="database not an object"),
        pytest.param("<html>holding page</html>", "an HTML holding page", id="html"),
        pytest.param("[]", "an array where an object is documented", id="array"),
    ],
)
def test_a_health_report_that_is_not_one_is_refused(body: str, why: str) -> None:
    """`parse_health` refuses a shape rather than reading a verdict out of it.

    Driven directly, because the arms reached only through `health()` all collapse into one
    unhealthy answer -- so a parser that refused the wrong thing, or refused nothing, would
    look identical from outside. Here each shape is its own row.

    **The string-flag row is the reason `_require_flag` tests `isinstance(value, bool)`.**
    A vendor rendering `isSynced` as `"false"` is not hypothetical -- it is what a loosely
    typed serialiser does -- and truthiness would read it as `True`, report the node as
    synced and UTXO-indexed, and answer healthy while every balance read failed. Strictness
    at a trust boundary is what turns that into a refusal a person can see.
    """
    del why  # In the parameter id, where a failure can read it.

    with pytest.raises(ProviderResponseError):
        parse_health(body)


def test_a_well_formed_health_report_is_still_read() -> None:
    """The control. A parser that refused everything would pass every row above.

    Without this, `parse_health` could ship raising on the vendor's real answer and the
    only symptom would be a chain permanently reported as unhealthy -- which reads as an
    outage rather than as a bug, and would be chased at the vendor.
    """
    parsed = parse_health(health_body(nodes=((True, True), (True, False), (False, False))))

    assert parsed.database_synced is True
    assert parsed.usable_nodes == 1
    assert parsed.nodes == 3


async def test_a_refused_health_report_is_unhealthy_rather_than_an_exception() -> None:
    """The refusals above have to reach `health()` as a verdict, not as a traceback.

    `health()` is documented as never raising, and it is what an operations view calls --
    so a parser refusal that escaped would take down the page whose whole job is to say the
    chain is down. The detail names the reason and nothing about the vendor's nodes.
    """
    fake = KaspaFake(
        primary=ScriptedInstance(Reply(body=FLAGS_AS_STRINGS)),
        fallback=ScriptedInstance(Reply(body=NODE_NOT_AN_OBJECT)),
    )
    provider, client = kaspa_provider(fake, max_attempts=1)

    async with client:
        health = await provider.health()

    assert health.healthy is False
    assert health.detail is not None
    assert "isSynced" not in health.detail
    assert "not-an-object" not in health.detail


async def test_health_requires_a_synced_database_and_an_indexed_node() -> None:
    """The happy path, and it is stricter than a ping on purpose.

    A node that is reachable but not synced returns balances that are *stale and well
    formed*, which is the failure this project keeps finding in other clothes: a wrong
    number is worse than an error. So healthy requires a 200, `database.isSynced`, and at
    least one `kaspadServers` entry with both `isSynced` and `isUtxoIndexed`.

    The endpoint and the label are asserted off the request, and the path is the documented
    one rather than an assumed one.
    """
    fake = KaspaFake(primary=ScriptedInstance(Reply(nodes=((True, True),))))
    provider, client = kaspa_provider(fake)

    async with client:
        health = await provider.health()

    assert health.healthy is True
    assert health.chain_key is ChainKey.KASPA
    request = fake.primary.requests[0]
    assert request.method == "GET"
    assert request.url.path == HEALTH_PATH
    assert request.extensions.get(ENDPOINT_EXTENSION) == NODE_HEALTH
    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 0}


async def test_a_node_without_a_utxo_index_is_not_healthy() -> None:
    """`isUtxoIndexed` is the field a reader would drop as redundant. It is not.

    A node without the UTXO index is synced and simply **cannot answer a balance query**,
    which is precisely the state where a ping-shaped health check says yes and every read
    fails. The database is synced here and the node is synced here; the only thing wrong is
    the index, so a health check that looked at anything else passes this and lies.
    """
    fake = KaspaFake(
        primary=ScriptedInstance(Reply(nodes=((True, False),))),
        fallback=ScriptedInstance(Reply(nodes=((True, False),))),
    )
    provider, client = kaspa_provider(fake, max_attempts=1)

    async with client:
        health = await provider.health()

    assert health.healthy is False


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        pytest.param(Reply(nodes=((False, True),)), "a node that is indexed but not synced"),
        pytest.param(Reply(nodes=((True, False), (False, True))), "neither node is both"),
        pytest.param(Reply(nodes=()), "no backing nodes at all"),
        pytest.param(Reply(database_synced=False), "the database lags behind the nodes"),
        pytest.param(Reply(status=503), "the documented lagging-database answer"),
        pytest.param(Reply(status=500), "an ordinary outage"),
        pytest.param(Reply(body="<html>holding page</html>"), "a 200 carrying HTML"),
        pytest.param(Reply(body='{"database": {"isSynced": true}}'), "no kaspadServers key"),
        pytest.param(Reply(body='{"kaspadServers": []}'), "no database key"),
        pytest.param(Reply(body="{}"), "an empty object"),
        pytest.param(Reply(body=DEEPLY_NESTED), "a body that breaks the decoder itself"),
        pytest.param(Reply(error=httpx.ConnectError("refused")), "nothing connects"),
        pytest.param(Reply(body=NODES_NOT_AN_ARRAY), "kaspadServers is an object"),
        pytest.param(Reply(body=NODE_NOT_AN_OBJECT), "a node entry that is a string"),
        pytest.param(Reply(body=FLAGS_AS_STRINGS), "flags rendered as the strings 'false'"),
        pytest.param(Reply(body=FLAGS_MISSING), "a node with neither flag"),
        pytest.param(Reply(body=DATABASE_NOT_AN_OBJECT), "database is an array"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
async def test_health_reports_unhealthy_without_raising(reply: Reply, why: str) -> None:
    """`health()` never raises, whatever a broken or hostile instance sends.

    An operations view has to be able to report "the chain is down" without catching
    anything, and the nested-array row is the one that made that untrue in #7: 5000 nested
    arrays raise `RecursionError` out of `json.loads`, which is not a `ValueError` and so
    escaped a parser whose contract said it never raises -- from a body a hostile instance
    chooses freely.

    Both instances answer the same way, so this is about the verdict rather than about
    failover.
    """
    del why  # In the parameter id.
    fake = KaspaFake(ScriptedInstance(reply), ScriptedInstance(reply))
    provider, client = kaspa_provider(fake, max_attempts=1)

    async with client:
        health = await provider.health()

    assert health.healthy is False
    assert health.chain_key is ChainKey.KASPA


async def test_health_tries_the_fallback_when_the_primary_is_unusable() -> None:
    """Healthy if *either* instance answers, and the detail says which.

    A position rather than a URL, because `detail` is rendered in an operations view and
    reaches a log, and a URL there would name the deployment.
    """
    fake = KaspaFake(
        primary=ScriptedInstance(Reply(nodes=((True, False),))),
        fallback=ScriptedInstance(Reply(nodes=((True, True),))),
    )
    provider, client = kaspa_provider(fake, max_attempts=1)

    async with client:
        health = await provider.health()

    assert health.healthy is True
    assert health.detail is not None
    assert FALLBACK in health.detail
    assert fake.counts == {PRIMARY_HOST: 1, FALLBACK_HOST: 1}


async def test_health_detail_never_names_a_backend_node() -> None:
    """`kaspadHost` must never leave the provider, and neither must anything beside it.

    It names the vendor's internal node topology, and `ProviderHealth.detail` is rendered
    in an operations view and reaches a log. `detail` says **how many** nodes were synced
    and indexed, never which or where.

    Driven over a healthy answer and an unhealthy one, because the two build their detail
    separately and it is the failing one that a person actually reads. The body carries
    `kaspadHost`, `serverVersion` and `p2pId` in every case, so a provider that copied any
    of them into the detail fails here rather than in production.
    """
    for reply in (Reply(nodes=((True, True), (True, True))), Reply(nodes=((True, False),))):
        fake = KaspaFake(ScriptedInstance(reply), ScriptedInstance(reply))
        provider, client = kaspa_provider(fake, max_attempts=1)

        async with client:
            health = await provider.health()

        detail = health.detail or ""
        for forbidden in (
            KASPAD_HOST,
            "kaspadHost",
            "p2p-0",
            "p2pId",
            "0.15.2",
            str(BLUE_SCORE),
            PRIMARY_HOST,
            FALLBACK_HOST,
            PRIMARY_URL,
            FALLBACK_URL,
            "://",
        ):
            assert forbidden not in detail, f"ProviderHealth.detail disclosed {forbidden!r}"
        assert len(detail) <= 120


async def test_health_says_how_many_nodes_were_usable() -> None:
    """The control on the test above: a detail that said nothing would pass it.

    An absence assertion over an empty string is satisfied for the wrong reason, and this
    project has now caught that shape twice in its logging tests. `detail` has to carry
    something an operator can act on -- a count -- while carrying nothing that identifies a
    node.
    """
    fake = KaspaFake(
        primary=ScriptedInstance(Reply(nodes=((True, True), (True, False), (False, True))))
    )
    provider, client = kaspa_provider(fake)

    async with client:
        health = await provider.health()

    assert health.healthy is True
    assert health.detail
    assert any(character.isdigit() for character in health.detail), (
        "detail must say how many nodes were synced and indexed; a detail with no count "
        "tells an operator nothing they can act on"
    )


async def test_health_reads_no_address_because_it_never_has_one() -> None:
    """A failing health check must not itself disclose what is being watched.

    Separate from `fetch_balances` precisely so an operations view can say "the chain is
    down" without naming a wallet. Asserted as the request log: no address-shaped path was
    ever requested and no body was ever posted.
    """
    fake = KaspaFake(primary=ScriptedInstance(Reply(status=500)))
    provider, client = kaspa_provider(fake, fallback_url="", max_attempts=1)

    async with client:
        health = await provider.health()

    assert health.healthy is False
    assert all(request.url.path == HEALTH_PATH for request in fake.requests)
    assert all(request.method == "GET" for request in fake.requests)
    assert all(not request.content for request in fake.requests)


async def test_health_says_so_when_no_endpoint_is_configured() -> None:
    """Both URLs blank is a misconfiguration, and it is not an exception.

    `health()` is what an operator reads to tell a broken vendor from a broken sync, so the
    one case where there is nothing to ask has to come back as an unhealthy answer rather
    than as a traceback in whatever rendered the page.
    """
    fake = KaspaFake()
    provider, client = kaspa_provider(fake, primary_url="", fallback_url="")

    async with client:
        health = await provider.health()

    assert health.healthy is False
    assert fake.requests == []


def test_the_health_body_this_suite_scripts_is_the_shape_the_vendor_documents() -> None:
    """The fixture is checked against the documented schema, not only against the parser.

    A harness and a parser that agree with each other and disagree with the vendor is the
    failure that a mock-based suite is most prone to, and the only defence is asserting the
    fixture's shape against what was read off the live OpenAPI document on 2026-09-22.
    """
    document = json.loads(health_body(nodes=((True, True),)))

    assert set(document) == {"kaspadServers", "database"}
    assert set(document["kaspadServers"][0]) == {
        "kaspadHost",
        "serverVersion",
        "isUtxoIndexed",
        "isSynced",
        "p2pId",
        "blueScore",
    }
    assert set(document["database"]) == {
        "isSynced",
        "blueScore",
        "blueScoreDiff",
        "acceptedTxBlockTime",
        "acceptedTxBlockTimeDiff",
    }


# --------------------------------------------------------------------------------------
# The documented endpoints, pinned as the literals the vendor publishes
# --------------------------------------------------------------------------------------


def test_the_three_endpoints_are_the_ones_the_document_declares() -> None:
    """Pinned, because a path is a vendor fact and not an implementation detail.

    Confirmed against the live OpenAPI document on 2026-09-22. A provider that changed one
    of these would still pass every behavioural test in this file, because the fake routes
    on the **host** rather than on the URL -- which is deliberate, and this is the test that
    pays for it.
    """
    assert ADDRESS_BALANCE_PATH == "/addresses/{address}/balance"
    assert BALANCES_PATH == "/addresses/balances"
    assert HEALTH_PATH == "/info/health"


# --------------------------------------------------------------------------------------
# The shipped settings, observed rather than injected
# --------------------------------------------------------------------------------------
#
# Every test above builds its own `Settings`, which is the discipline that keeps them
# deterministic and is exactly what leaves the shipped defaults unobserved -- the #6
# lesson, where `DEFAULT_RETRY_POLICY.max_attempts` could have shipped as 1 with 1102 tests
# green. So these tests pass no arguments at all.


def test_the_shipped_network_is_mainnet() -> None:
    """A fresh deployment reads mainnet, which is the only useful default.

    Pinned as a literal: `Settings().kaspa_network == Settings().kaspa_network` is true of
    any value at all, including one that would make every real address refused with
    `WRONG_NETWORK` and no clue as to why.
    """
    assert Settings().kaspa_network == "mainnet"


def test_the_shipped_endpoints_are_one_public_instance_and_a_deliberate_blank() -> None:
    """Pinned as literals, because these are the URLs a fresh deployment actually calls.

    Not derived from the field defaults -- `Settings().kaspa_api_url ==
    Settings().kaspa_api_url` is true of any value at all, including an empty string, which
    would silently make a fresh deployment "one instance only" with no instance.

    **The blank fallback is the decision worth pinning.** Bitcoin ships two independent
    public Esplora instances; there is one well-known public kaspa-rest-server operator and
    no second one to name, so inventing a fallback would mean either a URL nobody operates
    or the primary written twice -- and the second is the configuration #7 spent a test
    proving is actively harmful, because a 429 would cost the retry budget and then the
    failover would spend it again on the host that has just asked us to stop.

    A blank is therefore "one instance only", the supported spelling, and an operator who
    runs their own index fills it in. Asserting it deliberately is what stops the blank
    being read later as an omission somebody forgot to finish.
    """
    settings = Settings()

    assert settings.kaspa_api_url == "https://api.kaspa.org"
    assert settings.kaspa_api_fallback_url == ""
    assert settings.kaspa_api_url != settings.kaspa_api_fallback_url


async def test_the_shipped_blank_fallback_means_exactly_one_instance() -> None:
    """The literal above says what is configured; this says what it does.

    A blank must not become a request to the empty string -- which `httpx` would refuse as
    a relative URL, from inside the provider, as something other than a `ProviderError` --
    and it must not become a second attempt against the primary either. The count is the
    only thing that can see the difference: the balances, the error type and the message
    are identical whether one host is asked once or twice.
    """
    fake = KaspaFake(primary=ScriptedInstance(Reply(status=503)))
    provider, client = kaspa_provider(fake, fallback_url="", max_attempts=2)

    async with client:
        with pytest.raises(ProviderUnavailableError):
            await provider.fetch_balances([KASPA_TESTNET_V0])

    assert fake.counts == {PRIMARY_HOST: 2, FALLBACK_HOST: 0}


@pytest.mark.parametrize("network", ["mainnet", "testnet", "devnet"])
def test_every_network_name_is_accepted_by_the_setting(network: str) -> None:
    """The three the enum defines, and nothing else, so a typo fails at startup.

    A free-form string would let `PORTFOLIO_KASPA_NETWORK=testnet10` build a `Settings` and
    then compare unequal to every member, which presents as a provider refusing every
    address with `WRONG_NETWORK` and no clue as to why.
    """
    assert kaspa_settings(network=network).kaspa_network == network


def test_a_network_that_does_not_exist_is_refused_at_construction() -> None:
    """The control for the row above."""
    with pytest.raises(ValueError, match=r"kaspa_network"):
        Settings(kaspa_network="testnet10")


async def test_a_provider_built_with_no_settings_uses_the_shipped_ones() -> None:
    """`KaspaProvider(client)` -- the registry's own construction -- reads `get_settings()`.

    Every other test in this file hands over a `Settings` it built, which is the discipline
    that keeps them deterministic and is exactly what leaves the `settings is None` fallback
    observed only as a *covered line*. Coverage says the branch ran; it says nothing about
    whether the object it produced is the one production gets.

    Both halves are observed from outside, and neither needs a mainnet address. The
    **network** is observed through a refusal: the shipped default is `mainnet`, so a
    provider built this way must refuse a `kaspatest:` vector with `WRONG_NETWORK`. The
    **URL** is observed through `health()`, which reads `/info/health` and names no address
    at all, and the host is compared against the setting rather than against a literal --
    the literal lives in the test above, and the pair says the value is both intended and
    applied.
    """
    shipped = Settings()
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, content=health_body())

    client = build_http_client(
        transport=httpx.MockTransport(handler),
        policy=RetryPolicy(max_attempts=1, base_backoff_ms=0, max_backoff_ms=0),
        limiter=HostRateLimiter(min_interval_ms=0, clock=lambda: 0, sleep=_no_sleep),
        sleep=_no_sleep,
    )
    provider = KaspaProvider(client)

    with pytest.raises(AddressInvalidError) as caught:
        provider.validate_address(KASPA_TESTNET_V0)

    assert caught.value.reason is AddressRejection.WRONG_NETWORK
    assert shipped.kaspa_network == "mainnet"

    async with client:
        health = await provider.health()

    assert health.healthy is True
    assert str(recorded[0].url.host) == httpx.URL(shipped.kaspa_api_url).host
    base_path = httpx.URL(shipped.kaspa_api_url).path.rstrip("/")
    assert recorded[0].url.path == f"{base_path}{HEALTH_PATH}"


def test_the_position_names_are_the_shared_ones() -> None:
    """Kaspa reports the same two positions Bitcoin does, out of the extracted module.

    Two providers inventing their own words for "the first one" is how an operations view
    ends up showing `primary` for one chain and `main` for the other, and the extraction is
    what makes that impossible rather than merely discouraged.
    """
    assert PRIMARY == "primary"
    assert FALLBACK == "fallback"


_CONFORMS: ChainProvider = KaspaProvider(httpx.AsyncClient(), settings=kaspa_settings())
"""Criterion 7 of #6, applied to the second real provider.

`mypy --strict` deciding assignability is the assertion; there is no `isinstance` here and
there must not be one. `ChainProvider` is deliberately not `@runtime_checkable`, so an
`isinstance` check would compare four attribute names and say nothing about whether
`fetch_balances` takes a sequence or whether `health` is a coroutine function.

The client is never used -- nothing is awaited on this instance -- so no connection pool is
opened by importing this module.
"""
