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

import decimal
import json
import sys
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
    decode_json,
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


# --------------------------------------------------------------------------------------
# Criterion 10 of #7: pending is signed, optional, and its None means something
# --------------------------------------------------------------------------------------
#
# #6 refused a `pending` field and named the condition on which it would be reasonable:
# the field *plus* a way to say "not answerable here", so that zero is never ambiguous
# between "nothing is pending" and "this chain cannot tell you". #7 meets that condition
# rather than overriding it, which is why the test that pinned the field's absence is
# replaced by tests that pin the condition instead of simply being deleted.


def test_the_balance_carries_a_pending_field_whose_none_is_a_statement() -> None:
    """The field set, pinned -- and `None` as the default rather than `0`.

    The default is the whole decision in one line. A provider that says nothing about the
    mempool -- Kaspa's REST balance endpoint exposes nothing of the kind -- produces
    `None`, which a caller can render as "unknown"; a default of `0` would make every
    Kaspa address indistinguishable from one with nothing pending, which is the ambiguity
    #6 refused the field over.
    """
    fields = set(AddressBalance.__dataclass_fields__)

    assert fields == {"address", "confirmed", "decimals", "pending"}
    assert AddressBalance(address=BIP173_TESTNET_P2WPKH, confirmed=0, decimals=8).pending is None


def test_a_provider_that_says_nothing_about_pending_reports_none() -> None:
    """`align_balances` with no `pending` mapping: every result carries `None`.

    This is the signature every provider written before #7 already calls, so the default
    has to be the safe one. A zero-filling default would silently convert "this chain
    cannot tell you" into "nothing is pending" for the entire Kaspa provider, at the one
    call nobody would think to re-read.
    """
    aligned = align_balances(THREE_ADDRESSES, {CORE_SIGNET_P2PKH: 5}, decimals=BITCOIN_DECIMALS)

    assert [balance.pending for balance in aligned] == [None, None, None]
    assert [balance.confirmed for balance in aligned] == [5, 0, 0]


def test_a_negative_pending_survives_alignment_and_a_negative_confirmed_does_not() -> None:
    """The asymmetry, in one test, because the two guards are one line apart in the code.

    `pending` is a **net mempool delta**, not a balance: an outgoing payment sitting in
    the mempool spends a confirmed output and funds nothing, so it reads negative, which
    is exactly right. A `confirmed` below zero is a chain that is not telling the truth.

    Asserted together rather than in two tests, because the failure being guarded against
    is a single guard applied to both mappings -- the tempting tidy-up -- and only an
    assertion that pins both directions at once catches it.
    """
    aligned = align_balances(
        (BIP173_TESTNET_P2WPKH,),
        {BIP173_TESTNET_P2WPKH: ONE_COIN_IN_BASE_UNITS},
        decimals=BITCOIN_DECIMALS,
        pending={BIP173_TESTNET_P2WPKH: -1_000},
    )

    assert aligned[0].pending == -1_000
    assert aligned[0].confirmed == ONE_COIN_IN_BASE_UNITS

    with pytest.raises(ProviderResponseError):
        align_balances(
            (BIP173_TESTNET_P2WPKH,),
            {BIP173_TESTNET_P2WPKH: -1},
            decimals=BITCOIN_DECIMALS,
            pending={BIP173_TESTNET_P2WPKH: 0},
        )


def test_an_address_missing_from_pending_is_unknown_rather_than_zero() -> None:
    """The asymmetry with `found`, which is the other half of what the field means.

    A requested address missing from `found` is a **zero** -- that is what an unused
    address holds on chain. A requested address missing from `pending` is **`None`** --
    the provider did not answer, and inventing a zero for it would be the same lie the
    field exists to avoid.

    Both in one assertion, over one call, so a zero-fill applied to both mappings fails.
    """
    aligned = align_balances(
        THREE_ADDRESSES,
        {CORE_SIGNET_P2PKH: 7},
        decimals=BITCOIN_DECIMALS,
        pending={CORE_SIGNET_P2PKH: 3},
    )
    by_address = {balance.address: balance for balance in aligned}

    assert by_address[CORE_SIGNET_P2PKH].pending == 3
    assert by_address[BIP173_TESTNET_P2WPKH].pending is None
    assert by_address[BIP173_TESTNET_P2WPKH].confirmed == 0


