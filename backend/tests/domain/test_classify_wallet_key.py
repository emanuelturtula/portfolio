"""Spec 031, criterion 1 at the registration boundary: what kind of thing was pasted.

`classify_wallet_key(chain_key, raw)` decides between an address and an extended key. On
Bitcoin, any of the twenty prefixes R2 names goes to the extended-key parser and everything
else to the address codecs, unchanged. On Kaspa the twenty prefixes are refused by prefix
(R2a): a private one as `private_key`, a public one as `extended_key`.

The private-key and multisig strings are short and not key-shaped (R2, R11).
"""

from __future__ import annotations

from typing import Final

import pytest

from portfolio.domain.addresses import MAX_ADDRESS_LENGTH, AddressInvalidError, AddressRejection
from portfolio.domain.chains import (
    ChainKey,
    WalletKey,
    WalletKind,
    classify_wallet_key,
    validate_address,
)
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WPKH_UPPERCASE,
    BLANK_INPUTS,
    CORE_TESTNET4_P2SH,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V1_KEY,
    NAMED_CORRUPTIONS,
    SYNTHETIC_TPUB,
)
from tests.extended_key_forms import (
    TPUB_VERSION,
    depth_zero,
    private_key_shaped_run,
    reserialised,
    with_run_inside,
)
from tests.extended_key_vectors import (
    BIP32_TV1_M,
    BIP49_ACCOUNT_UPUB,
    BIP84_ACCOUNT_VPUB,
    DERIVED_VPUB_MULTISIG,
    MULTISIG_PREFIXES,
    PRIVATE_PREFIXES,
    SINGLE_SIG_PREFIXES,
    short,
)

TEST_NETWORK_KEYS: Final = (BIP32_TV1_M, BIP49_ACCOUNT_UPUB, BIP84_ACCOUNT_VPUB)


def refusal(chain_key: str, raw: str) -> AddressRejection:
    with pytest.raises(AddressInvalidError) as caught:
        classify_wallet_key(chain_key, raw)
    return caught.value.reason


def test_the_wallet_kinds() -> None:
    assert {kind.value for kind in WalletKind} == {"address", "extended_key"}
    assert WalletKind.ADDRESS.value == "address"
    assert WalletKind.EXTENDED_KEY.value == "extended_key"


# --------------------------------------------------------------------------------------
# Bitcoin: extended keys
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", TEST_NETWORK_KEYS, ids=["tpub", "upub", "vpub"])
def test_a_bitcoin_extended_key_is_classified_as_one(key: str) -> None:
    """R1 and review finding S1: display is the key as entered; canonical is it at depth 0.

    No case folding in either. The canonical form is computed in `tests/extended_key_forms.py`
    from the serialisation format, not by the function under test.
    """
    classified = classify_wallet_key("bitcoin", key)

    assert classified == WalletKey(
        kind=WalletKind.EXTENDED_KEY, canonical=depth_zero(key), display=key
    )


def test_a_master_key_is_its_own_canonical_form_and_an_account_key_is_not() -> None:
    """TV1's master is at depth 0 already; BIP-84's account key is at depth 3."""
    assert classify_wallet_key("bitcoin", BIP32_TV1_M).canonical == BIP32_TV1_M
    assert classify_wallet_key("bitcoin", BIP84_ACCOUNT_VPUB).canonical != BIP84_ACCOUNT_VPUB


@pytest.mark.parametrize(
    "position",
    [
        pytest.param(bytes(9), id="depth 0"),
        pytest.param(bytes([1]) + bytes.fromhex("aabbccdd") + (5).to_bytes(4, "big"), id="depth 1"),
        pytest.param(
            bytes([255]) + bytes.fromhex("01020304") + (2**31 - 1).to_bytes(4, "big"),
            id="depth 255",
        ),
    ],
)
def test_every_export_of_one_account_has_one_canonical_form(position: bytes) -> None:
    """S1: depth, parent fingerprint and child number do not change what is derived."""
    other_export = reserialised(BIP84_ACCOUNT_VPUB, position=position)

    classified = classify_wallet_key("bitcoin", other_export)

    assert classified.canonical == classify_wallet_key("bitcoin", BIP84_ACCOUNT_VPUB).canonical
    assert classified.display == other_export


def test_the_version_is_part_of_the_canonical_form() -> None:
    """The same chain code and key under P2PKH and under P2WPKH derive different addresses."""
    as_tpub = reserialised(BIP84_ACCOUNT_VPUB, version=TPUB_VERSION)

    assert as_tpub.startswith("tpub")
    assert (
        classify_wallet_key("bitcoin", as_tpub).canonical
        != classify_wallet_key("bitcoin", BIP84_ACCOUNT_VPUB).canonical
    )


