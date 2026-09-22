"""Criterion 3, the pure half: which Bitcoin network an address belongs to.

`domain/addresses.py` answers "is this a Bitcoin address", which is a question about the
string. **Which network it is on is a different question**, and the Esplora provider is the
first thing that has ever needed it, because one Esplora instance serves exactly one
network and a balance read from the wrong chain is a number rather than an error.

Pure, offline, no fixtures, no I/O -- the way everything in `domain/` is tested.

## The mainnet arm, and why it is asserted through a table rather than an address

Rule 3 forbids a mainnet address anywhere in this repository, and
`tests/security/test_address_logging.py::test_fixtures_contain_no_mainnet_address` scans
this file to prove it. So the mainnet rows cannot be driven through `bitcoin_network_of`
with an address, and minting one at runtime to get around the scanner would be scan
evasion dressed as rigour.

What is available instead is better than a single example would have been. The two
lookup tables are exported, so the mainnet rows are pinned **by their HRP and by their
version byte** -- neither of which is an address, and both of which are the actual thing
the function reads. The tables are then asserted to be *total* over `BITCOIN_HRPS` and
`BITCOIN_VERSION_BYTES`, which is the assertion that catches the real future mistake: a
prefix or a version byte added to the codec without a network mapping, which would make
`bitcoin_network_of` raise on an address the registry had just accepted.

The behavioural sweep -- the function itself, over real vectors -- then runs on testnet,
signet and regtest, which is every network this suite is allowed to hold an address for.

## The residual this file states rather than hides

`tb1` is testnet3, testnet4 and signet alike, and **a base58 regtest address reads as
`TESTNET`**: regtest shares version bytes `0x6F` and `0xC4` with testnet, so the string
does not carry the fact and nothing pure can recover it. That is asserted here, on the
`CORE_*` regtest base58 vectors, rather than left for somebody to discover from a wrong
balance. The provider's consequence -- a provider configured for regtest refuses a base58
regtest address -- is asserted in `tests/providers/chains/test_bitcoin.py`.
"""

from __future__ import annotations

import pytest

from portfolio.domain.addresses import (
    BITCOIN_HRPS,
    BITCOIN_NETWORK_BY_HRP,
    BITCOIN_NETWORK_BY_VERSION_BYTE,
    BITCOIN_VERSION_BYTES,
    AddressInvalidError,
    AddressRejection,
    BitcoinNetwork,
    bitcoin_network_of,
)
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WSH,
    BIP350_TESTNET_V1,
    BIP350_UNKNOWN_HRP,
    CORE_REGTEST_P2SH,
    CORE_REGTEST_P2WPKH,
    CORE_REGTEST_V1,
    CORE_SIGNET_P2PKH,
    CORE_TESTNET4_P2PKH,
    CORE_TESTNET4_P2SH,
    CORE_TESTNET4_V1,
    CORE_UNKNOWN_VERSION_BYTE,
    KASPA_TESTNET_V0,
    NAMED_CORRUPTIONS,
)

# --------------------------------------------------------------------------------------
# The tables, which is where the mainnet rows live
# --------------------------------------------------------------------------------------


def test_every_prefix_and_version_byte_maps_to_its_network() -> None:
    """The named test from the plan: both tables, total, and pinned against literals.

    Total over the codec's own sets, so a prefix or a version byte added to
    `domain/addresses.py` without a network mapping fails here -- which is the failure
    that would otherwise present as `bitcoin_network_of` raising on an address the wallet
    registry had just accepted as valid.

    The values are literals rather than derived from the tables. `BITCOIN_NETWORK_BY_HRP
    == BITCOIN_NETWORK_BY_HRP` is true of any table at all, including one that mapped
    everything to `MAINNET` -- which is the mapping that reads a testnet balance off the
    mainnet chain and reports a number.
    """
    assert set(BITCOIN_NETWORK_BY_HRP) == set(BITCOIN_HRPS)
    assert set(BITCOIN_NETWORK_BY_VERSION_BYTE) == set(BITCOIN_VERSION_BYTES)

    assert BITCOIN_NETWORK_BY_HRP["bc"] is BitcoinNetwork.MAINNET
    assert BITCOIN_NETWORK_BY_HRP["tb"] is BitcoinNetwork.TESTNET
    assert BITCOIN_NETWORK_BY_HRP["bcrt"] is BitcoinNetwork.REGTEST

    # 0x00 P2PKH and 0x05 P2SH are mainnet; 0x6F and 0xC4 are testnet, testnet4, signet
    # and regtest alike -- see `test_a_base58_regtest_address_cannot_say_it_is_regtest`.
    assert BITCOIN_NETWORK_BY_VERSION_BYTE[0x00] is BitcoinNetwork.MAINNET
    assert BITCOIN_NETWORK_BY_VERSION_BYTE[0x05] is BitcoinNetwork.MAINNET
    assert BITCOIN_NETWORK_BY_VERSION_BYTE[0x6F] is BitcoinNetwork.TESTNET
    assert BITCOIN_NETWORK_BY_VERSION_BYTE[0xC4] is BitcoinNetwork.TESTNET


