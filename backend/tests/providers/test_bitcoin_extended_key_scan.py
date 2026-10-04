"""Spec 031, criteria 3, 4 (provider side) and 6: `EsploraProvider.scan_extended_key`.

A fake Esplora instance answers **by address**: each address it is asked about gets the
holding scripted for it, and every other address is unused and empty. That is what lets the
gap scan be tested as a gap scan. The fixture names the used addresses by their position in
BIP-84's account key (as `vpub`, R11), and the assertions are about which addresses were
asked about, in which order, and how many times -- which is the vendor's view of the scan,
and the only one that shows a scan that reads too far or not far enough.

The limiter tests use the real `build_http_client` -- the real `RetryingTransport` and a
real `HostRateLimiter` -- with an injected clock that only moves when the limiter sleeps.
The arrival time of each request at the fake is then exact, and "every request was at
least the interval after the one before it" is an equality rather than a measurement.

Every key and address here is a test-network form, from `tests/extended_key_vectors.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING

import anyio
import httpx
import pytest

from portfolio.domain import extended_keys
from portfolio.domain.addresses import AddressInvalidError, AddressRejection
from portfolio.domain.extended_keys import (
    CHANGE_BRANCH,
    GAP_LIMIT,
    HARDENED_INDEX,
    MAX_ADDRESSES_PER_BRANCH,
    RECEIVE_BRANCH,
    parse_extended_public_key,
)
from portfolio.domain.secp256k1 import CURVE_ORDER
from portfolio.providers.base import (
    ExtendedKeyScan,
    ExtendedKeyScanner,
    KnownDerivedAddress,
    ScannedAddress,
)
from portfolio.providers.chains import bitcoin
from portfolio.providers.chains.bitcoin import (
    BITCOIN_DECIMALS,
    BRANCH_CAP_MESSAGE,
    AddressStats,
    EsploraProvider,
    parse_address_response,
)
from portfolio.providers.chains.kaspa import KaspaProvider
from portfolio.providers.errors import ProviderResponseError
from portfolio.providers.http import (
    DEFAULT_MIN_HOST_INTERVAL_MS,
    HostRateLimiter,
)
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WPKH_UPPERCASE,
    CORE_REGTEST_P2WPKH,
    KASPA_TESTNET_V0,
    SYNTHETIC_TPUB,
)
from tests.extended_key_harness import (
    CRITERION_THREE_CONFIRMED,
    AddressBook,
    DerivationSpy,
    Holding,
    criterion_three_book,
    esplora_body,
    provider_over,
    spy_on_derivation,
)
from tests.extended_key_vectors import (
    BIP32_TV1_M,
    BIP49_ACCOUNT_UPUB,
    BIP49_RECEIVE_0_ADDRESS,
    BIP84_CHILDREN,
    DERIVED_VPUB_MULTISIG,
    ENCODINGS,
    SCAN_CHANGE,
    SCAN_KEY,
    SCAN_RECEIVE,
    SCAN_USED_CHANGE,
    TV1_MASTER_CHILDREN_P2PKH,
    short,
)
from tests.providers.chains.harness import FALLBACK_HOST, PRIMARY_HOST, esplora_settings
from tests.providers.harness import FakeClock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

# --------------------------------------------------------------------------------------
# Helpers over the shared fake (`tests/extended_key_harness.py`)
# --------------------------------------------------------------------------------------


async def scan(
    book: AddressBook,
    key: str = SCAN_KEY,
    known: Sequence[KnownDerivedAddress] = (),
    **options: object,
) -> ExtendedKeyScan:
    provider, client = provider_over(book, **options)  # type: ignore[arg-type]
    async with client:
        return await provider.scan_extended_key(key, known)


def on_branch(result: ExtendedKeyScan, branch: int) -> list[ScannedAddress]:
    return [address for address in result.addresses if address.branch == branch]


def indices(result: ExtendedKeyScan, branch: int) -> list[int]:
    return [address.index for address in on_branch(result, branch)]


def known_from(result: ExtendedKeyScan) -> list[KnownDerivedAddress]:
    """What the sync would persist and hand back: every scanned address, as it was."""
    return [
        KnownDerivedAddress(
            branch=address.branch, index=address.index, address=address.address, used=address.used
        )
        for address in result.addresses
    ]


@pytest.fixture
def derivation_spy(monkeypatch: pytest.MonkeyPatch) -> DerivationSpy:
    return spy_on_derivation(monkeypatch)


# --------------------------------------------------------------------------------------
# Conformance
# --------------------------------------------------------------------------------------


_SCANNER: ExtendedKeyScanner = EsploraProvider(httpx.AsyncClient(), settings=esplora_settings())
"""`mypy --strict` deciding that the Esplora provider satisfies the protocol's signature.

