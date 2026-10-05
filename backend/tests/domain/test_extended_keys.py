"""Spec 031, criterion 1: an extended public key is parsed and validated offline.

"Validated" means the checksum, a known version, BIP-32's depth-0 rules and a public key on
the curve (criterion 1's interpretation). Multisig and private keys are refused **by prefix,
before any decoding** (R2), so the strings that test those refusals are short and are not
key-shaped: a refusal that needs only four characters is proven with four characters and a
short tail.

**Mainnet is proven on the version table, never on a mainnet key** (R11). No `xpub`, `ypub`
or `zpub` string exists in this file or is assembled by it. The table is asserted as
integers, and the parser's use of the table is proven with the three test-network prefixes.

Every key here comes from `tests/extended_key_vectors.py`, whose docstring records where each
one was published and how it was converted.
"""

from __future__ import annotations

import dataclasses
import re
import string
import time
import unicodedata
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from portfolio.domain.addresses import AddressInvalidError, AddressRejection
from portfolio.domain.extended_keys import (
    ALL_EXTENDED_KEY_PREFIXES,
    EXTENDED_PUBLIC_KEY_VERSIONS,
    MULTISIG_PUBLIC_PREFIXES,
    PRIVATE_KEY_PREFIXES,
    PRIVATE_KEY_RUN_PATTERN,
    SINGLE_SIG_PUBLIC_PREFIXES,
    ExtendedPublicKey,
    NetworkFamily,
    ScriptType,
    canonical_extended_key,
    derive_child,
    looks_like_private_key,
    parse_extended_public_key,
)
from tests.address_vectors import BASE58_ALPHABET, SYNTHETIC_TPUB
from tests.extended_key_forms import (
    TEST_PRIVATE_PREFIXES,
    VPUB_VERSION,
    depth_zero,
    payload_of,
    private_key_shaped_run,
    reserialised,
    with_run_inside,
)
from tests.extended_key_vectors import (
    BIP32_ALL,
    BIP32_TV1_M,
    BIP49_ACCOUNT_UPUB,
    BIP84_ACCOUNT_VPUB,
    BIP84_ROOT_VPUB,
    DERIVED_UNKNOWN_VERSION_TPUB,
    DERIVED_UPUB_MULTISIG,
    DERIVED_VPUB_MULTISIG,
    MULTISIG_PREFIXES,
    PRIVATE_PREFIXES,
    R2_VERSIONS,
    SINGLE_SIG_PREFIXES,
    TV5_DEPTH_ZERO_WITH_FINGERPRINT,
    TV5_DEPTH_ZERO_WITH_INDEX,
    TV5_PUBKEY_NOT_ON_CURVE,
    TV5_PUBKEY_PREFIX_01,
    TV5_PUBKEY_PREFIX_04,
    TV5_PUBKEY_VERSION_PRVKEY_DATA,
    ParsedShape,
    short,
)

#: The fields of BIP-32 test vector 1's master key, as the independent script decoded them.
#: The public key and chain code of TV1's master are also widely republished.
TV1_MASTER_CHAIN_CODE: Final = "873dff81c02f525623fd1fe5167eac3a55a049de3d314bb42ee227ffed37d508"
TV1_MASTER_PUBLIC_KEY: Final = "0339a36013301597daef41fbe593a02cc513d0b55527ec2df1050e2e8ff49c85c2"

#: BIP-84's account key (as `vpub`), decoded by the independent script.
BIP84_ACCOUNT_FINGERPRINT: Final = "7ef32bdb"
BIP84_ACCOUNT_CHAIN_CODE: Final = "4a53a0ab21b9dc95869c4e92a161194e03c0ef3ff5014ac692f433c4765490fc"
BIP84_ACCOUNT_PUBLIC_KEY: Final = (
    "02707a62fdacc26ea9b63b1c197906f56ee0180d0bcf1966e1a2da34f5f3a09a9b"
)