def test_the_network_enum_is_the_three_networks_and_their_wire_values() -> None:
    """Pinned, because the values are what reach a setting and a document.

    `PORTFOLIO_BITCOIN_NETWORK` is typed against these strings, so renaming a member
    silently invalidates every `.env` file on every deployment. A `StrEnum` whose values
    drifted from its members would also make `Settings(bitcoin_network="testnet")` build
    and then compare unequal to `BitcoinNetwork.TESTNET`, which is a bug that reads as a
    provider refusing every address.
    """
    assert [member.value for member in BitcoinNetwork] == ["mainnet", "testnet", "regtest"]
    # `.value`, not the member, and not by preference. Under `mypy --strict` a `StrEnum`
    # member and a string literal are non-overlapping literal types -- mypy does not read
    # the member's value -- so `BitcoinNetwork.MAINNET == "mainnet"` is a comparison-overlap
    # error even though it is `True` at runtime. `ChainKey` has the same property and the
    # source spells it `.value` for the same reason.
    assert BitcoinNetwork.MAINNET.value == "mainnet"
    assert BitcoinNetwork.TESTNET.value == "testnet"
    assert BitcoinNetwork.REGTEST.value == "regtest"


def test_no_table_entry_is_a_network_that_does_not_exist() -> None:
    """The control on the two totality assertions above.

    A table whose values were plain strings rather than members would satisfy every
    `set(...) == set(...)` assertion and then compare unequal to a `BitcoinNetwork` at the
    one place it matters, inside the provider's wrong-network check.
    """
    for network in (*BITCOIN_NETWORK_BY_HRP.values(), *BITCOIN_NETWORK_BY_VERSION_BYTE.values()):
        assert isinstance(network, BitcoinNetwork)


# --------------------------------------------------------------------------------------
# The function itself, over every vector this repository is allowed to hold
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "network"),
    [
        pytest.param(BIP173_TESTNET_P2WPKH, BitcoinNetwork.TESTNET, id="tb1 v0 p2wpkh"),
        pytest.param(BIP173_TESTNET_P2WSH, BitcoinNetwork.TESTNET, id="tb1 v0 p2wsh"),
        pytest.param(BIP350_TESTNET_V1, BitcoinNetwork.TESTNET, id="tb1 v1 bech32m"),
        pytest.param(CORE_TESTNET4_V1, BitcoinNetwork.TESTNET, id="tb1 v1 testnet4"),
        pytest.param(CORE_REGTEST_P2WPKH, BitcoinNetwork.REGTEST, id="bcrt1 v0"),
        pytest.param(CORE_REGTEST_V1, BitcoinNetwork.REGTEST, id="bcrt1 v1 bech32m"),
        pytest.param(CORE_TESTNET4_P2PKH, BitcoinNetwork.TESTNET, id="base58 p2pkh testnet4"),
        pytest.param(CORE_SIGNET_P2PKH, BitcoinNetwork.TESTNET, id="base58 p2pkh signet"),
        pytest.param(CORE_TESTNET4_P2SH, BitcoinNetwork.TESTNET, id="base58 p2sh testnet4"),
    ],
)
def test_a_published_vector_reads_as_the_network_its_source_says_it_is_on(
    address: str, network: BitcoinNetwork
) -> None:
    """Both encodings and both witness versions, on three of the four networks.

    Every row's network comes from the vector's published provenance -- BIP-173, BIP-350,
    or the `chain` field of Bitcoin Core's `key_io_valid.json` -- and not from running the
    function and writing down what it said. A vector labelled by its subject proves the
    subject agrees with itself.
    """
    assert bitcoin_network_of(address) is network


def test_a_base58_regtest_address_cannot_say_it_is_regtest() -> None:
    """The residual, asserted rather than documented, because it has a consequence.

    Regtest reuses testnet's version bytes, so `CORE_REGTEST_P2SH` -- Bitcoin Core's own
    `key_io_valid.json` row for chain `regtest` -- is indistinguishable from a testnet
    P2SH address. Nothing pure can recover the fact, so the honest answer is `TESTNET` and
    the provider's honest response to it, when configured for regtest, is a refusal rather
    than a guess.

    The vector is named rather than spelled out, here and everywhere else. Provenance is
    the entire reason `tests/address_vectors.py` exists: a literal pasted into a docstring
    has no source attached to it, drifts silently when the constant it was copied from
    changes, and is invisible to the scans that check where an address came from.

    This test exists so that "fixing" it -- by sniffing something else, or by returning
    `REGTEST` on a hunch -- fails here rather than producing confident wrong balances.
    """
    assert bitcoin_network_of(CORE_REGTEST_P2SH) is BitcoinNetwork.TESTNET
    # And the bech32 spelling of the same network *does* carry it, which is the asymmetry.
    assert bitcoin_network_of(CORE_REGTEST_P2WPKH) is BitcoinNetwork.REGTEST