The client is never used, so importing this module opens no connection pool.
"""


def test_the_esplora_provider_is_an_extended_key_scanner_and_kaspas_is_not() -> None:
    """The run-time gate the balance sync uses to fail a chain loudly as `internal`."""
    assert isinstance(_SCANNER, ExtendedKeyScanner)
    assert not hasattr(KaspaProvider, "scan_extended_key")


def test_the_provider_uses_the_domains_limits() -> None:
    """One copy of each number: the provider imports them, it does not restate them."""
    assert vars(bitcoin)["MAX_ADDRESSES_PER_BRANCH"] is extended_keys.MAX_ADDRESSES_PER_BRANCH
    assert MAX_ADDRESSES_PER_BRANCH == 1000
    assert GAP_LIMIT == 20
    assert str(MAX_ADDRESSES_PER_BRANCH) in BRANCH_CAP_MESSAGE


# --------------------------------------------------------------------------------------
# Criterion 3: the gap scan finds every funded address of the fixture
# --------------------------------------------------------------------------------------


async def test_the_scan_finds_every_used_address_of_the_fixture() -> None:
    book = criterion_three_book()

    result = await scan(book)

    used = {(address.branch, address.index) for address in result.addresses if address.used}
    assert used == {(RECEIVE_BRANCH, index) for index in (0, 5, 24)} | {
        (CHANGE_BRANCH, index) for index in SCAN_USED_CHANGE
    }
    assert result.decimals == BITCOIN_DECIMALS == 8


async def test_each_branch_ends_twenty_past_its_last_used_address() -> None:
    """Receive: 24 + 20 = 44, so 45 addresses. Change: 3 + 20 = 23, so 24. 69 requests."""
    book = criterion_three_book()

    result = await scan(book)

    assert indices(result, RECEIVE_BRANCH) == list(range(45))
    assert indices(result, CHANGE_BRANCH) == list(range(24))
    assert len(book.asked) == 69
    assert SCAN_RECEIVE[44] in book.asked
    assert SCAN_CHANGE[23] in book.asked
    assert SCAN_RECEIVE[45] not in book.asked
    assert SCAN_CHANGE[24] not in book.asked


async def test_a_used_address_more_than_twenty_past_the_last_is_not_reached() -> None:
    """Receive 46 is 22 past 24. Never asked about, by design: that is the gap limit."""
    book = criterion_three_book()

    result = await scan(book)

    assert SCAN_RECEIVE[46] not in book.asked
    assert SCAN_RECEIVE[46] not in {address.address for address in result.addresses}


async def test_the_scan_reads_receive_then_change_each_in_index_order() -> None:
    book = criterion_three_book()

    result = await scan(book)

    assert [address.address for address in result.addresses] == book.asked
    assert [(address.branch, address.index) for address in result.addresses] == [
        (RECEIVE_BRANCH, index) for index in range(45)
    ] + [(CHANGE_BRANCH, index) for index in range(24)]
    for index, address in SCAN_RECEIVE.items():
        if index <= 44:
            assert book.asked[index] == address, index
    for index, address in SCAN_CHANGE.items():
        if index <= 23:
            assert book.asked[45 + index] == address, index
    assert len(set(book.asked)) == len(book.asked), "no address is asked about twice"


async def test_every_scanned_address_carries_its_own_figures() -> None:
    book = criterion_three_book()

    result = await scan(book)
    by_position = {(address.branch, address.index): address for address in result.addresses}

    assert by_position[(RECEIVE_BRANCH, 0)].confirmed == 0
    assert by_position[(RECEIVE_BRANCH, 5)].confirmed == 120_000
    assert by_position[(RECEIVE_BRANCH, 24)].confirmed == 7_000
    assert by_position[(RECEIVE_BRANCH, 24)].pending == 3_000
    assert by_position[(CHANGE_BRANCH, 0)].confirmed == 6_000
    assert by_position[(CHANGE_BRANCH, 3)].confirmed == 25_000
    others = [
        address
        for position, address in by_position.items()
        if position not in {(0, 0), (0, 5), (0, 24), (1, 0), (1, 3)}
    ]
    assert len(others) == 64
    assert all(address.confirmed == 0 and address.pending == 0 for address in others)
    assert sum(address.confirmed for address in result.addresses) == CRITERION_THREE_CONFIRMED


async def test_a_key_with_nothing_used_reads_twenty_per_branch() -> None:
    """The cheapest first scan there is: 40 requests. The published children are in it."""
    book = AddressBook()

    result = await scan(book)

    assert indices(result, RECEIVE_BRANCH) == list(range(GAP_LIMIT))
    assert indices(result, CHANGE_BRANCH) == list(range(GAP_LIMIT))
    assert len(book.asked) == 2 * GAP_LIMIT
    assert not any(address.used for address in result.addresses)
    by_position = {(address.branch, address.index): address.address for address in result.addresses}
    for child in BIP84_CHILDREN:
        assert by_position[(child.branch, child.child_index)] == child.address, child.id


async def test_used_comes_from_the_transaction_count_not_from_the_balance() -> None:
    """An emptied address is used; a balance with no transaction count is not (R5)."""
    book = AddressBook(
        holdings={
            SCAN_RECEIVE[0]: Holding(funded=0, chain_tx=3),
            SCAN_RECEIVE[1]: Holding(funded=5_000, chain_tx=0),
        }
    )

    result = await scan(book)
    receive = on_branch(result, RECEIVE_BRANCH)

    assert receive[0].used is True
    assert receive[1].used is False
    assert len(receive) == 1 + GAP_LIMIT, "only index 0 extends the gap"


async def test_a_transaction_only_in_the_mempool_counts_as_used() -> None:
    book = AddressBook(holdings={SCAN_RECEIVE[5]: Holding(mempool_funded=1_000, mempool_tx=1)})

    result = await scan(book)
    receive = on_branch(result, RECEIVE_BRANCH)

    assert receive[5].used is True
    assert receive[5].pending == 1_000
    assert len(receive) == 5 + 1 + GAP_LIMIT


async def test_an_instance_with_no_mempool_figures_reports_pending_as_none() -> None:
    book = AddressBook(
        holdings={SCAN_RECEIVE[0]: Holding(funded=1, chain_tx=1, mempool_funded=None)}
    )

    result = await scan(book)

    assert on_branch(result, RECEIVE_BRANCH)[0].pending is None
    assert on_branch(result, RECEIVE_BRANCH)[1].pending == 0


# --------------------------------------------------------------------------------------
# Criterion 4, the provider's half: derive only above what is persisted (R6)
# --------------------------------------------------------------------------------------


async def test_a_rescan_with_no_new_use_derives_nothing(derivation_spy: DerivationSpy) -> None:
    first = await scan(criterion_three_book())
    derivation_spy.calls.clear()
    book = criterion_three_book()

    second = await scan(book, known=known_from(first))

    assert derivation_spy.accounts() == [RECEIVE_BRANCH, CHANGE_BRANCH], "the two branch keys"
    assert derivation_spy.children(RECEIVE_BRANCH) == []
    assert derivation_spy.children(CHANGE_BRANCH) == []
    assert second == first
    assert book.asked == [address.address for address in first.addresses], "every one re-read"


async def test_the_first_scan_derives_each_index_once(derivation_spy: DerivationSpy) -> None:
    await scan(criterion_three_book())

    assert derivation_spy.accounts() == [RECEIVE_BRANCH, CHANGE_BRANCH]
    assert derivation_spy.children(RECEIVE_BRANCH) == list(range(45))
    assert derivation_spy.children(CHANGE_BRANCH) == list(range(24))


async def test_a_newly_used_address_derives_only_up_to_twenty_past_it(
    derivation_spy: DerivationSpy,
) -> None:
    """Criterion 4: receive 44 becomes used, so 45 to 64 are derived, and nothing else.

    Receive 46 is taken out of the book here, so that k + 20 is the whole answer; the next
    test keeps it in.
    """
    first = await scan(criterion_three_book())
    derivation_spy.calls.clear()
    book = criterion_three_book()
    book.holdings = {
        **{address: held for address, held in book.holdings.items() if address != SCAN_RECEIVE[46]},
        SCAN_RECEIVE[44]: Holding(funded=1_234, chain_tx=1),
    }

    second = await scan(book, known=known_from(first))

    assert derivation_spy.accounts() == [RECEIVE_BRANCH, CHANGE_BRANCH]
    assert derivation_spy.children(RECEIVE_BRANCH) == list(range(45, 65))
    assert derivation_spy.children(CHANGE_BRANCH) == []
    assert indices(second, RECEIVE_BRANCH) == list(range(65))
    assert len(book.asked) == 69 + 20
    assert SCAN_RECEIVE[64] in book.asked
    assert SCAN_RECEIVE[65] not in book.asked
    assert on_branch(second, RECEIVE_BRANCH)[44].used is True


async def test_a_newly_used_address_brings_the_next_one_into_reach(
    derivation_spy: DerivationSpy,
) -> None:
    """With 44 used, the fixture's receive 46 is within twenty, is found, and extends again."""
    first = await scan(criterion_three_book())
    derivation_spy.calls.clear()
    book = criterion_three_book()
    book.holdings = {**book.holdings, SCAN_RECEIVE[44]: Holding(funded=1_234, chain_tx=1)}

    second = await scan(book, known=known_from(first))

    assert derivation_spy.children(RECEIVE_BRANCH) == list(range(45, 67))
    receive = on_branch(second, RECEIVE_BRANCH)
    assert receive[46].address == SCAN_RECEIVE[46]
    assert receive[46].used is True
    assert receive[46].confirmed == 999_999
    assert SCAN_RECEIVE[65] in book.asked


