"""Criterion 6, the pure half: which Kaspa network an address belongs to.

`domain/addresses.py` answers "is this a Kaspa address", which is a question about the
string. **Which network it is on is a different question**, and the Kaspa provider is the
first thing that has ever needed it, because one Kaspa REST instance serves exactly one
network -- measured on 2026-09-23, the vendor's own path validation hard-codes `kaspa:` in
its regex -- and a balance read from the wrong chain is a number rather than an error.

Pure, offline, no fixtures, no I/O -- the way everything in `domain/` is tested.

## The contrast with Bitcoin, which is the reason this file exists separately

`bitcoin_network_of` **cannot** tell testnet3 from testnet4 from signet, and cannot tell a
base58 regtest address from a testnet one; `tests/domain/test_bitcoin_network.py` records
that residual because nothing can close it. **Kaspa has no such collapse.** `kaspa`,
`kaspatest` and `kaspadev` are three distinct prefixes and each one is folded into the
40-bit checksum, so the same payload checksums differently on each network. The check is
exact here and approximate there, and a reader comparing the two modules deserves to be
told which is which rather than assuming they are copies.

## The mainnet and devnet arms, asserted through a table rather than an address

Rule 3 forbids a mainnet address anywhere in this repository, and
`tests/security/test_address_logging.py::test_fixtures_contain_no_mainnet_address` scans
this file to prove it. `tests/address_vectors.py` also holds no *valid* `kaspadev:` vector
-- the one devnet string in it, `KASPA_WRONG_NETWORK_PREFIX`, carries the testnet vector's
payload and therefore fails its checksum by construction, which is the entire point of it.

So the mainnet and devnet rows are pinned **by their prefix**, which is not an address and
is the actual thing the function reads, and the table is then asserted to be *total* over
`KASPA_PREFIXES`. That totality assertion is the one that catches the real future mistake:
a prefix added to the codec without a network mapping, which would make `kaspa_network_of`
raise on an address the wallet registry had just accepted.

The behavioural sweep -- the function itself, over real published vectors -- then runs on
testnet, which is the only Kaspa network this suite is allowed to hold an address for.
"""

from __future__ import annotations

import pytest

from portfolio.domain.addresses import (
    KASPA_NETWORK_BY_PREFIX,
    KASPA_PREFIXES,
    AddressInvalidError,
    AddressRejection,
    KaspaNetwork,
    kaspa_network_of,
)
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    KASPA_MIXED_CASE,
    KASPA_NAMED_CORRUPTIONS,
    KASPA_NO_PREFIX,
    KASPA_OVERLONG_PAYLOAD,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V0_ASPECTRON,
    KASPA_TESTNET_V1_KEY,
    KASPA_TESTNET_V1_ZERO,
    KASPA_UNKNOWN_PREFIX,
    KASPA_UNKNOWN_VERSION_BYTE,
    KASPA_VECTORS,
    KASPA_WRONG_NETWORK_PREFIX,
    Vector,
)

# --------------------------------------------------------------------------------------
# The table, which is where the mainnet and devnet rows live
# --------------------------------------------------------------------------------------


def test_every_prefix_maps_to_its_network() -> None:
    """The named test from the plan: the table, total, and pinned against literals.

    Total over the codec's own set, so a prefix added to `domain/addresses.py` without a
    network mapping fails here -- which is the failure that would otherwise present as
    `kaspa_network_of` raising on an address the wallet registry had just accepted.

    The values are literals rather than derived from the table. `KASPA_NETWORK_BY_PREFIX
    == KASPA_NETWORK_BY_PREFIX` is true of any table at all, including one that mapped
    every prefix to `MAINNET` -- which is the mapping that reads a testnet balance off the
    mainnet chain and reports a number nothing downstream can tell from a right one.
    """
    assert set(KASPA_NETWORK_BY_PREFIX) == set(KASPA_PREFIXES)

    assert KASPA_NETWORK_BY_PREFIX["kaspa"] is KaspaNetwork.MAINNET
    assert KASPA_NETWORK_BY_PREFIX["kaspatest"] is KaspaNetwork.TESTNET
    assert KASPA_NETWORK_BY_PREFIX["kaspadev"] is KaspaNetwork.DEVNET