def test_a_pending_entry_for_an_address_nobody_asked_about_is_refused() -> None:
    """The correlation rule applies to both halves, because a batch correlates as a whole.

    A response that carried a mempool figure for an address we did not request is the same
    paging or caching mistake `found` already refuses -- and dropping it silently would
    hide it behind a total that still looks plausible.
    """
    with pytest.raises(ProviderResponseError):
        align_balances(
            (BIP173_TESTNET_P2WPKH,),
            {BIP173_TESTNET_P2WPKH: 1},
            decimals=BITCOIN_DECIMALS,
            pending={BIP173_TESTNET_P2WPKH: 1, KASPA_TESTNET_V1_KEY: 2},
        )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1.0, id="a float that is a whole number"),
        pytest.param(-0.5, id="a fractional float"),
        pytest.param(True, id="a bool, which is an int subclass"),
        pytest.param("1", id="a string of digits"),
    ],
)
def test_a_pending_value_that_is_not_a_whole_number_of_base_units_is_refused(
    value: object,
) -> None:
    """The same guard as `confirmed`, because the same `json.loads` produced both.

    `Mapping[str, int]` is a static claim and this boundary meets values `mypy` never saw.
    A float reaching `pending` lives inside `providers/`, where the AST ban in
    `tests/security/test_no_float.py` cannot see it -- it reads source, and this float has
    no literal. `True` is the row the annotation provably cannot catch: `bool` is a
    subtype of `int` and `Mapping` is covariant in its value.
    """
    with pytest.raises(ProviderResponseError):
        align_balances(
            (BIP173_TESTNET_P2WPKH,),
            {BIP173_TESTNET_P2WPKH: 1},
            decimals=BITCOIN_DECIMALS,
            pending={BIP173_TESTNET_P2WPKH: value},  # type: ignore[dict-item]
        )


def test_an_explicit_none_in_the_pending_mapping_is_unknown_and_not_a_refusal() -> None:
    """An entry whose value is `None` means the same as no entry at all.

    Worth its own test because the obvious reading is the other one -- `None` is not an
    `int`, so refuse it alongside the float and the string -- and that reading would make
    a provider's choice between `pending.pop(address)` and `pending[address] = None` the
    difference between a balance and a `ProviderResponseError`. Two spellings of "the
    chain did not say" have to mean the chain did not say.

    Asserted next to the type refusals above so the boundary between them is one place
    rather than two, and paired with the absent-key case so a change to either arm cannot
    silently diverge from the other.
    """
    absent = align_balances(
        (BIP173_TESTNET_P2WPKH,),
        {BIP173_TESTNET_P2WPKH: 1},
        decimals=BITCOIN_DECIMALS,
        pending={},
    )
    explicit = align_balances(
        (BIP173_TESTNET_P2WPKH,),
        {BIP173_TESTNET_P2WPKH: 1},
        decimals=BITCOIN_DECIMALS,
        pending={BIP173_TESTNET_P2WPKH: None},  # type: ignore[dict-item]
    )

    assert absent[0].pending is None
    assert explicit[0].pending is None


