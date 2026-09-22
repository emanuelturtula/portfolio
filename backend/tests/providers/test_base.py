"""Criteria 1 and 2: what a provider answers, and what it declares it can be asked.

`align_balances` is the only place the "one result per requested address, in order"
invariant is decided, so it is the only place it has to be tested -- and it is pure, so it
is tested the way `domain/` is tested: no fixtures, no I/O, and a property over generated
input as well as the examples.

The property matters here specifically. The cases somebody thinks to write down are the
ones they already had in mind; the invariant has to hold for *any* sequence of addresses a
wallet table can produce, including the ones with a single entry, with fifty, and with the
address that happens to sort first.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from portfolio.domain.chains import ChainKey
from portfolio.domain.money import from_base_units
from portfolio.providers.base import (
    AddressBalance,
    ChainCapabilities,
    align_balances,
    chunk_addresses,
)
from portfolio.providers.errors import ProviderResponseError
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    CORE_SIGNET_P2PKH,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V1_KEY,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

BITCOIN_DECIMALS: Final = 8

#: Three distinct testnet addresses, which is the smallest number that can tell "in order"
#: apart from "sorted" and from "reversed". Two cannot: a reversal of two is also a sort.
THREE_ADDRESSES: Final[tuple[str, ...]] = (
    CORE_SIGNET_P2PKH,
    BIP173_TESTNET_P2WPKH,
    KASPA_TESTNET_V0,
)

#: A whole coin, in satoshis. Chosen so the decimal conversion is not the identity and a
#: dropped or doubled exponent is visible in the assertion rather than plausible.
ONE_COIN_IN_BASE_UNITS: Final = 100_000_000


# --------------------------------------------------------------------------------------
# Criterion 1: one result per requested address, in the order asked
# --------------------------------------------------------------------------------------


def test_every_requested_address_gets_exactly_one_result_in_order() -> None:
    """Same length, same order, each entry carrying the address it is about.

    Asserted as the whole tuple rather than as a length and a set. A length assertion
    passes for a reordering, and a set assertion passes for a permutation -- and a
    permutation is the failure that matters, because it reports one wallet's balance
    against another wallet's address and every total stays plausible.
    """
    found = {
        CORE_SIGNET_P2PKH: 1,
        BIP173_TESTNET_P2WPKH: 2,
        KASPA_TESTNET_V0: 3,
    }

    aligned = align_balances(THREE_ADDRESSES, found, decimals=BITCOIN_DECIMALS)

    assert tuple(balance.address for balance in aligned) == THREE_ADDRESSES
    assert tuple(balance.confirmed for balance in aligned) == (1, 2, 3)
    assert all(balance.decimals == BITCOIN_DECIMALS for balance in aligned)


def test_the_order_is_the_requested_order_and_not_the_sorted_one() -> None:
    """The control for the test above: the requested order is deliberately not sorted.

    Without this, a `sorted()` in the implementation would satisfy the assertion above for
    any input whose natural order happened to match. Here it cannot.
    """
    requested = tuple(sorted(THREE_ADDRESSES, reverse=True))
    assert requested != tuple(sorted(requested)), "the fixture no longer distinguishes the two"

    aligned = align_balances(requested, {}, decimals=BITCOIN_DECIMALS)

    assert tuple(balance.address for balance in aligned) == requested


def test_an_address_with_no_history_is_zero_not_an_omission() -> None:
    """An address nobody has ever paid is a zero balance. That is what it means on chain.

    Omitting it would make "we could not answer" and "the answer is nothing" the same
    value at the caller, and a wallet that silently disappeared from a total is a wrong
    total that never says so.
    """
    found = {BIP173_TESTNET_P2WPKH: ONE_COIN_IN_BASE_UNITS}

    aligned = align_balances(THREE_ADDRESSES, found, decimals=BITCOIN_DECIMALS)

    by_address = {balance.address: balance.confirmed for balance in aligned}
    assert by_address == {
        CORE_SIGNET_P2PKH: 0,
        BIP173_TESTNET_P2WPKH: ONE_COIN_IN_BASE_UNITS,
        KASPA_TESTNET_V0: 0,
    }
    assert len(aligned) == len(THREE_ADDRESSES)


def test_an_answer_about_an_address_we_did_not_ask_about_is_refused() -> None:
    """A batch API answering about something we never asked about is a correlation bug.

    Dropping the extra entry silently would hide a provider that has mixed up two
    requests, behind a total that still looks plausible. The only safe reading of an
    unrequested address is that the response cannot be trusted at all.
    """
    found = {
        BIP173_TESTNET_P2WPKH: ONE_COIN_IN_BASE_UNITS,
        KASPA_TESTNET_V1_KEY: 7,  # never requested
    }

    with pytest.raises(ProviderResponseError) as caught:
        align_balances(THREE_ADDRESSES, found, decimals=BITCOIN_DECIMALS)

    # The refusal has to be about the unrequested address without quoting it: an error
    # message is a string that reaches a log, and rule 3 does not stop applying because
    # the provider misbehaved.
    assert KASPA_TESTNET_V1_KEY not in str(caught.value)


def test_a_negative_base_unit_count_is_refused() -> None:
    """No chain has a negative balance, so a negative is a parse error wearing a number.

    Accepting one would let a mis-signed integer subtract from the portfolio total.
    """
    found = {BIP173_TESTNET_P2WPKH: -1}

    with pytest.raises(ProviderResponseError):
        align_balances(THREE_ADDRESSES, found, decimals=BITCOIN_DECIMALS)


def test_zero_is_not_refused_alongside_the_negative() -> None:
    """The boundary. `confirmed < 0` and `confirmed <= 0` differ on exactly this input."""
    found = {BIP173_TESTNET_P2WPKH: 0}

    aligned = align_balances(THREE_ADDRESSES, found, decimals=BITCOIN_DECIMALS)

    assert aligned[1].confirmed == 0


def test_the_same_address_requested_twice_is_refused() -> None:
    """A `ValueError`, not a `ProviderResponseError`: the caller is wrong, not the server.

    The distinction is the whole reason there are two error types. A provider error means
    "retry or report the API"; a `ValueError` means "this code asked a nonsensical
    question". Collapsing the two sends an operator to look at the wrong system.
    """
    requested = (BIP173_TESTNET_P2WPKH, BIP173_TESTNET_P2WPKH)

    with pytest.raises(ValueError, match=r"distinct") as caught:
        align_balances(requested, {}, decimals=BITCOIN_DECIMALS)

    assert not isinstance(caught.value, ProviderResponseError)


def test_no_addresses_is_no_results_rather_than_an_error() -> None:
    """The empty page. A wallet table with nothing in it is an ordinary state.

    Raising here would make "the user has added no wallets yet" an error path, and the
    first sync after sign-up would report a failure.
    """
    assert tuple(align_balances((), {}, decimals=BITCOIN_DECIMALS)) == ()


def test_one_address_is_one_result() -> None:
    """The single page. The `n == 1` case is where an off-by-one in a chunker shows up."""
    aligned = align_balances(
        (BIP173_TESTNET_P2WPKH,), {BIP173_TESTNET_P2WPKH: 5}, decimals=BITCOIN_DECIMALS
    )

    assert tuple(aligned) == (
        AddressBalance(address=BIP173_TESTNET_P2WPKH, confirmed=5, decimals=BITCOIN_DECIMALS),
    )


@given(
    addresses=st.lists(
        st.sampled_from((*THREE_ADDRESSES, KASPA_TESTNET_V1_KEY)),
        min_size=0,
        max_size=4,
        unique=True,
    ),
    amounts=st.lists(st.integers(min_value=0, max_value=2**63 - 1), min_size=0, max_size=4),
)
def test_alignment_holds_for_any_request_and_any_subset_of_answers(
    addresses: list[str], amounts: list[int]
) -> None:
    """The invariant, over generated input rather than over the three cases above.

    Three properties, each of which a plausible implementation gets wrong on its own:
    the result is as long as the request, it is in the request's order, and every amount
    that came back is reported against the address it came back for.
    """
    found = dict(zip(addresses, amounts, strict=False))

    aligned = align_balances(addresses, found, decimals=BITCOIN_DECIMALS)

    assert len(aligned) == len(addresses)
    assert [balance.address for balance in aligned] == addresses
    for balance in aligned:
        assert balance.confirmed == found.get(balance.address, 0)


# --------------------------------------------------------------------------------------
# Criterion 1: the balance converts through the one rule that owns conversion
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("confirmed", "decimals", "expected"),
    [
        pytest.param(ONE_COIN_IN_BASE_UNITS, BITCOIN_DECIMALS, "1", id="one whole coin"),
        pytest.param(1, BITCOIN_DECIMALS, "0.00000001", id="one satoshi"),
        pytest.param(0, BITCOIN_DECIMALS, "0", id="nothing"),
        pytest.param(2_100_000_000_000_000, BITCOIN_DECIMALS, "21000000", id="every coin"),
        pytest.param(123, 0, "123", id="a chain with no fractional part"),
    ],
)
def test_amount_converts_through_the_domain_rule(
    confirmed: int, decimals: int, expected: str
) -> None:
    """The value is asserted, not merely the call.

    `assert balance.amount() == from_base_units(...)` alone would pass for an `amount()`
    that returned the wrong thing in exactly the way `from_base_units` returns it -- which
    is to say it would pass for a copy of the bug. So the expected decimal is written out
    by hand, and the agreement with the domain rule is asserted as well.
    """
    balance = AddressBalance(address=BIP173_TESTNET_P2WPKH, confirmed=confirmed, decimals=decimals)

    assert balance.amount() == Decimal(expected)
    assert balance.amount() == from_base_units(confirmed, decimals)
    # The exponent survives, so a `Decimal("1")` is not silently a `Decimal("1.00000000")`
    # or the other way round -- `==` on Decimal ignores trailing zeros and would not say.
    assert str(balance.amount()) == str(from_base_units(confirmed, decimals))


def test_a_balance_is_frozen() -> None:
    """A snapshot that a caller can edit is a snapshot of nothing in particular."""
    balance = AddressBalance(address=BIP173_TESTNET_P2WPKH, confirmed=1, decimals=8)

    with pytest.raises((AttributeError, TypeError)):
        balance.confirmed = 2  # type: ignore[misc]


def test_a_balance_carries_its_own_exponent() -> None:
    """`decimals` is on the balance as well as on the capabilities, deliberately.

    A snapshot that outlives the provider instance has to be interpretable without it, and
    an integer with no exponent beside it is a number nobody can turn back into money.
    """
    balance = AddressBalance(address=BIP173_TESTNET_P2WPKH, confirmed=1, decimals=8)

    assert balance.decimals == BITCOIN_DECIMALS


def test_there_is_no_pending_field() -> None:
    """Pinned, because the spec rules it out with a reason and #7 may be tempted.

    A field one provider always sets to zero makes zero ambiguous between "nothing
    pending" and "this chain cannot tell you". Adding it needs something that expresses
    the second, which is a decision with a caller behind it -- so it fails this test first
    and gets made on purpose.
    """
    fields = set(AddressBalance.__dataclass_fields__)

    assert fields == {"address", "confirmed", "decimals"}


# --------------------------------------------------------------------------------------
# Criterion 2: batching is a declared size, and something consumes the declaration
# --------------------------------------------------------------------------------------


def test_capabilities_declare_batching_as_a_size() -> None:
    """`can_batch` is derived from the integer and is not a field of its own.

    The boolean is derivable from the integer; the integer is not derivable from the
    boolean. Storing both is storing the same fact twice, and two copies of one fact is
    how they come to disagree.
    """
    batching = ChainCapabilities(chain_key=ChainKey.KASPA, decimals=8, max_addresses_per_call=10)
    single = ChainCapabilities(chain_key=ChainKey.BITCOIN, decimals=8, max_addresses_per_call=1)

    assert batching.can_batch is True
    assert single.can_batch is False
    assert "can_batch" not in ChainCapabilities.__dataclass_fields__
    assert set(ChainCapabilities.__dataclass_fields__) == {
        "chain_key",
        "decimals",
        "max_addresses_per_call",
    }


@pytest.mark.parametrize("size", [0, -1, -100])
def test_capabilities_refuse_a_call_size_below_one(size: int) -> None:
    """A provider that can make no calls at all is a configuration error, not a zero.

    Left unchecked it becomes an infinite loop or an empty result in the chunker, both of
    which present as "the sync does nothing" with no error anywhere.
    """
    with pytest.raises(ValueError, match=r"max_addresses_per_call"):
        ChainCapabilities(chain_key=ChainKey.BITCOIN, decimals=8, max_addresses_per_call=size)


@pytest.mark.parametrize("decimals", [-1, -8])
def test_capabilities_refuse_a_negative_exponent(decimals: int) -> None:
    """`from_base_units` raises on a negative exponent, so this is where it is caught."""
    with pytest.raises(ValueError, match=r"decimals"):
        ChainCapabilities(chain_key=ChainKey.BITCOIN, decimals=decimals, max_addresses_per_call=1)


@pytest.mark.parametrize(
    ("count", "size"),
    [
        pytest.param(0, 3, id="no addresses"),
        pytest.param(1, 3, id="fewer than one call"),
        pytest.param(3, 3, id="exactly one call"),
        pytest.param(4, 3, id="one over"),
        pytest.param(9, 3, id="exactly three calls"),
        pytest.param(7, 1, id="a provider that cannot batch"),
        pytest.param(5, 100, id="a ceiling nobody reaches"),
    ],
)
def test_chunk_addresses_never_exceeds_the_declared_call_size(count: int, size: int) -> None:
    """The capability is consumed, not decorative -- and every chunk is a legal call.

    Three properties at once: no chunk is larger than the provider said it can take, no
    chunk is empty (an empty call is a request for nothing that still costs a round trip
    and still counts against a rate limit), and the concatenation is the input in order
    with nothing dropped and nothing repeated.
    """
    addresses = tuple(f"tb1q{index:038d}" for index in range(count))
    capabilities = ChainCapabilities(
        chain_key=ChainKey.BITCOIN, decimals=8, max_addresses_per_call=size
    )

    chunks: list[Sequence[str]] = [
        tuple(chunk) for chunk in chunk_addresses(addresses, capabilities)
    ]

    assert all(len(chunk) <= size for chunk in chunks), chunks
    assert all(len(chunk) > 0 for chunk in chunks), chunks
    assert tuple(address for chunk in chunks for address in chunk) == addresses
    expected_calls = -(-count // size)  # ceiling division, without a float in sight
    assert len(chunks) == expected_calls


def test_chunking_a_provider_that_cannot_batch_makes_one_call_per_address() -> None:
    """Esplora's shape, asserted as the settled value rather than as a size bound.

    A chunker that returned everything in one chunk would satisfy "no chunk exceeds the
    size" for `size=1` only by accident of the input length, so the chunks are written out.
    """
    addresses = (CORE_SIGNET_P2PKH, BIP173_TESTNET_P2WPKH, KASPA_TESTNET_V0)
    capabilities = ChainCapabilities(
        chain_key=ChainKey.BITCOIN, decimals=8, max_addresses_per_call=1
    )

    chunks = [tuple(chunk) for chunk in chunk_addresses(addresses, capabilities)]

    assert chunks == [
        (CORE_SIGNET_P2PKH,),
        (BIP173_TESTNET_P2WPKH,),
        (KASPA_TESTNET_V0,),
    ]


# --------------------------------------------------------------------------------------
# A base-unit count that is not an integer
# --------------------------------------------------------------------------------------
#
# The vendor's answer is what arrives here, parsed out of JSON. `json.loads` produces a
# `float` for `1.0e8` without being asked, and a provider that forwarded one would hand a
# float into a package where rule 2 bans the word -- and, worse, would do it with a value
# that looks entirely reasonable.


@pytest.mark.parametrize(
    "units",
    [
        pytest.param(1.0e8, id="a float in exponent form, which is what json.loads returns"),
        pytest.param(100000000.0, id="a whole float"),
        pytest.param(0.5, id="a fractional float"),
        pytest.param("100000000", id="a string of digits"),
        pytest.param(None, id="null, which is what a missing field decodes to"),
        pytest.param(Decimal("1"), id="a Decimal, which is right everywhere but here"),
    ],
)
def test_a_base_unit_count_that_is_not_an_integer_is_refused(units: object) -> None:
    """`ProviderResponseError`, not `TypeError`: it is the vendor's answer that is wrong.

    The distinction is the same one the duplicate-request case makes in the other
    direction. A `TypeError` says this code has a bug; a `ProviderResponseError` says the
    response cannot be trusted, which is what is true when a chain index answers with a
    float where its documentation promises an integer.
    """
    with pytest.raises(ProviderResponseError):
        align_balances(
            (BIP173_TESTNET_P2WPKH,),
            {BIP173_TESTNET_P2WPKH: units},  # type: ignore[dict-item]
            decimals=BITCOIN_DECIMALS,
        )


def test_a_bool_is_not_a_base_unit_count_even_though_it_is_an_int() -> None:
    """`True` is an `int` subclass, so a naive `isinstance` check lets it straight through.

    It would then be reported as a holding of one satoshi. That is the whole reason this
    case is written out separately rather than folded into the list above: it is the one
    input that a plausible implementation of the guard accepts, and `domain/money.py`
    rejects it at the same boundary for the same reason.

    Note the absence of a `type: ignore` below, which the other cases all need:
    `bool` is a subclass of `int`, so `mypy` accepts `{address: True}` as a
    `Mapping[str, int]` without complaint. The type checker cannot see this one
    either, which is precisely why the runtime guard has to.
    """
    with pytest.raises(ProviderResponseError):
        align_balances(
            (BIP173_TESTNET_P2WPKH,),
            {BIP173_TESTNET_P2WPKH: True},
            decimals=BITCOIN_DECIMALS,
        )


def test_the_refusal_names_the_type_and_never_the_address() -> None:
    """An error message is a string that reaches a log; rule 3 does not pause for a bug.

    The address vector is a deliberately distinctive one. Checking a short or common
    string against a sentence is how an assertion like this passes by accident -- be-6's
    own probe of this message matched because the test address was `"a"`.
    """
    with pytest.raises(ProviderResponseError) as caught:
        align_balances(
            (KASPA_TESTNET_V1_KEY,),
            {KASPA_TESTNET_V1_KEY: 1.5},  # type: ignore[dict-item]
            decimals=BITCOIN_DECIMALS,
        )

    message = str(caught.value)
    assert KASPA_TESTNET_V1_KEY not in message
    assert KASPA_TESTNET_V1_KEY[:20] not in message
    assert "float" in message


def test_an_ordinary_integer_is_still_accepted() -> None:
    """The control. A guard that refused everything would pass every test above."""
    aligned = align_balances(
        (BIP173_TESTNET_P2WPKH,),
        {BIP173_TESTNET_P2WPKH: ONE_COIN_IN_BASE_UNITS},
        decimals=BITCOIN_DECIMALS,
    )

    assert aligned[0].confirmed == ONE_COIN_IN_BASE_UNITS