#: Every valid key in the vector module, for the sweeps.
VALID_KEYS: Final[tuple[str, ...]] = (
    *(shape.key for shape in BIP32_ALL),
    BIP49_ACCOUNT_UPUB,
    BIP84_ROOT_VPUB,
    BIP84_ACCOUNT_VPUB,
)


def refusal(raw: str) -> AddressRejection:
    with pytest.raises(AddressInvalidError) as caught:
        parse_extended_public_key(raw)
    return caught.value.reason


# --------------------------------------------------------------------------------------
# The R2 table: mainnet proven here, as integers
# --------------------------------------------------------------------------------------


def test_the_version_table_is_exactly_r2() -> None:
    """All six versions, as integers, with their prefix, network family and script type."""
    table = {
        version: (entry.prefix, entry.network_family, entry.script_type)
        for version, entry in EXTENDED_PUBLIC_KEY_VERSIONS.items()
    }
    expected = {
        version: (prefix, NetworkFamily(family), ScriptType(script))
        for prefix, (version, family, script) in R2_VERSIONS.items()
    }
    assert table == expected
    assert set(EXTENDED_PUBLIC_KEY_VERSIONS) == {
        0x0488B21E,
        0x049D7CB2,
        0x04B24746,
        0x043587CF,
        0x044A5262,
        0x045F1CF6,
    }


def test_the_enums_have_the_spelled_values() -> None:
    assert {member.value for member in NetworkFamily} == {"main", "test"}
    assert {member.value for member in ScriptType} == {"p2pkh", "p2sh-p2wpkh", "p2wpkh"}


def test_the_prefix_tuples_are_exactly_r2() -> None:
    assert tuple(SINGLE_SIG_PUBLIC_PREFIXES) == SINGLE_SIG_PREFIXES
    assert tuple(MULTISIG_PUBLIC_PREFIXES) == MULTISIG_PREFIXES
    assert set(PRIVATE_KEY_PREFIXES) == set(PRIVATE_PREFIXES)
    assert len(PRIVATE_KEY_PREFIXES) == 10
    assert set(ALL_EXTENDED_KEY_PREFIXES) == {
        *SINGLE_SIG_PREFIXES,
        *MULTISIG_PREFIXES,
        *PRIVATE_PREFIXES,
    }
    assert len(ALL_EXTENDED_KEY_PREFIXES) == 20
    assert {entry.prefix for entry in EXTENDED_PUBLIC_KEY_VERSIONS.values()} == set(
        SINGLE_SIG_PREFIXES
    )


# --------------------------------------------------------------------------------------
# Each test-network prefix parses
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("shape", BIP32_ALL, ids=[shape.id for shape in BIP32_ALL])
def test_every_bip32_vector_parses_as_a_tpub(shape: ParsedShape) -> None:
    key = parse_extended_public_key(shape.key)

    assert isinstance(key, ExtendedPublicKey)
    assert key.network_family is NetworkFamily.TEST
    assert key.script_type is ScriptType.P2PKH
    assert key.depth == shape.depth
    assert key.child_number == shape.child_number
    assert len(key.parent_fingerprint) == 4
    assert len(key.chain_code) == 32
    assert len(key.public_key) == 33
    assert key.public_key[0] in (2, 3)


def test_the_fields_of_a_depth_zero_tpub() -> None:
    key = parse_extended_public_key(BIP32_TV1_M)

    assert key.depth == 0
    assert key.parent_fingerprint == bytes(4)
    assert key.child_number == 0
    assert key.chain_code.hex() == TV1_MASTER_CHAIN_CODE
    assert key.public_key.hex() == TV1_MASTER_PUBLIC_KEY