def test_a_pending_refusal_names_the_type_and_never_the_address() -> None:
    """The #44 rule, at the boundary the new mapping opened.

    An exception message ends up in a log, in a response body, or in a traceback, and the
    set of addresses this application watches *is* the owner's holdings. The type is the
    part anyone can act on; which address a vendor mangled is not.
    """
    with pytest.raises(ProviderResponseError) as caught:
        align_balances(
            (BIP173_TESTNET_P2WPKH,),
            {BIP173_TESTNET_P2WPKH: 1},
            decimals=BITCOIN_DECIMALS,
            pending={BIP173_TESTNET_P2WPKH: 1.5},  # type: ignore[dict-item]
        )

    assert "float" in str(caught.value)
    assert BIP173_TESTNET_P2WPKH not in str(caught.value)
    assert BIP173_TESTNET_P2WPKH[:20] not in str(caught.value)
    assert all(BIP173_TESTNET_P2WPKH not in str(argument) for argument in caught.value.args)


def test_spendable_is_the_sum_and_the_sign_is_what_makes_it_work() -> None:
    """Nothing in #7 computes spendable; #11 does, and it gets a sign that already works.

    Stated here because it is the reason the sign is not a detail. `confirmed + pending`
    is the whole calculation, and it only produces the right answer for an outgoing
    payment if the delta is allowed to be negative.
    """
    aligned = align_balances(
        (BIP173_TESTNET_P2WPKH, CORE_SIGNET_P2PKH),
        {BIP173_TESTNET_P2WPKH: ONE_COIN_IN_BASE_UNITS, CORE_SIGNET_P2PKH: ONE_COIN_IN_BASE_UNITS},
        decimals=BITCOIN_DECIMALS,
        pending={BIP173_TESTNET_P2WPKH: -25_000, CORE_SIGNET_P2PKH: 25_000},
    )

    spendable = [balance.confirmed + (balance.pending or 0) for balance in aligned]

    assert spendable == [ONE_COIN_IN_BASE_UNITS - 25_000, ONE_COIN_IN_BASE_UNITS + 25_000]


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


# --------------------------------------------------------------------------------------
# #9: a JSON number is a Decimal built from the vendor's digits, never a float
# --------------------------------------------------------------------------------------
#
# This is the shared decoder every provider's trust boundary goes through, and #9 is where
# it starts carrying money. Kraken and Coinbase send prices as JSON **strings**; the Kaspa
# node's `/info/price` sends `{"price": 0.04228645}` -- a JSON **number**. `json.loads`
# turns that into a `float` before a single line of this application runs, and by the time
# a parser could refuse it the digits the vendor sent are already gone.
#
# `parse_float=Decimal` is the whole fix, and it is fixed inside `decode_json` with no way
# to opt out: a parameter would let a provider ask for the float back.
#
# **Every expectation below is a literal string.** Building one by calling `decode_json`
# and comparing the result to itself would be the verifier sharing state with its subject,
# which is the failure this project has now catalogued seven times. The digits are written
# out by hand, and the companion test shows that plain `json.loads` produces something
# different from them -- which is what proves the hook is doing work rather than being
# present.

#: The measured Kaspa body, byte for byte, and the digits it carries. Public market data,
#: not an address and not a credential, so it is a fixture rule 3 permits in full.
KASPA_PRICE_BODY: Final = '{"price": 0.04228645}'
KASPA_PRICE_DIGITS: Final = "0.04228645"


def test_a_json_number_is_decoded_as_a_decimal_not_a_float() -> None:
    """The decoder's answer is a `Decimal` carrying exactly the characters in the body.

    Three assertions, and none of them is redundant:

    * the **type** is `Decimal`, because a `float` here is the bug;
    * `str()` is the literal from the body, so the *digits* agree and not merely the
      value -- `==` on a `Decimal` ignores a trailing zero and would not notice
      `0.042286450` either way;
    * and the value equals `Decimal(KASPA_PRICE_DIGITS)`, built from the same literal
      string the body contains, which is a constant this test typed out rather than a
      number the code under test produced.
    """
    decoded = decode_json(KASPA_PRICE_BODY)

    assert isinstance(decoded, dict)
    price = decoded["price"]

    assert isinstance(price, Decimal)
    assert str(price) == KASPA_PRICE_DIGITS
    assert price == Decimal(KASPA_PRICE_DIGITS)