async def test_a_persisted_used_flag_survives_a_vendor_that_now_says_unused(
    derivation_spy: DerivationSpy,
) -> None:
    """Once used, always used (R5): a pruned instance cannot shrink the scan."""
    first = await scan(criterion_three_book())
    derivation_spy.calls.clear()
    book = AddressBook()  # Every address now reports no transaction at all.

    second = await scan(book, known=known_from(first))

    assert [address.used for address in second.addresses] == [
        address.used for address in first.addresses
    ]
    assert derivation_spy.children(RECEIVE_BRANCH) == []
    assert len(book.asked) == 69


async def test_every_persisted_address_is_read_whatever_order_it_is_handed_in() -> None:
    first = await scan(criterion_three_book())
    book = criterion_three_book()

    second = await scan(book, known=list(reversed(known_from(first))))

    assert book.asked == [address.address for address in first.addresses]
    assert second == first


async def test_a_gap_in_the_persisted_indices_is_not_filled_in(
    derivation_spy: DerivationSpy,
) -> None:
    """Derivation starts above the highest persisted index; a missing one is not re-derived.

    A persisted set with a hole is what a skipped invalid index leaves behind (R5).
    """
    first = await scan(criterion_three_book())
    holed = [entry for entry in known_from(first) if (entry.branch, entry.index) != (0, 30)]
    derivation_spy.calls.clear()
    book = criterion_three_book()

    second = await scan(book, known=holed)

    assert derivation_spy.children(RECEIVE_BRANCH) == [45], "one more, for the uncounted hole"
    assert 30 not in indices(second, RECEIVE_BRANCH)
    assert indices(second, RECEIVE_BRANCH)[-1] == 45