def test_the_fields_of_a_depth_three_vpub() -> None:
    """R4: a BIP-44-style account key, depth 3, hardened child number. Not enforced."""
    key = parse_extended_public_key(BIP84_ACCOUNT_VPUB)

    assert key.network_family is NetworkFamily.TEST
    assert key.script_type is ScriptType.P2WPKH
    assert key.depth == 3
    assert key.child_number == 0x80000000
    assert key.parent_fingerprint.hex() == BIP84_ACCOUNT_FINGERPRINT
    assert key.chain_code.hex() == BIP84_ACCOUNT_CHAIN_CODE
    assert key.public_key.hex() == BIP84_ACCOUNT_PUBLIC_KEY


def test_a_upub_is_p2sh_p2wpkh_on_the_test_network() -> None:
    key = parse_extended_public_key(BIP49_ACCOUNT_UPUB)

    assert key.network_family is NetworkFamily.TEST
    assert key.script_type is ScriptType.P2SH_P2WPKH
    assert key.depth == 3
    assert key.child_number == 0x80000000


def test_a_depth_zero_vpub_parses() -> None:
    key = parse_extended_public_key(BIP84_ROOT_VPUB)

    assert key.script_type is ScriptType.P2WPKH
    assert key.depth == 0
    assert key.parent_fingerprint == bytes(4)
    assert key.child_number == 0


def test_an_extended_public_key_is_immutable() -> None:
    key = parse_extended_public_key(BIP32_TV1_M)
    with pytest.raises(dataclasses.FrozenInstanceError):
        key.depth = 1  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# Refusals by prefix, before any decoding
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", PRIVATE_PREFIXES)
def test_every_private_prefix_is_refused_as_a_private_key(prefix: str) -> None:
    assert refusal(short(prefix)) is AddressRejection.PRIVATE_KEY


@pytest.mark.parametrize("prefix", PRIVATE_PREFIXES)
def test_a_private_prefix_is_refused_whatever_follows_it(prefix: str) -> None:
    """Before decoding: characters outside base58, and nothing at all, still name it."""
    assert refusal(prefix) is AddressRejection.PRIVATE_KEY
    assert refusal(f"{prefix}0OIl") is AddressRejection.PRIVATE_KEY


def test_a_short_tprv_is_refused_as_a_private_key() -> None:
    """The case the tech lead named: a `tprv` with a typo is still a private key."""
    assert refusal("tprv8ZgxMBicQKsP") is AddressRejection.PRIVATE_KEY


@pytest.mark.parametrize("prefix", MULTISIG_PREFIXES)
def test_every_multisig_prefix_is_refused_by_name(prefix: str) -> None:
    assert refusal(short(prefix)) is AddressRejection.EXTENDED_KEY_MULTISIG
    assert refusal(f"{prefix}0OIl") is AddressRejection.EXTENDED_KEY_MULTISIG


@pytest.mark.parametrize(
    "raw",
    [DERIVED_UPUB_MULTISIG, DERIVED_VPUB_MULTISIG],
    ids=["Upub", "Vpub"],
)
def test_a_well_formed_test_network_multisig_key_is_refused_by_name(raw: str) -> None:
    """With a correct checksum, so the refusal is the multisig rule rather than a decode error."""
    assert refusal(raw) is AddressRejection.EXTENDED_KEY_MULTISIG


@pytest.mark.parametrize("prefix", ["XPUB", "Tpub", "TPUB", "Xprv", "TPRV", "Xpub"])
def test_a_prefix_is_matched_in_its_own_case(prefix: str) -> None:
    """Base58 is case-sensitive, so `TPUB` is not `tpub`, and `Xprv` is none of the ten
    private spellings R2 names. Each reaches the decoder, which finds eight characters."""
    assert refusal(short(prefix)) is AddressRejection.MALFORMED


# --------------------------------------------------------------------------------------
# Refusals after decoding
# --------------------------------------------------------------------------------------


def test_a_corrupted_checksum_is_refused() -> None:
    replacement = "2" if BIP32_TV1_M[-1] != "2" else "3"
    corrupted = BIP32_TV1_M[:-1] + replacement
    assert refusal(corrupted) is AddressRejection.BAD_CHECKSUM