def test_plain_json_loads_produces_something_different_from_those_digits() -> None:
    """The companion, and without it the test above proves nothing about the hook.

    If `decode_json` were still a bare `json.loads`, the assertion above would fail on the
    type -- but only because somebody chose to assert the type. This one shows what the
    unpatched decoder actually does to the same body: it produces a `float`, and the exact
    value of that float is not the number the vendor sent.

    `Decimal(0.04228645)` is
    `0.0422864500000000032020608387028914876282215118408203125`. Those twenty-odd extra
    digits are not a rounding display artefact; they are the value, and every multiplication
    by a quantity carries them forward into a portfolio total.
    """
    naive = json.loads(KASPA_PRICE_BODY)["price"]

    assert isinstance(naive, float)
    assert Decimal(naive) != Decimal(KASPA_PRICE_DIGITS)
    assert str(Decimal(naive)) != KASPA_PRICE_DIGITS
    # And the thing that makes it dangerous rather than merely wrong: it *prints* right.
    assert str(naive) == KASPA_PRICE_DIGITS


@pytest.mark.parametrize(
    "digits",
    [
        pytest.param("0.04228645", id="a sub-cent price, as the Kaspa node sends it"),
        pytest.param("86000.10000", id="trailing zeros a float would drop"),
        pytest.param("0.1", id="the value IEEE-754 cannot represent at all"),
        pytest.param("1e-8", id="exponent form, which a vendor is free to send"),
        pytest.param("123456789012345678901234567890.123456789", id="more digits than a double"),
        pytest.param("-0.5", id="negative, because a parser must not read the sign twice"),
    ],
)
def test_every_json_number_keeps_the_characters_the_vendor_sent(digits: str) -> None:
    """The property, over the shapes a vendor actually sends, not only the measured one.

    `86000.10000` is the row that matters most after the Kaspa one. A `float` round trip
    renders it `86000.1`, which is the *same number* and a different string -- and the
    string is what `NumericText` stores and what a diff of the database shows. The
    exponent form is the row that catches a parser reaching for `str()` on a float
    somewhere in the middle.
    """
    decoded = decode_json(f'{{"amount": {digits}}}')

    assert isinstance(decoded, dict)
    amount = decoded["amount"]

    assert isinstance(amount, Decimal)
    assert amount == Decimal(digits)
    assert str(amount) == str(Decimal(digits))


def test_a_json_integer_is_still_an_int_and_not_a_decimal() -> None:
    """`parse_int` is untouched, and that is what keeps both balance providers working.

    Bitcoin and Kaspa both count in integer base units, and both parsers refuse anything
    that is not an `int` -- `Decimal("100000000")` included. Turning every JSON number into
    a `Decimal` would have turned every balance in the application into a refusal, so the
    hook has to apply to *floats* and nothing else.

    The bool row is here because `json` has no bool number: `true` decodes to `True`, which
    is an `int` subclass, and a parser that only checked `isinstance(value, int)` would read
    it as one satoshi. That guard lives in `align_balances` and is tested above; this
    assertion pins that the decoder does not quietly change what reaches it.
    """
    decoded = decode_json('{"confirmed": 100000000, "flag": true, "nothing": null}')

    assert isinstance(decoded, dict)
    confirmed = decoded["confirmed"]

    assert confirmed == ONE_COIN_IN_BASE_UNITS
    # `type(...) is int` rather than a pair of `isinstance` checks. Two reasons: it is the
    # stronger claim, excluding `bool` as well as `Decimal`; and `isinstance(x, int)`
    # followed by `isinstance(x, Decimal)` narrows `x` to `int` and mypy then reports the
    # second line as unreachable, which is a correct static observation about an
    # assertion whose subject is a runtime value the annotation cannot see.
    assert type(confirmed) is int
    assert decoded["flag"] is True
    assert decoded["nothing"] is None