# --------------------------------------------------------------------------------------
# R5: an index BIP32 gives no key is skipped, and the branch cap
# --------------------------------------------------------------------------------------


@pytest.fixture
def receive_index_thirty_has_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject `IL >= n` for receive child 30, and only for it.

    `derive_child` looks `hmac_sha512` up at call time (spec 031, R5), which is the seam.
    """
    real = extended_keys.hmac_sha512
    receive = extended_keys.derive_child(parse_extended_public_key(SCAN_KEY), RECEIVE_BRANCH)
    assert receive is not None
    target = receive.public_key + (30).to_bytes(4, "big")

    def injected(key: bytes, data: bytes) -> bytes:
        if key == receive.chain_code and data == target:
            return CURVE_ORDER.to_bytes(32, "big") + bytes(32)
        return real(key, data)

    monkeypatch.setattr(extended_keys, "hmac_sha512", injected)


@pytest.mark.usefixtures("receive_index_thirty_has_no_key")
async def test_an_index_with_no_key_is_skipped_and_not_counted_toward_the_gap() -> None:
    book = criterion_three_book()

    result = await scan(book)

    assert indices(result, RECEIVE_BRANCH) == [*range(30), *range(31, 46)]
    assert SCAN_RECEIVE[45] in book.asked, "the skipped index did not count toward the gap"
    assert SCAN_RECEIVE[46] not in book.asked
    assert indices(result, CHANGE_BRANCH) == list(range(24))
    assert len(book.asked) == 45 + 24


async def test_a_branch_with_no_key_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = extended_keys.hmac_sha512
    parsed = parse_extended_public_key(SCAN_KEY)

    def injected(key: bytes, data: bytes) -> bytes:
        if key == parsed.chain_code:
            return CURVE_ORDER.to_bytes(32, "big") + bytes(32)
        return real(key, data)

    monkeypatch.setattr(extended_keys, "hmac_sha512", injected)
    book = AddressBook()

    with pytest.raises(AddressInvalidError) as refused:
        await scan(book)

    assert refused.value.reason is AddressRejection.INVALID_PUBLIC_KEY
    assert book.asked == []


@pytest.fixture
def cap_of(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    """Lower the provider's cap for one test. The real number is pinned above."""

    def lower(cap: int) -> None:
        monkeypatch.setattr(bitcoin, "MAX_ADDRESSES_PER_BRANCH", cap)

    return lower