def test_one_appended_character_is_still_82_bytes_and_fails_the_checksum() -> None:
    """The order the spec gives: exactly 82 bytes first, then the checksum."""
    assert refusal(BIP32_TV1_M + "2") is AddressRejection.BAD_CHECKSUM


def test_an_unknown_version_with_a_tpub_prefix_is_refused_on_its_version() -> None:
    """Version 0x043587D0 renders as `tpub` with a correct checksum. The table refuses it."""
    assert DERIVED_UNKNOWN_VERSION_TPUB.startswith("tpub")
    assert refusal(DERIVED_UNKNOWN_VERSION_TPUB) is AddressRejection.UNKNOWN_VERSION_BYTE


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(BIP32_TV1_M[:-1], id="one character short"),
        pytest.param(BIP32_TV1_M[:60], id="truncated"),
        # Two characters, not one: a tpub's payload starts 0x04, and 0x04 * 58 plus a carry
        # is still one byte, so one appended character keeps 82 bytes and fails the checksum
        # instead (see the checksum test below). Two multiply past 256.
        pytest.param(BIP32_TV1_M + "22", id="two characters long"),
        pytest.param(short("tpub"), id="short"),
        pytest.param("tpub", id="prefix only"),
    ],
)
def test_a_key_of_the_wrong_length_is_malformed(raw: str) -> None:
    assert refusal(raw) is AddressRejection.MALFORMED


def test_a_character_outside_base58_is_refused() -> None:
    corrupted = BIP32_TV1_M[:20] + "0" + BIP32_TV1_M[21:]
    assert refusal(corrupted) is AddressRejection.INVALID_CHARACTER


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(TV5_PUBKEY_VERSION_PRVKEY_DATA, id="private key data under a public version"),
        pytest.param(TV5_PUBKEY_PREFIX_04, id="prefix 04"),
        pytest.param(TV5_PUBKEY_PREFIX_01, id="prefix 01"),
        pytest.param(TV5_PUBKEY_NOT_ON_CURVE, id="x with no point on the curve"),
        pytest.param(SYNTHETIC_TPUB, id="a hash where the key should be"),
    ],
)
def test_a_key_that_is_not_a_curve_point_is_refused(raw: str) -> None:
    """BIP-32 test vector 5's public cases, re-versioned to `tpub` with the defect kept."""
    assert refusal(raw) is AddressRejection.INVALID_PUBLIC_KEY


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(TV5_DEPTH_ZERO_WITH_FINGERPRINT, id="non-zero parent fingerprint"),
        pytest.param(TV5_DEPTH_ZERO_WITH_INDEX, id="non-zero child number"),
    ],
)
def test_the_depth_zero_rules_are_enforced(raw: str) -> None:
    """R4: depth is not enforced, but a depth-0 key must look like a master key."""
    assert refusal(raw) is AddressRejection.MALFORMED


def test_every_new_reason_has_a_fixed_sentence_that_quotes_nothing() -> None:
    from portfolio.domain.addresses import REJECTION_MESSAGES

    for reason in (
        AddressRejection.PRIVATE_KEY,
        AddressRejection.EXTENDED_KEY_MULTISIG,
        AddressRejection.INVALID_PUBLIC_KEY,
    ):
        message = REJECTION_MESSAGES[reason]
        assert message.endswith(".")
        assert "{" not in message
        assert "%" not in message
    assert AddressRejection.PRIVATE_KEY.value == "private_key"
    assert AddressRejection.EXTENDED_KEY_MULTISIG.value == "extended_key_multisig"
    assert AddressRejection.INVALID_PUBLIC_KEY.value == "invalid_public_key"