def test_the_network_enum_is_the_three_networks_and_their_wire_values() -> None:
    """Pinned, because the values are what reach a setting and a document.

    `PORTFOLIO_KASPA_NETWORK` is typed against these strings, so renaming a member
    silently invalidates every `.env` file on every deployment. A `StrEnum` whose values
    drifted from its members would also make `Settings(kaspa_network="testnet")` build and
    then compare unequal to `KaspaNetwork.TESTNET`, which is a bug that reads as a
    provider refusing every address it is given.
    """
    assert [member.value for member in KaspaNetwork] == ["mainnet", "testnet", "devnet"]
    # `.value`, not the member, and not by preference. Under `mypy --strict` a `StrEnum`
    # member and a string literal are non-overlapping literal types -- mypy does not read
    # the member's value -- so `KaspaNetwork.MAINNET == "mainnet"` is a comparison-overlap
    # error even though it is `True` at runtime. `BitcoinNetwork` is spelled the same way
    # for the same reason.
    assert KaspaNetwork.MAINNET.value == "mainnet"
    assert KaspaNetwork.TESTNET.value == "testnet"
    assert KaspaNetwork.DEVNET.value == "devnet"


def test_no_table_entry_is_a_network_that_does_not_exist() -> None:
    """The control on the totality assertion above.

    A table whose values were plain strings rather than members would satisfy
    `set(...) == set(...)` and then compare unequal to a `KaspaNetwork` at the one place it
    matters, inside the provider's wrong-network check -- where the consequence is that
    every address is refused, or none is.
    """
    for network in KASPA_NETWORK_BY_PREFIX.values():
        assert isinstance(network, KaspaNetwork)


def test_the_three_prefixes_are_distinct_which_is_what_bitcoin_cannot_say() -> None:
    """The contrast stated as an assertion rather than only in a docstring.

    Bitcoin's table maps four version bytes onto three networks and collapses regtest into
    testnet; this one is a bijection. If a future edit ever mapped two Kaspa prefixes onto
    one network, the module docstring's claim that "the check is exact here" would become
    false, and the place that would otherwise notice is a wrong balance.
    """
    networks = list(KASPA_NETWORK_BY_PREFIX.values())

    assert len(set(networks)) == len(networks)
    assert len(networks) == len(KaspaNetwork)


# --------------------------------------------------------------------------------------
# The function itself, over every vector this repository is allowed to hold
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("vector", [pytest.param(vector, id=vector.id) for vector in KASPA_VECTORS])
def test_every_published_testnet_vector_reads_as_testnet(vector: Vector) -> None:
    """All four published vectors, from two unrelated publishers.

    Every row's network comes from its provenance -- rusty-kaspa's own `mod tests::cases()`
    and the Aspectron documentation -- rather than from running the function and writing
    down what it said. A vector labelled by its subject proves the subject agrees with
    itself and nothing more.
    """
    assert kaspa_network_of(vector.canonical) is KaspaNetwork.TESTNET


def test_the_version_byte_does_not_change_the_network() -> None:
    """Version 0 and version 1 are the same network, which a length-based guess would miss.

    A version-1 address is one character longer than a version-0 one. Nothing here reads
    the length, and this pair is what says so: an implementation that classified on the
    string's size rather than on its prefix would split these two apart.
    """
    assert kaspa_network_of(KASPA_TESTNET_V0) is kaspa_network_of(KASPA_TESTNET_V1_ZERO)
    assert kaspa_network_of(KASPA_TESTNET_V1_KEY) is KaspaNetwork.TESTNET
    assert kaspa_network_of(KASPA_TESTNET_V0_ASPECTRON) is KaspaNetwork.TESTNET


def test_the_uppercase_spelling_of_an_address_is_the_same_network() -> None:
    """`validate_kaspa_address` accepts an uppercase rendering, so this has to as well.

    A QR code or a hardware wallet screen shows the uppercase form, and the registry keeps
    it in the `display` column deliberately. A network function that read the prefix
    case-sensitively would raise `UNKNOWN_PREFIX` on a string the registry had already
    accepted -- a contradiction that surfaces as a wallet nobody can read.
    """
    assert kaspa_network_of(KASPA_TESTNET_V0.upper()) is KaspaNetwork.TESTNET


# --------------------------------------------------------------------------------------
# It is not a guess: a string it cannot read is refused, not assigned a network
# --------------------------------------------------------------------------------------


def test_a_prefix_this_application_does_not_accept_is_refused_rather_than_guessed() -> None:
    """`kaspasim:` is a real prefix in rusty-kaspa and deliberately not one we accept.

    The tempting implementation is `raw.split(":")[0]` followed by "anything unfamiliar is
    mainnet", and its failure is silent: a simulation-network address becomes a mainnet
    address, and a provider configured for mainnet then reads a balance for it.
    """
    with pytest.raises(AddressInvalidError) as caught:
        kaspa_network_of(KASPA_UNKNOWN_PREFIX)

    assert caught.value.reason is AddressRejection.UNKNOWN_PREFIX