async def test_a_vendor_reporting_everything_used_stops_at_the_cap(
    cap_of: Callable[[int], None],
) -> None:
    cap_of(30)
    book = AddressBook(everything_used=True)

    with pytest.raises(ProviderResponseError) as refused:
        await scan(book)

    assert str(refused.value) == BRANCH_CAP_MESSAGE
    assert len(book.asked) == 30, "exactly the cap, on the receive branch, then the refusal"
    assert SCAN_RECEIVE[0] == book.asked[0]
    for value in (SCAN_KEY, *book.asked):
        assert value not in str(refused.value)


async def test_the_cap_counts_the_persisted_addresses_too(cap_of: Callable[[int], None]) -> None:
    first = await scan(criterion_three_book())
    cap_of(50)
    book = AddressBook(everything_used=True)

    with pytest.raises(ProviderResponseError, match="stopped"):
        await scan(book, known=known_from(first))

    assert len(book.asked) == 50, "45 persisted on receive, then 5 new, then the refusal"


async def test_a_branch_one_short_of_the_cap_completes(cap_of: Callable[[int], None]) -> None:
    """The boundary: 45 + 24 addresses fit a cap of 45 exactly."""
    cap_of(45)
    book = criterion_three_book()

    result = await scan(book)

    assert len(result.addresses) == 69


async def test_running_out_of_non_hardened_indices_is_the_same_refusal() -> None:
    """Reachable only from a persisted address at the top of the range."""
    book = AddressBook()
    top = KnownDerivedAddress(
        branch=RECEIVE_BRANCH, index=HARDENED_INDEX - 1, address=SCAN_RECEIVE[0], used=True
    )

    with pytest.raises(ProviderResponseError) as refused:
        await scan(book, known=[top])

    assert str(refused.value) == BRANCH_CAP_MESSAGE
    assert book.asked == [SCAN_RECEIVE[0]]


# --------------------------------------------------------------------------------------
# Refusals, every one before the first request
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", [SCAN_KEY, BIP32_TV1_M, BIP49_ACCOUNT_UPUB], ids=["vpub", "tpub", "upub"]
)
async def test_a_test_network_key_on_a_mainnet_instance_is_refused_first(key: str) -> None:
    """R3: the family is checked against the configured network before anything is read."""
    book = AddressBook()

    with pytest.raises(AddressInvalidError) as refused:
        await scan(book, key, network="mainnet")

    assert refused.value.reason is AddressRejection.WRONG_NETWORK
    assert book.asked == []


@pytest.mark.parametrize("network", ["testnet", "regtest"])
async def test_a_test_network_key_is_scanned_on_both_test_networks(network: str) -> None:
    """The control for the refusal above: the family admits both."""
    book = AddressBook()

    result = await scan(book, network=network)

    assert len(result.addresses) == 2 * GAP_LIMIT


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        (short("tprv"), AddressRejection.PRIVATE_KEY),
        (DERIVED_VPUB_MULTISIG, AddressRejection.EXTENDED_KEY_MULTISIG),
        (SYNTHETIC_TPUB, AddressRejection.INVALID_PUBLIC_KEY),
        (SCAN_KEY[:-1] + ("1" if SCAN_KEY[-1] != "1" else "2"), AddressRejection.BAD_CHECKSUM),
        (short("vpub"), AddressRejection.MALFORMED),
    ],
    ids=["private", "multisig", "not a point", "checksum", "short"],
)
async def test_the_parsers_refusals_pass_through_before_any_request(
    key: str, reason: AddressRejection
) -> None:
    book = AddressBook()

    with pytest.raises(AddressInvalidError) as refused:
        await scan(book, key)

    assert refused.value.reason is reason
    assert book.asked == []


@pytest.mark.parametrize(
    "known",
    [
        [KnownDerivedAddress(2, 0, SCAN_RECEIVE[0], used=False)],
        [KnownDerivedAddress(-1, 0, SCAN_RECEIVE[0], used=False)],
        [KnownDerivedAddress(0, -1, SCAN_RECEIVE[0], used=False)],
        [KnownDerivedAddress(0, HARDENED_INDEX, SCAN_RECEIVE[0], used=False)],
        [
            KnownDerivedAddress(0, 1, SCAN_RECEIVE[1], used=False),
            KnownDerivedAddress(0, 1, SCAN_RECEIVE[5], used=True),
        ],
    ],
    ids=["branch two", "branch minus one", "index minus one", "hardened index", "duplicate"],
)
async def test_a_persisted_position_outside_the_tree_is_a_programming_error(
    known: list[KnownDerivedAddress],
) -> None:
    book = AddressBook()

    with pytest.raises(ValueError, match="persisted derived address") as refused:
        await scan(book, known=known)

    assert not isinstance(refused.value, AddressInvalidError)
    assert book.asked == []
    for entry in known:
        assert entry.address not in str(refused.value)