@pytest.mark.parametrize(
    "raw",
    [
        TV5_PUBKEY_PREFIX_04,
        TV5_DEPTH_ZERO_WITH_INDEX,
        DERIVED_UNKNOWN_VERSION_TPUB,
        DERIVED_VPUB_MULTISIG,
        BIP32_TV1_M[:-1] + ("2" if BIP32_TV1_M[-1] != "2" else "3"),
        short("tprv"),
    ],
    ids=["point", "depth", "version", "multisig", "checksum", "private"],
)
def test_no_refusal_quotes_the_key(raw: str) -> None:
    with pytest.raises(AddressInvalidError) as caught:
        parse_extended_public_key(raw)

    rendered = f"{caught.value} {caught.value.args!r} {caught.value.message}"
    assert raw not in rendered
    assert raw[4:16] not in rendered


# --------------------------------------------------------------------------------------
# A corruption never parses as a different valid key
# --------------------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(data=st.data())
def test_a_single_substitution_never_parses_as_a_different_valid_key(data: st.DataObject) -> None:
    """Base58Check's four bytes make a surviving substitution a one-in-four-billion event.

    The independent script that produced the vectors swept every single-character
    substitution of every valid key here and found none that verified; this samples the same
    space through the parser itself.
    """
    original = data.draw(st.sampled_from(VALID_KEYS))
    position = data.draw(st.integers(min_value=0, max_value=len(original) - 1))
    replacement = data.draw(
        st.sampled_from(BASE58_ALPHABET).filter(lambda c: c != original[position])
    )
    corrupted = original[:position] + replacement + original[position + 1 :]

    try:
        parsed = parse_extended_public_key(corrupted)
    except AddressInvalidError:
        return
    assert parsed == parse_extended_public_key(original), "a typo parsed as another key"


@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_a_transposition_never_parses_as_a_different_valid_key(data: st.DataObject) -> None:
    original = data.draw(st.sampled_from(VALID_KEYS))
    position = data.draw(st.integers(min_value=4, max_value=len(original) - 2))
    swapped = (
        original[:position] + original[position + 1] + original[position] + original[position + 2 :]
    )
    if swapped == original:
        return
    with pytest.raises(AddressInvalidError):
        parse_extended_public_key(swapped)


def test_the_parser_does_not_strip_whitespace() -> None:
    """`classify_wallet_key` strips; the parser takes the string as it is."""
    with pytest.raises(AddressInvalidError):
        parse_extended_public_key(f" {BIP32_TV1_M}")
    with pytest.raises(AddressInvalidError):
        parse_extended_public_key(f"{BIP32_TV1_M}\n")


# --------------------------------------------------------------------------------------
# The canonical form (review finding S1)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", VALID_KEYS)
def test_the_canonical_form_is_the_key_at_depth_zero(key: str) -> None:
    """Checked against `tests/extended_key_forms.py`, which builds it from the byte layout."""
    assert canonical_extended_key(parse_extended_public_key(key)) == depth_zero(key)


@pytest.mark.parametrize("key", VALID_KEYS)
def test_the_canonical_form_parses_again_to_the_same_derivation_inputs(key: str) -> None:
    original = parse_extended_public_key(key)
    canonical = parse_extended_public_key(canonical_extended_key(original))

    assert (canonical.depth, canonical.parent_fingerprint, canonical.child_number) == (
        0,
        bytes(4),
        0,
    )
    assert (canonical.network_family, canonical.script_type) == (
        original.network_family,
        original.script_type,
    )
    assert (canonical.chain_code, canonical.public_key) == (
        original.chain_code,
        original.public_key,
    )
    assert canonical_extended_key(canonical) == canonical_extended_key(original), "idempotent"


def test_the_canonical_form_keeps_the_version_and_the_key_bytes() -> None:
    canonical = canonical_extended_key(parse_extended_public_key(BIP84_ACCOUNT_VPUB))

    before, after = payload_of(BIP84_ACCOUNT_VPUB), payload_of(canonical)
    assert after[:4] == before[:4] == VPUB_VERSION.to_bytes(4, "big")
    assert after[4:13] == bytes(9)
    assert after[13:] == before[13:]
    assert before[4] == 3, "the account key is at depth 3, so the form really moved"