#: The deepest nesting worth building before concluding that this interpreter has no limit
#: to find. Reached by doubling, so the cost of a high ceiling is one more probe.
DEEPEST_NESTING_PROBED: Final = 1 << 17


def _longer_than_this_build_will_convert() -> str:
    """A JSON integer with more digits than CPython will turn into an `int`.

    The limit defaults to 4300, but `PYTHONINTMAXSTRDIGITS` and `-X int_max_str_digits` set
    it per process, so it is read rather than written down -- the same reason as the
    recursion limit below, arrived at by the same failure.
    """
    limit = sys.get_int_max_str_digits()
    if limit == 0:
        message = "This interpreter has no integer string conversion limit to exceed."
        raise RuntimeError(message)
    return "1" * (limit + 1)


def _nested_past_what_this_build_will_follow() -> str:
    """A JSON array nested deeper than this interpreter's scanner will follow.

    The depth that stops `json` is a CPython *build* constant -- `Py_C_RECURSION_LIMIT`,
    which `sys.setrecursionlimit` does not move -- and it is not the same everywhere.
    Measured: this build gives up at 2998 and the `ubuntu-24.04` runner follows 5000 arrays
    without complaint. A hard-coded 5000 is therefore deep enough on one machine and
    shallow enough on another, which is precisely how it passed here and failed in CI.

    Probed with a bare `json.loads`: `decode_json`'s hooks change how a number is built,
    not which scanner runs, so the limit measured here is the one it will meet.

    Raises rather than returning a body this interpreter parses. A fixture that quietly
    stops provoking the failure it is named after is worse than a missing test, because
    everything downstream still reports as covered.
    """
    depth = 1024
    while depth <= DEEPEST_NESTING_PROBED:
        body = "[" * depth + "]" * depth
        try:
            json.loads(body)
        except RecursionError:
            return body
        depth *= 2
    message = (
        f"No array nested up to {DEEPEST_NESTING_PROBED} deep made this interpreter's JSON "
        "scanner give up, so the RecursionError arm cannot be exercised on this build."
    )
    raise RuntimeError(message)


#: The two bodies `json` refuses for reasons of its own rather than for their syntax. Both
#: are measured rather than written down, because both limits belong to the interpreter and
#: neither is the same on every machine that runs this suite.
TOO_MANY_DIGITS: Final = _longer_than_this_build_will_convert()
NESTED_TOO_DEEP: Final = _nested_past_what_this_build_will_follow()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json", id="not JSON at all: JSONDecodeError"),
        pytest.param(b"\xff\xfe not utf-8", id="not UTF-8: UnicodeDecodeError"),
        pytest.param(TOO_MANY_DIGITS, id="past the integer digit limit: ValueError"),
        pytest.param(NESTED_TOO_DEEP, id="past the scanner's depth: RecursionError"),
    ],
)
def test_the_decoder_still_refuses_the_bodies_it_refused_before(body: str | bytes) -> None:
    """The regression control on a shared decoder that two shipped providers go through.

    `parse_float=` changes how a number is built and must change nothing about which bodies
    are rejected. All four arms in `decode_json`'s own table, because the risk this change
    carries is not that it fails loudly -- it is that it quietly widens or narrows the
    catch clause that #7's review put there.

    Parametrized rather than looped. As a loop this reported `DID NOT RAISE` against the
    `with` line and nothing else: four bodies, one of them no longer refused, and no way to
    tell which without reasoning about interpreter internals. A case that can fail should
    say which case it was.
    """
    with pytest.raises(ProviderResponseError):
        decode_json(body)