@pytest.mark.parametrize(
    ("address", "network", "reason"),
    [
        (BIP173_TESTNET_P2WPKH_UPPERCASE, "testnet", AddressRejection.MALFORMED),
        (CORE_REGTEST_P2WPKH, "testnet", AddressRejection.WRONG_NETWORK),
        (SCAN_RECEIVE[0], "regtest", AddressRejection.WRONG_NETWORK),
        (KASPA_TESTNET_V0, "testnet", None),
        ("not-an-address", "testnet", None),
    ],
    ids=["not canonical", "regtest on testnet", "testnet on regtest", "kaspa", "garbage"],
)
async def test_a_persisted_address_is_validated_before_it_goes_into_a_url(
    address: str, network: str, reason: AddressRejection | None
) -> None:
    """Out of a database column and into a URL path: the same rule `fetch_balances` keeps."""
    book = AddressBook()
    known = [KnownDerivedAddress(RECEIVE_BRANCH, 0, address, used=True)]

    with pytest.raises(AddressInvalidError) as refused:
        await scan(book, known=known, network=network)

    if reason is not None:
        assert refused.value.reason is reason
    assert book.asked == []
    assert address not in str(refused.value)


async def test_a_persisted_address_that_validates_is_read_as_given() -> None:
    """The control: a valid canonical testnet address at a persisted position is read."""
    book = AddressBook()
    known = [KnownDerivedAddress(CHANGE_BRANCH, 7, BIP173_TESTNET_P2WPKH, used=False)]

    result = await scan(book, known=known)

    assert BIP173_TESTNET_P2WPKH in book.asked
    change = on_branch(result, CHANGE_BRANCH)
    assert (change[0].index, change[0].address) == (7, BIP173_TESTNET_P2WPKH)
    assert [address.index for address in change] == [7, *range(8, 8 + GAP_LIMIT - 1)]


# --------------------------------------------------------------------------------------
# R3 on regtest: bech32 carries `bcrt`, base58 is testnet's bytes
# --------------------------------------------------------------------------------------


async def test_a_vpub_on_regtest_derives_bcrt_addresses_and_accepts_them_back() -> None:
    book = AddressBook(holdings={ENCODINGS[0].regtest_p2wpkh: Holding(funded=1, chain_tx=1)})

    first = await scan(book, network="regtest")

    assert all(address.address.startswith("bcrt1q") for address in first.addresses)
    receive = on_branch(first, RECEIVE_BRANCH)
    assert receive[0].address == ENCODINGS[0].regtest_p2wpkh
    assert receive[1].address == ENCODINGS[1].regtest_p2wpkh
    assert on_branch(first, CHANGE_BRANCH)[0].address == ENCODINGS[2].regtest_p2wpkh

    second = await scan(AddressBook(), known=known_from(first), network="regtest")
    assert [address.address for address in second.addresses] == [
        address.address for address in first.addresses
    ]


@pytest.mark.parametrize("network", ["testnet", "regtest"])
async def test_a_upub_derives_nested_segwit_with_the_same_bytes_on_both(network: str) -> None:
    """BIP-49's published receive address, read under either test network, and read back."""
    first = await scan(AddressBook(), BIP49_ACCOUNT_UPUB, network=network)

    assert on_branch(first, RECEIVE_BRANCH)[0].address == BIP49_RECEIVE_0_ADDRESS
    assert all(address.address.startswith("2") for address in first.addresses)
    second = await scan(AddressBook(), BIP49_ACCOUNT_UPUB, known_from(first), network=network)
    assert second == first


@pytest.mark.parametrize("network", ["testnet", "regtest"])
async def test_a_tpub_derives_legacy_addresses_on_both(network: str) -> None:
    first = await scan(AddressBook(), BIP32_TV1_M, network=network)

    for child in TV1_MASTER_CHILDREN_P2PKH:
        assert on_branch(first, child.branch)[child.child_index].address == child.address
    second = await scan(AddressBook(), BIP32_TV1_M, known_from(first), network=network)
    assert second == first


# --------------------------------------------------------------------------------------
# Criterion 6: every request goes through the host limiter, retries included
# --------------------------------------------------------------------------------------