def test_a_reserialised_export_derives_the_same_children() -> None:
    """Why S1 is right: the position bytes play no part in public derivation."""
    account = parse_extended_public_key(BIP84_ACCOUNT_VPUB)
    other = parse_extended_public_key(
        reserialised(BIP84_ACCOUNT_VPUB, position=bytes([1]) + bytes(4) + (7).to_bytes(4, "big"))
    )

    for branch in (0, 1):
        mine, theirs = derive_child(account, branch), derive_child(other, branch)
        assert mine is not None
        assert theirs is not None
        assert mine.public_key == theirs.public_key
        assert mine.chain_code == theirs.chain_code


# --------------------------------------------------------------------------------------
# The repr carries nothing derivation reads (review finding N5)
# --------------------------------------------------------------------------------------


def renderings(value: bytes) -> tuple[str, ...]:
    return (repr(value), value.hex(), value.hex().upper(), str(value))


def test_the_repr_of_a_parsed_key_leaves_out_its_chain_code_and_public_key() -> None:
    parsed = parse_extended_public_key(BIP84_ACCOUNT_VPUB)
    shown = repr(parsed)

    for hidden in (parsed.chain_code, parsed.public_key):
        for form in renderings(hidden):
            assert form not in shown
    assert "chain_code" not in shown
    assert "public_key" not in shown
    assert BIP84_ACCOUNT_CHAIN_CODE not in shown
    assert BIP84_ACCOUNT_PUBLIC_KEY not in shown
    # Still useful: the fields that say where the key sits are there.
    assert "depth=3" in shown


def test_the_repr_of_a_derived_key_leaves_them_out_too() -> None:
    branch = derive_child(parse_extended_public_key(BIP84_ACCOUNT_VPUB), 0)
    assert branch is not None
    shown = repr(branch)

    for hidden in (branch.chain_code, branch.public_key):
        for form in renderings(hidden):
            assert form not in shown
    assert "chain_code" not in shown
    assert "public_key" not in shown
    assert "index=0" in shown


def test_equality_still_compares_the_hidden_fields() -> None:
    """`repr=False` hides a field from the repr only: two different keys are still unequal."""
    vpub = parse_extended_public_key(BIP84_ACCOUNT_VPUB)
    tv1 = parse_extended_public_key(BIP32_TV1_M)
    same_position_other_key = dataclasses.replace(vpub, public_key=tv1.public_key)

    assert same_position_other_key != vpub
    assert repr(same_position_other_key) == repr(vpub)


# --------------------------------------------------------------------------------------
# What counts as a private key (R2b, review finding D1, with the left boundary)
# --------------------------------------------------------------------------------------

#: Unicode format characters (`Cf`), from their code points so the source shows them.
FORMAT_CHARACTERS: Final = tuple(chr(point) for point in (0x200B, 0x2060, 0x200E, 0xFEFF, 0xAD))
ZERO_WIDTH_SPACE: Final = chr(0x200B)

#: The run pattern with its length floor lowered to one, so prefixes are proven on short text.
SHORT_RUN: Final = re.compile(PRIVATE_KEY_RUN_PATTERN.pattern.replace("{100,}", "{1,}"))


def test_the_run_pattern_is_exactly_the_ruling() -> None:
    assert PRIVATE_KEY_RUN_PATTERN.pattern == (
        r"(?<![1-9A-HJ-NP-Za-km-z])(?:[xyztuv]|[YZUV])prv[1-9A-HJ-NP-Za-km-z]{100,}"
    )
    assert not PRIVATE_KEY_RUN_PATTERN.flags & re.IGNORECASE
    assert SHORT_RUN.pattern != PRIVATE_KEY_RUN_PATTERN.pattern