def test_the_deeply_nested_body_is_refused_for_the_reason_it_claims() -> None:
    """The fourth arm above is the one that can pass without exercising what it names.

    A body nested less deeply than the scanner will follow parses into a *list*, and every
    shipped parser then refuses that list for being a list. So a provider-level test can
    feed in nested arrays, assert the typed refusal, pass on every platform, and never once
    reach `RecursionError` -- which is what the two chain suites have been doing on Linux
    since #7, with `id="5000 nested arrays: RecursionError"` on the case.

    The arm is only worth something if the body reaches the recursion limit, so that is
    asserted here directly rather than inferred from a typed error further downstream.
    """
    with pytest.raises(RecursionError):
        json.loads(NESTED_TOO_DEEP)


def test_a_refusal_never_quotes_the_body_that_caused_it() -> None:
    """Unchanged by #9 and asserted here because #9 is what made bodies carry money.

    A price body is public market data, but the same decoder reads address balances, and a
    parser error that quoted the text it failed on would put the owner's holdings into a
    log line. The message says the body did not parse and shows nothing.
    """
    secret = f"{{not json {BIP173_TESTNET_P2WPKH}"

    with pytest.raises(ProviderResponseError) as caught:
        decode_json(secret)

    rendered = f"{caught.value}{caught.value!r}"
    assert BIP173_TESTNET_P2WPKH not in rendered
    assert "not json" not in rendered


# --------------------------------------------------------------------------------------
# A number `Decimal` cannot hold at all (#12's review)
# --------------------------------------------------------------------------------------
#
# `parse_float=Decimal` hands the literal text to `Decimal()`, which refuses an exponent past
# `decimal.MAX_EMAX` with `decimal.InvalidOperation` -- an `ArithmeticError`, not a
# `ValueError`, so it went straight through `decode_json`'s `except (ValueError,
# RecursionError)` as an untyped error, from a body the vendor chooses.

#: One past the largest exponent a 64-bit `decimal` build holds, and the largest itself.
#: Written out as the reviewer reproduced them; the premise test below ties both to
#: `decimal.MAX_EMAX`, so a build with a different limit fails there, legibly.
EXPONENT_PAST_THE_LIMIT: Final = "1e1000000000000000000"
EXPONENT_AT_THE_LIMIT: Final = "1e999999999999999999"


def test_the_exponent_literals_straddle_this_builds_limit() -> None:
    assert int(EXPONENT_AT_THE_LIMIT[2:]) == decimal.MAX_EMAX
    assert int(EXPONENT_PAST_THE_LIMIT[2:]) == decimal.MAX_EMAX + 1


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(EXPONENT_PAST_THE_LIMIT, id="bare"),
        pytest.param(f'{{"price": {EXPONENT_PAST_THE_LIMIT}}}', id="inside an object"),
    ],
)
def test_an_exponent_decimal_cannot_hold_is_a_typed_refusal(body: str) -> None:
    """`ProviderResponseError` exactly, never `decimal.InvalidOperation`."""
    with pytest.raises(ProviderResponseError) as caught:
        decode_json(body)

    assert type(caught.value) is ProviderResponseError
    # The reason: the decimal module refused the exponent, and the cause is kept.
    assert isinstance(caught.value.__cause__, decimal.InvalidOperation)
    assert "1000000000000000000" not in f"{caught.value}{caught.value!r}"


def test_the_largest_exponent_decimal_holds_still_decodes() -> None:
    """The companion: the refusal is at the limit, not somewhere short of it."""
    decoded = decode_json(EXPONENT_AT_THE_LIMIT)

    assert type(decoded) is Decimal
    assert decoded.as_tuple() == (0, (1,), 999999999999999999)