def test_a_payload_carrying_another_networks_checksum_is_refused_not_classified() -> None:
    """`KASPA_WRONG_NETWORK_PREFIX` is the vector this whole file turns on.

    It is `KASPA_TESTNET_V0`'s data part under the `kaspadev:` prefix. Kaspa folds the
    prefix into the checksum, so it is **not** a devnet address -- it is not an address at
    all, and the only thing that can tell is a decoder that recomputes the checksum over
    the prefix.

    A `kaspa_network_of` that partitioned on `":"` and looked the prefix up in the table
    would answer `DEVNET` here, confidently, for a string no Kaspa node would accept. That
    is the same class of answer `bitcoin_network_of` refuses to give, and it matters more
    here because the caller is a provider holding a value that came out of a database
    column -- the checksum is re-verified rather than assumed, which is what makes this
    function safe on any string rather than only on one that has already validated.
    """
    with pytest.raises(AddressInvalidError) as caught:
        kaspa_network_of(KASPA_WRONG_NETWORK_PREFIX)

    assert caught.value.reason is AddressRejection.BAD_CHECKSUM


@pytest.mark.parametrize(
    ("name", "corrupted"),
    [pytest.param(name, corrupted, id=name) for name, _valid, corrupted in KASPA_NAMED_CORRUPTIONS],
)
def test_a_one_character_corruption_has_no_network_at_all(name: str, corrupted: str) -> None:
    """The checksum is re-run, so a mistyped address is refused rather than classified.

    Every named corruption in the shared vectors. A network function that read only the
    prefix would happily report `TESTNET` for a string that is not an address, and the
    provider would then build a URL out of it -- which is precisely the path-traversal
    shape validation stands in front of.
    """
    del name  # In the parameter id, where a failure can read it.

    with pytest.raises(AddressInvalidError):
        kaspa_network_of(corrupted)


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        pytest.param("", "empty", id="empty"),
        pytest.param("   ", "whitespace", id="whitespace"),
        pytest.param("kaspatest", "a prefix with no separator", id="no separator"),
        pytest.param(
            "kaspatest:", "a separator and nothing after it", id="nothing after the colon"
        ),
        pytest.param(KASPA_NO_PREFIX, "the payload alone", id="no prefix at all"),
        pytest.param(KASPA_MIXED_CASE, "mixed case", id="mixed case"),
        pytest.param(KASPA_UNKNOWN_VERSION_BYTE, "version byte 2", id="an unknown version byte"),
        pytest.param(KASPA_OVERLONG_PAYLOAD, "one extra character", id="an overlong payload"),
        pytest.param(BIP173_TESTNET_P2WPKH, "an address on another chain", id="a bitcoin address"),
        pytest.param("not-an-address", "prose", id="prose"),
    ],
)
def test_a_string_that_is_not_a_kaspa_address_is_refused(raw: str, why: str) -> None:
    """Whatever the reason, it is an `AddressInvalidError` and never something else.

    A `KeyError`, an `IndexError` or a bare `ValueError` escaping here would reach the
    provider, which catches `AddressInvalidError` and nothing else -- so an unhandled
    exception type turns a mistyped address into a 500 three layers up.

    The overlong-payload row is the subtle one. It regroups into the same bytes as
    `KASPA_TESTNET_V0` and carries a recomputed, *valid* checksum, so a decoder that
    checked only the decoded byte count would accept it as a second spelling of an address
    that already exists -- and a second spelling is a holding counted twice.
    """
    del why  # In the parameter id, where a failure can read it.

    with pytest.raises(AddressInvalidError):
        kaspa_network_of(raw)


def test_no_refusal_message_contains_the_address() -> None:
    """The #44 shape, at the one function in this change that takes an address by value.

    `AddressInvalidError` renders a fixed sentence per reason and never interpolates, and
    that property has to survive a new raiser being added to a module full of them.
    Asserted over `str(exc)` and over `exc.args`, because a message built correctly and an
    argument tuple built carelessly are two different mistakes.
    """
    _name, _valid, corrupted = KASPA_NAMED_CORRUPTIONS[0]

    for raw in (corrupted, KASPA_UNKNOWN_PREFIX, KASPA_WRONG_NETWORK_PREFIX, KASPA_MIXED_CASE):
        with pytest.raises(AddressInvalidError) as caught:
            kaspa_network_of(raw)

        assert raw not in str(caught.value)
        assert raw[:20] not in str(caught.value)
        assert all(raw not in str(argument) for argument in caught.value.args)