@dataclass
class PacedHost:
    """A real `HostRateLimiter` whose clock moves only when the limiter sleeps."""

    clock: FakeClock = field(default_factory=FakeClock)
    limiter_slept_ms: list[int] = field(default_factory=list)
    transport_slept_ms: list[int] = field(default_factory=list)

    async def limiter_sleep(self, milliseconds: int) -> None:
        self.limiter_slept_ms.append(milliseconds)
        self.clock.advance(milliseconds)
        await anyio.lowlevel.checkpoint()

    async def transport_sleep(self, milliseconds: int) -> None:
        """The retry backoff. Recorded, and deliberately not moving the clock."""
        self.transport_slept_ms.append(milliseconds)
        await anyio.lowlevel.checkpoint()

    def limiter(self) -> HostRateLimiter:
        return HostRateLimiter(
            min_interval_ms=DEFAULT_MIN_HOST_INTERVAL_MS, clock=self.clock, sleep=self.limiter_sleep
        )


def spacing(arrivals: Sequence[int]) -> Iterator[int]:
    return (later - earlier for earlier, later in pairwise(arrivals))


async def test_every_derived_address_request_waits_its_turn_at_the_limiter() -> None:
    """Forty requests, one second apart by the injected clock, and not one early."""
    paced = PacedHost()
    book = AddressBook(clock=paced.clock)

    await scan(book, limiter=paced.limiter(), transport_sleep=paced.transport_sleep)

    assert DEFAULT_MIN_HOST_INTERVAL_MS == 1000
    assert len(book.asked) == 40
    assert book.arrivals_ms == [index * 1000 for index in range(40)]
    assert paced.limiter_slept_ms == [1000] * 39


async def test_a_retried_request_acquires_the_limiter_again() -> None:
    """A 503 and a 429 on two addresses: 42 requests, every one of them paced."""
    paced = PacedHost()
    book = AddressBook(
        clock=paced.clock,
        failures={
            SCAN_RECEIVE[5]: [httpx.Response(503)],
            SCAN_CHANGE[3]: [httpx.Response(429, headers={"Retry-After": "0"})],
        },
    )

    result = await scan(book, limiter=paced.limiter(), transport_sleep=paced.transport_sleep)

    assert len(result.addresses) == 40
    assert len(book.asked) == 42
    assert book.asked.count(SCAN_RECEIVE[5]) == 2
    assert book.asked.count(SCAN_CHANGE[3]) == 2
    assert book.arrivals_ms == [index * 1000 for index in range(42)]
    assert list(spacing(book.arrivals_ms)) == [1000] * 41
    assert paced.limiter_slept_ms == [1000] * 41, "one acquire per request, retries included"


async def test_the_whole_criterion_three_scan_is_paced() -> None:
    paced = PacedHost()
    book = criterion_three_book(clock=paced.clock)

    await scan(book, limiter=paced.limiter(), transport_sleep=paced.transport_sleep)

    assert len(book.asked) == 69
    assert min(spacing(book.arrivals_ms)) >= DEFAULT_MIN_HOST_INTERVAL_MS
    assert book.arrivals_ms[-1] == 68 * 1000


async def test_failover_is_sticky_for_the_whole_scan_both_branches() -> None:
    """The primary is down: asked once, then every read of both branches goes to the fallback."""
    book = AddressBook(down_hosts=frozenset({PRIMARY_HOST}))

    result = await scan(book, max_attempts=1)

    assert len(result.addresses) == 40
    assert book.hosts[0] == PRIMARY_HOST
    assert book.hosts[1:] == [FALLBACK_HOST] * 40
    assert book.asked[0] == book.asked[1] == SCAN_RECEIVE[0]


async def test_failover_is_sticky_through_the_persisted_addresses_of_a_rescan() -> None:
    """The primary is down on a rescan: asked once, then every persisted read is the fallback's.

    The test above is a first scan, where every read is an extension. A rescan reads its
    persisted addresses first, through a call of its own, and that call must carry the
    instance that answered forward too. Otherwise every persisted address asks the dead
    primary first, on every sync: a wasted request and a wasted limiter slot each.
    """
    first = await scan(criterion_three_book())
    book = criterion_three_book()
    book.down_hosts = frozenset({PRIMARY_HOST})

    second = await scan(book, known=known_from(first), max_attempts=1)

    assert second == first
    assert len(first.addresses) == 69
    assert book.hosts[0] == PRIMARY_HOST
    assert book.hosts[1:] == [FALLBACK_HOST] * 69
    assert book.asked[0] == book.asked[1] == SCAN_RECEIVE[0]


# --------------------------------------------------------------------------------------
# `tx_count` in the parser
# --------------------------------------------------------------------------------------