# --------------------------------------------------------------------------------------
# The three JSON tokens `parse_float` never sees
# --------------------------------------------------------------------------------------
#
# `parse_float=Decimal` covers every number in a JSON document **except** three, and the
# exception is not documented anywhere a reader would look: `NaN`, `Infinity` and
# `-Infinity` go through `parse_constant`, not `parse_float`. Measured --
# `json.loads('{"p": NaN}', parse_float=Decimal)["p"]` is `nan`, a Python **float**.
#
# So the decoder written to keep floats out of `providers/` was producing one, through the
# one path nobody would check. Invisible to the AST ban as well: there is no literal and no
# name `float` anywhere in the source that produces it.
#
# None of the three is valid JSON. RFC 8259 admits no non-finite number, so refusing them
# turns off a Python extension rather than rejecting a vendor's legitimate output.


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("NaN", id="NaN"),
        pytest.param("Infinity", id="Infinity"),
        pytest.param("-Infinity", id="-Infinity"),
    ],
)
def test_the_three_non_finite_json_extensions_are_refused(token: str) -> None:
    """A typed refusal naming the token, not a `float` handed onward to a parser.

    The message names the token and nothing else. It is one of three fixed words from a
    closed set, so it discloses nothing about the body -- which is the standard every other
    refusal in this package is held to.
    """
    with pytest.raises(ProviderResponseError) as caught:
        decode_json(f'{{"price": {token}}}')

    assert token in str(caught.value)


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("NaN", id="NaN"),
        pytest.param("Infinity", id="Infinity"),
        pytest.param("-Infinity", id="-Infinity"),
    ],
)
def test_plain_json_loads_hands_back_a_float_for_each_of_them(token: str) -> None:
    """The companion, and the reason the refusal is in the decoder rather than in a parser.

    `json.loads` accepts all three out of the box and produces a `float` for each, **even
    with `parse_float=Decimal`**. That is the whole finding in one assertion: the hook a
    reader would assume covers every number does not cover these, and a parser downstream
    would be refusing a value that had already been through binary floating point.

    A NaN is the worst of the three in a money column, because it compares false against
    itself forever -- including against the row it was read from.
    """
    naive = json.loads(f'{{"price": {token}}}', parse_float=Decimal)["price"]

    # `type(...) is float` rather than a pair of `isinstance` checks, for the reason
    # `test_a_json_integer_is_still_an_int_and_not_a_decimal` gives: narrowing to `float`
    # and then asking about `Decimal` is a question mypy can answer statically -- the two
    # have disjoint bases -- and it reports the second line as unreachable.
    assert type(naive) is float
    # And it really did go through `parse_float=Decimal`, which is the finding: the hook
    # was applied and these three tokens went round it.
    assert type(json.loads('{"price": 1.5}', parse_float=Decimal)["price"]) is Decimal


def test_an_ordinary_number_still_decodes_beside_them() -> None:
    """The control. A `parse_constant` that refused everything would pass the tests above.

    `parse_constant` is only consulted for the three tokens, so an ordinary number must be
    unaffected -- and the assertion is on the same document shape the refusals use, so the
    difference is the token and nothing else.
    """
    decoded = decode_json('{"price": 0.04228645}')

    assert isinstance(decoded, dict)
    assert decoded["price"] == Decimal(KASPA_PRICE_DIGITS)


def test_the_refusal_is_not_lost_inside_the_decoders_own_catch_clause() -> None:
    """`decode_json` catches `ValueError`, and a `ValueError` here would be swallowed.

    The refusal is raised from inside `json.loads`, so it passes through
    `except (ValueError, RecursionError)` on its way out. A `ProviderResponseError` is not
    a `ValueError`, so it travels untouched and the caller gets the message naming the
    token; raising a `ValueError` instead would have it caught two frames later and
    re-raised as the generic "the response body is not JSON", which every malformed body
    already produces and which names nothing.

    Asserted by comparing the two messages, because the exception **type** is identical
    either way -- which is exactly why this would have been invisible.
    """
    with pytest.raises(ProviderResponseError) as specific:
        decode_json('{"price": NaN}')
    with pytest.raises(ProviderResponseError) as generic:
        decode_json("not json at all")

    assert str(specific.value) != str(generic.value)
    assert "NaN" in str(specific.value)