@pytest.mark.parametrize("letter", ["x", "y", "z", "t", "u", "v", "Y", "Z", "U", "V"])
def test_the_run_pattern_knows_every_private_prefix_mainnet_included(letter: str) -> None:
    assert SHORT_RUN.search(f"{letter}prva") is not None


@pytest.mark.parametrize("letter", ["X", "T", "w", "q", "a", "W"])
def test_the_run_pattern_knows_no_other_prefix(letter: str) -> None:
    assert SHORT_RUN.search(f"{letter}prva") is None


def test_the_run_pattern_and_the_prefix_table_name_the_same_ten_prefixes() -> None:
    """Exhaustive over every ASCII letter and digit, so the two can never drift apart."""
    known = {
        f"{letter}prv"
        for letter in string.ascii_letters + string.digits
        if SHORT_RUN.search(f" {letter}prva") is not None
    }

    assert known == set(PRIVATE_KEY_PREFIXES)


@pytest.mark.parametrize("letter", ["x", "y", "z", "t", "u", "v", "Y", "Z", "U", "V"])
def test_the_run_pattern_does_not_start_inside_base58_text(letter: str) -> None:
    assert SHORT_RUN.search(f" {letter}prva") is not None
    assert SHORT_RUN.search(f'"{letter}prva') is not None
    assert SHORT_RUN.search(f"a{letter}prva") is None
    assert SHORT_RUN.search(f"5{letter}prva") is None


def test_the_run_pattern_is_about_private_keys_not_public_ones() -> None:
    for prefix in (*SINGLE_SIG_PREFIXES, *MULTISIG_PREFIXES):
        assert SHORT_RUN.search(f"{prefix}a") is None


@pytest.mark.parametrize("outside", ["0", "O", "I", "l"])
def test_the_run_is_base58_and_a_character_outside_it_ends_it(outside: str) -> None:
    assert SHORT_RUN.search(f"tprv{outside}") is None


@pytest.mark.parametrize("prefix", TEST_PRIVATE_PREFIXES)
@pytest.mark.parametrize(
    "around",
    [
        pytest.param("{}", id="alone"),
        pytest.param('"{}"', id="in quotes"),
        pytest.param("my key: {}", id="after text"),
        pytest.param("{} is the one", id="before text"),
        pytest.param("line one\n{}", id="after a newline"),
        pytest.param("({})", id="in brackets"),
    ],
)
def test_a_run_of_100_is_a_private_key_anywhere(prefix: str, around: str) -> None:
    run = private_key_shaped_run(prefix)
    assert len(run) == 104

    assert looks_like_private_key(around.format(run))


@pytest.mark.parametrize("prefix", TEST_PRIVATE_PREFIXES)
def test_a_run_of_99_behind_text_is_not(prefix: str) -> None:
    short_run = private_key_shaped_run(prefix, 99)

    assert not looks_like_private_key(f"my key: {short_run}")
    assert not looks_like_private_key(f'"{short_run}"')
    # At the start it is still a private prefix: that is the first rule, not the run.
    assert looks_like_private_key(short_run)


def test_the_run_needs_a_left_boundary() -> None:
    """The boundary: start, a space, a quote -- and nothing Base58 glued in front."""
    run = private_key_shaped_run("uprv")

    assert PRIVATE_KEY_RUN_PATTERN.search(run) is not None
    assert PRIVATE_KEY_RUN_PATTERN.search(f" {run}") is not None
    assert PRIVATE_KEY_RUN_PATTERN.search(f'"{run}') is not None
    assert PRIVATE_KEY_RUN_PATTERN.search(f"abc{run}") is None
    assert PRIVATE_KEY_RUN_PATTERN.search(f"0{run}") is not None, "0 is not Base58"


def test_a_run_glued_onto_base58_text_is_accepted_by_design() -> None:
    """The one case R2b gives up, so that a public key is never read as a private one."""
    run = private_key_shaped_run("tprv")

    assert not looks_like_private_key(f"abc{run}")
    assert not looks_like_private_key(f"my key:abc{run}")