@pytest.mark.parametrize("padding", ["  ", "\n", "\t ", " \r\n"])
def test_surrounding_whitespace_is_stripped_from_both_forms(padding: str) -> None:
    classified = classify_wallet_key("bitcoin", f"{padding}{BIP84_ACCOUNT_VPUB}{padding}")

    assert classified.kind is WalletKind.EXTENDED_KEY
    assert classified.canonical == depth_zero(BIP84_ACCOUNT_VPUB)
    assert classified.display == BIP84_ACCOUNT_VPUB


def test_the_enum_chain_key_is_accepted_like_the_string() -> None:
    assert classify_wallet_key(ChainKey.BITCOIN, BIP32_TV1_M) == classify_wallet_key(
        "bitcoin", BIP32_TV1_M
    )


@pytest.mark.parametrize("prefix", PRIVATE_PREFIXES)
def test_a_private_key_on_bitcoin_is_refused_by_name(prefix: str) -> None:
    assert refusal("bitcoin", short(prefix)) is AddressRejection.PRIVATE_KEY
    assert refusal("bitcoin", f"  {short(prefix)}\n") is AddressRejection.PRIVATE_KEY


@pytest.mark.parametrize("prefix", MULTISIG_PREFIXES)
def test_a_multisig_key_on_bitcoin_is_refused_by_name(prefix: str) -> None:
    assert refusal("bitcoin", short(prefix)) is AddressRejection.EXTENDED_KEY_MULTISIG


def test_a_well_formed_multisig_key_on_bitcoin_is_refused_by_name() -> None:
    assert refusal("bitcoin", DERIVED_VPUB_MULTISIG) is AddressRejection.EXTENDED_KEY_MULTISIG


@pytest.mark.parametrize("prefix", SINGLE_SIG_PREFIXES)
def test_a_short_public_prefix_on_bitcoin_reaches_the_parser(prefix: str) -> None:
    """Not the address codec: a short `tpub` is a malformed key, not an extended_key refusal.

    The mainnet prefixes are here as four characters and a short tail, which is a prefix and
    not a key (R11).
    """
    assert refusal("bitcoin", short(prefix)) is AddressRejection.MALFORMED


def test_a_key_that_is_not_a_point_is_refused_as_such() -> None:
    """The suite's old `SYNTHETIC_TPUB`: well formed, but its key is a hash, not a point."""
    assert refusal("bitcoin", SYNTHETIC_TPUB) is AddressRejection.INVALID_PUBLIC_KEY


# --------------------------------------------------------------------------------------
# Bitcoin: addresses are unchanged
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [BIP173_TESTNET_P2WPKH, BIP173_TESTNET_P2WPKH_UPPERCASE, CORE_TESTNET4_P2SH],
    ids=["bech32", "bech32 uppercase", "base58"],
)
def test_a_bitcoin_address_is_classified_as_an_address(address: str) -> None:
    validated = validate_address("bitcoin", address)

    assert classify_wallet_key("bitcoin", address) == WalletKey(
        kind=WalletKind.ADDRESS, canonical=validated.canonical, display=validated.display
    )


def test_a_bitcoin_address_refusal_is_unchanged() -> None:
    corrupted = NAMED_CORRUPTIONS[0][2]
    with pytest.raises(AddressInvalidError) as expected:
        validate_address("bitcoin", corrupted)

    assert refusal("bitcoin", corrupted) is expected.value.reason


# --------------------------------------------------------------------------------------
# Kaspa: the twenty prefixes are refused by prefix, as ruling R2a says
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", PRIVATE_PREFIXES)
def test_a_private_key_on_kaspa_is_refused_as_a_private_key(prefix: str) -> None:
    assert refusal("kaspa", short(prefix)) is AddressRejection.PRIVATE_KEY


@pytest.mark.parametrize("prefix", SINGLE_SIG_PREFIXES + MULTISIG_PREFIXES)
def test_a_public_extended_key_prefix_on_kaspa_is_an_extended_key(prefix: str) -> None:
    assert refusal("kaspa", short(prefix)) is AddressRejection.EXTENDED_KEY


@pytest.mark.parametrize("key", TEST_NETWORK_KEYS, ids=["tpub", "upub", "vpub"])
def test_a_whole_extended_key_on_kaspa_is_an_extended_key_not_mixed_case(key: str) -> None:
    """Before R2a this reached the Kaspa codec and was refused as `mixed_case`."""
    assert refusal("kaspa", key) is AddressRejection.EXTENDED_KEY
    assert refusal("kaspa", f" {key} ") is AddressRejection.EXTENDED_KEY


@pytest.mark.parametrize("address", [KASPA_TESTNET_V0, KASPA_TESTNET_V1_KEY])
def test_a_kaspa_address_is_classified_as_an_address(address: str) -> None:
    assert classify_wallet_key("kaspa", address) == WalletKey(
        kind=WalletKind.ADDRESS, canonical=address, display=address
    )