def parsed_body(**holding: int | None) -> AddressStats:
    return parse_address_response(
        esplora_body(BIP173_TESTNET_P2WPKH, Holding(**holding)),  # type: ignore[arg-type]
        BIP173_TESTNET_P2WPKH,
    )


@pytest.mark.parametrize(
    ("chain_tx", "mempool_tx", "used"),
    [(0, 0, False), (1, 0, True), (0, 1, True), (5, 2, True), (10**9, 0, True)],
)
def test_used_is_either_count_above_zero(chain_tx: int, mempool_tx: int, used: bool) -> None:
    assert parsed_body(chain_tx=chain_tx, mempool_tx=mempool_tx).used is used


@pytest.mark.parametrize("chain_tx", [0, 1])
def test_with_no_mempool_figures_used_is_the_confirmed_count_alone(chain_tx: int) -> None:
    stats = parsed_body(chain_tx=chain_tx, mempool_funded=None)

    assert stats.used is bool(chain_tx)
    assert stats.pending is None


def body_with(stats: str, value: object, *, drop: bool = False) -> str:
    body = json.loads(esplora_body(BIP173_TESTNET_P2WPKH, Holding(chain_tx=1, mempool_tx=1)))
    if drop:
        del body[stats]["tx_count"]
    else:
        body[stats]["tx_count"] = value
    return json.dumps(body)


@pytest.mark.parametrize("stats", ["chain_stats", "mempool_stats"])
def test_a_missing_transaction_count_is_refused(stats: str) -> None:
    with pytest.raises(ProviderResponseError) as refused:
        parse_address_response(body_with(stats, None, drop=True), BIP173_TESTNET_P2WPKH)

    assert f"{stats}.tx_count" in str(refused.value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="null"),
        pytest.param("4242", id="a string of digits"),
        pytest.param(1.0, id="a float that is a whole number"),
        pytest.param(True, id="true, an int subclass"),
        pytest.param(False, id="false, which would read as zero"),
        pytest.param([], id="an array"),
        pytest.param({}, id="an object"),
    ],
)
@pytest.mark.parametrize("stats", ["chain_stats", "mempool_stats"])
def test_a_transaction_count_that_is_not_a_whole_number_is_refused(
    stats: str, value: object
) -> None:
    with pytest.raises(ProviderResponseError) as refused:
        parse_address_response(body_with(stats, value), BIP173_TESTNET_P2WPKH)

    message = str(refused.value)
    assert f"{stats}.tx_count" in message
    assert "4242" not in message, "the message names the field and the type, never the value"


@pytest.mark.parametrize("stats", ["chain_stats", "mempool_stats"])
def test_a_negative_transaction_count_is_refused(stats: str) -> None:
    with pytest.raises(ProviderResponseError) as refused:
        parse_address_response(body_with(stats, -4242), BIP173_TESTNET_P2WPKH)

    message = str(refused.value)
    assert f"{stats}.tx_count" in message
    assert "negative" in message
    assert "4242" not in message


def test_a_bad_mempool_count_is_refused_even_when_the_confirmed_count_says_used() -> None:
    """A short-circuit `or` would skip the mempool check for every used address."""
    body = json.loads(esplora_body(BIP173_TESTNET_P2WPKH, Holding(chain_tx=3)))
    body["mempool_stats"]["tx_count"] = "1"

    with pytest.raises(ProviderResponseError, match=r"mempool_stats\.tx_count"):
        parse_address_response(json.dumps(body), BIP173_TESTNET_P2WPKH)


def test_a_bad_sum_is_still_refused_for_the_sum_and_not_for_the_count() -> None:
    """The count is read after the two sums, so every existing refusal is unchanged."""
    body = json.loads(esplora_body(BIP173_TESTNET_P2WPKH, Holding(chain_tx=1)))
    body["chain_stats"]["funded_txo_sum"] = "100"
    body["chain_stats"]["tx_count"] = "1"

    with pytest.raises(ProviderResponseError) as refused:
        parse_address_response(json.dumps(body), BIP173_TESTNET_P2WPKH)

    assert "funded_txo_sum" in str(refused.value)
    assert "tx_count" not in str(refused.value)


async def test_a_refused_count_fails_the_scan() -> None:
    """The scan reads through the same parser: a vendor's bad count is not an unused address."""
    bad = json.loads(esplora_body(SCAN_RECEIVE[5], Holding()))
    bad["chain_stats"]["tx_count"] = -1
    book = AddressBook(failures={SCAN_RECEIVE[5]: [httpx.Response(200, json=bad)] * 3})

    with pytest.raises(ProviderResponseError, match="negative"):
        await scan(book, max_attempts=1)

    assert book.asked[-1] == SCAN_RECEIVE[5]