def test_a_stripped_format_character_counts_as_nothing_on_either_side() -> None:
    """The run is searched with the `Cf` characters removed: what they separated is joined."""
    run = private_key_shaped_run("vprv")

    assert looks_like_private_key(f"my key: {ZERO_WIDTH_SPACE}{run}")
    assert looks_like_private_key(f"my key:{ZERO_WIDTH_SPACE}{run}")
    # Removed, it glues the run onto Base58 text: the documented give-up, consistently.
    assert not looks_like_private_key(f"my key abc{ZERO_WIDTH_SPACE}{run}")


def test_a_format_character_inside_the_body_does_not_split_the_run() -> None:
    run = private_key_shaped_run("vprv")
    split = f"{run[:54]}{ZERO_WIDTH_SPACE}{run[54:]}"

    assert PRIVATE_KEY_RUN_PATTERN.search(split) is None, "neither half is 100 long"
    assert looks_like_private_key(f"my key: {split}")
    short_run = private_key_shaped_run("vprv", 99)
    assert not looks_like_private_key(f"my key: {short_run[:50]}{ZERO_WIDTH_SPACE}{short_run[50:]}")


@pytest.mark.parametrize(
    "character", FORMAT_CHARACTERS, ids=[f"U+{ord(c):04X}" for c in FORMAT_CHARACTERS]
)
def test_a_private_prefix_behind_a_format_character_is_still_at_the_start(character: str) -> None:
    assert looks_like_private_key(f"{character}xprv8Zgx")
    assert looks_like_private_key(f" \t{character}Yprv8Zgx")
    assert looks_like_private_key(f"{character}{character}tprv8Zgx")


@pytest.mark.parametrize("prefix", PRIVATE_PREFIXES)
def test_every_short_private_prefix_at_the_start_is_one(prefix: str) -> None:
    assert looks_like_private_key(short(prefix))
    assert looks_like_private_key(f"  {short(prefix)}\n")


def test_a_short_private_prefix_behind_text_is_not_one() -> None:
    """The ruling looks at the start for the short form; behind text it needs the run."""
    assert not looks_like_private_key("Savings uprv8Zgx more")


@pytest.mark.parametrize("at", [5, 6, 7])
def test_a_valid_public_key_with_a_run_inside_its_body_is_not_a_private_key(at: int) -> None:
    """The false positive the boundary removes: `uprv` and 100 more characters inside a vpub.

    Built in memory by `with_run_inside`, which keeps the result a well-formed key on the
    curve, so it parses; before the boundary it was refused as `private_key`.
    """
    key = with_run_inside(BIP84_ACCOUNT_VPUB, "uprv", at)
    assert key.startswith("vpub")
    assert key[at : at + 4] == "uprv"
    assert len(key) - (at + 4) >= 100, "the run inside is long enough to have matched"

    assert not looks_like_private_key(key)
    assert parse_extended_public_key(key).script_type is ScriptType.P2WPKH


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "Savings",
        *VALID_KEYS,
        *(short(prefix) for prefix in (*SINGLE_SIG_PREFIXES, *MULTISIG_PREFIXES)),
    ],
)
def test_nothing_that_is_not_a_private_key_looks_like_one(value: str) -> None:
    assert not looks_like_private_key(value)


def test_a_megabyte_of_ordinary_text_is_answered_promptly() -> None:
    started = time.perf_counter()

    assert not looks_like_private_key("a" * 1_000_000)
    assert not looks_like_private_key("tpr" + "a" * 1_000_000)
    assert not looks_like_private_key(" " + "prv" * 300_000)

    assert time.perf_counter() - started < 2.0


def test_no_ascii_character_is_a_format_character() -> None:
    """The premise of the ASCII fast path: skipping the `Cf` removal changes nothing."""
    assert [point for point in range(128) if unicodedata.category(chr(point)) == "Cf"] == []