def test_a_bitcoin_address_on_kaspa_is_still_refused_by_the_kaspa_codec() -> None:
    with pytest.raises(AddressInvalidError) as expected:
        validate_address("kaspa", BIP173_TESTNET_P2WPKH)

    assert refusal("kaspa", BIP173_TESTNET_P2WPKH) is expected.value.reason


# --------------------------------------------------------------------------------------
# Shapes common to both
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", BLANK_INPUTS, ids=["empty", "space", "tab", "newline", "mixed"])
@pytest.mark.parametrize("chain_key", ["bitcoin", "kaspa"])
def test_a_blank_input_is_empty(chain_key: str, raw: str) -> None:
    assert refusal(chain_key, raw) is AddressRejection.EMPTY


@pytest.mark.parametrize("chain_key", ["bitcoin", "kaspa"])
def test_an_over_long_input_is_too_long_even_with_a_key_prefix(chain_key: str) -> None:
    raw = "tpub" + "1" * MAX_ADDRESS_LENGTH
    assert refusal(chain_key, raw) is AddressRejection.TOO_LONG


def test_an_unknown_chain_is_refused() -> None:
    assert refusal("ethereum", BIP32_TV1_M) is AddressRejection.UNKNOWN_CHAIN


def test_a_private_key_is_named_before_the_chain_is_looked_at() -> None:
    """R2b: the private-key test is the first thing done, on any chain key at all."""
    assert refusal("ethereum", "tprv8Zgx") is AddressRejection.PRIVATE_KEY


def test_the_maximum_length_admits_an_extended_key() -> None:
    """A serialised extended key is 111 characters, under the 128-character ceiling."""
    assert len(BIP32_TV1_M) == 111
    assert len(BIP32_TV1_M) <= MAX_ADDRESS_LENGTH


@pytest.mark.parametrize(
    "raw",
    [short("tprv"), DERIVED_VPUB_MULTISIG, SYNTHETIC_TPUB, short("zpub")],
    ids=["private", "multisig", "not a point", "short public"],
)
@pytest.mark.parametrize("chain_key", ["bitcoin", "kaspa"])
def test_no_refusal_quotes_what_was_pasted(chain_key: str, raw: str) -> None:
    with pytest.raises(AddressInvalidError) as caught:
        classify_wallet_key(chain_key, raw)

    assert raw not in str(caught.value)
    assert raw not in repr(caught.value.args)


# --------------------------------------------------------------------------------------
# R2b at the registration boundary: the raw value, before stripping and the length cap
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("chain_key", ["bitcoin", "kaspa"])
@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param("{}", id="alone"),
        pytest.param('"{}"', id="in quotes"),
        pytest.param("my key: {}", id="after text"),
        pytest.param("​{}", id="behind a zero-width space"),
    ],
)
def test_a_private_key_run_anywhere_is_refused_by_name(chain_key: str, wrap: str) -> None:
    run = private_key_shaped_run("tprv")

    assert refusal(chain_key, wrap.format(run)) is AddressRejection.PRIVATE_KEY


@pytest.mark.parametrize("chain_key", ["bitcoin", "kaspa"])
def test_a_private_key_inside_an_over_long_paste_is_named_not_measured(chain_key: str) -> None:
    pasted = "x" * MAX_ADDRESS_LENGTH + " " + private_key_shaped_run("uprv", 107)
    assert len(pasted.strip()) > MAX_ADDRESS_LENGTH

    assert refusal(chain_key, pasted) is AddressRejection.PRIVATE_KEY


def test_an_over_long_paste_with_no_key_in_it_is_still_too_long() -> None:
    assert refusal("bitcoin", "x" * (MAX_ADDRESS_LENGTH + 1)) is AddressRejection.TOO_LONG


def test_a_glued_run_is_not_refused_as_a_private_key() -> None:
    """R2b's documented give-up: whatever it is refused as, it is not `private_key`."""
    assert refusal("bitcoin", f"abc{private_key_shaped_run('tprv')}") is not (
        AddressRejection.PRIVATE_KEY
    )


@pytest.mark.parametrize("at", [5, 6, 7])
def test_a_valid_public_key_with_a_run_in_its_body_is_registered(at: int) -> None:
    key = with_run_inside(BIP84_ACCOUNT_VPUB, "uprv", at)

    classified = classify_wallet_key("bitcoin", key)

    assert classified.kind is WalletKind.EXTENDED_KEY
    assert classified.display == key
    assert classified.canonical == depth_zero(key)


def test_a_format_character_is_removed_for_the_test_only() -> None:
    """An address carrying one is still refused, and not as a private key."""
    address = BIP173_TESTNET_P2WPKH
    carried = f"{address[:6]}​{address[6:]}"

    assert refusal("bitcoin", carried) is AddressRejection.INVALID_CHARACTER