def test_the_uppercase_spelling_of_a_bech32_address_is_the_same_network() -> None:
    """Bech32 is case insensitive, so a QR wallet's uppercase rendering is one address.

    The canonical form is lowercase and that is what a provider is asked about, but a
    function that read the HRP case-sensitively would raise `UNKNOWN_PREFIX` on `TB1...`
    -- which arrives from a scanner far more often than anybody plans for.
    """
    assert bitcoin_network_of(BIP173_TESTNET_P2WPKH.upper()) is BitcoinNetwork.TESTNET


# --------------------------------------------------------------------------------------
# It is not a guess: a string it cannot read is refused, not assigned a network
# --------------------------------------------------------------------------------------


def test_an_unknown_human_readable_prefix_is_refused_rather_than_guessed() -> None:
    """`tc1...` is BIP-350's own invalid vector, and it is not a network.

    The tempting implementation is `"tb" in address -> testnet, else mainnet`, and its
    failure mode is silent: every unrecognised string becomes a mainnet address, and a
    provider configured for mainnet then reads a balance for it.
    """
    with pytest.raises(AddressInvalidError) as caught:
        bitcoin_network_of(BIP350_UNKNOWN_HRP)

    assert caught.value.reason is AddressRejection.UNKNOWN_PREFIX


def test_an_unknown_version_byte_is_refused_rather_than_guessed() -> None:
    """A base58 string whose checksum verifies and whose version byte is `0xD4`.

    From Bitcoin Core's `key_io_invalid.json`. It is the vector that separates "the
    version byte was looked at" from "the checksum passed, so it is an address".
    """
    with pytest.raises(AddressInvalidError) as caught:
        bitcoin_network_of(CORE_UNKNOWN_VERSION_BYTE)

    assert caught.value.reason is AddressRejection.UNKNOWN_VERSION_BYTE


@pytest.mark.parametrize(
    ("name", "corrupted"),
    [pytest.param(name, corrupted, id=name) for name, _valid, corrupted in NAMED_CORRUPTIONS],
)
def test_a_one_character_corruption_has_no_network_at_all(name: str, corrupted: str) -> None:
    """The checksum is re-run, so a mistyped address is refused rather than classified.

    Every named corruption in the shared vectors, both encodings. A network function that
    read only the prefix would happily report `TESTNET` for a string that is not an
    address, and the provider would then build a URL out of it -- which is precisely the
    path-traversal shape the spec says validation stands in front of.
    """
    del name  # In the parameter id, where a failure can read it.

    with pytest.raises(AddressInvalidError):
        bitcoin_network_of(corrupted)


def test_an_address_from_another_chain_is_refused() -> None:
    """A Kaspa address is a valid address and is not on any Bitcoin network.

    The wallet registry holds both chains in one table, so a caller holding the wrong row
    is a real mistake rather than a hypothetical one -- and "it starts with `kaspatest:`,
    so it is not `bc`, so it is mainnet" is the shape of answer this refuses.
    """
    with pytest.raises(AddressInvalidError):
        bitcoin_network_of(KASPA_TESTNET_V0)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("tb1", id="a prefix and nothing else"),
        pytest.param("not-an-address", id="prose"),
    ],
)
def test_a_string_that_is_not_an_address_is_refused_as_an_address_rejection(raw: str) -> None:
    """Whatever the reason, it is an `AddressInvalidError` and never something else.

    A `KeyError`, an `IndexError` or a bare `ValueError` escaping here would reach the
    provider, which catches `AddressInvalidError` and nothing else -- so an unhandled
    exception type turns a mistyped address into a 500 three layers up.
    """
    with pytest.raises(AddressInvalidError):
        bitcoin_network_of(raw)


def test_no_refusal_message_contains_the_address() -> None:
    """The #44 shape, at the one function in this change that takes an address by value.

    `AddressInvalidError` renders a fixed sentence per reason and never interpolates, and
    that property has to survive a new raiser being added to a module full of them.
    Asserted over `str(exc)` and over `exc.args`, because a message built correctly and an
    argument tuple built carelessly are two different mistakes.
    """
    _name, _valid, corrupted = NAMED_CORRUPTIONS[0]

    for raw in (corrupted, BIP350_UNKNOWN_HRP, CORE_UNKNOWN_VERSION_BYTE, KASPA_TESTNET_V0):
        with pytest.raises(AddressInvalidError) as caught:
            bitcoin_network_of(raw)

        assert raw not in str(caught.value)
        assert raw[:20] not in str(caught.value)
        assert all(raw not in str(argument) for argument in caught.value.args)
