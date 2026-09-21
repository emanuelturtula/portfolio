"""Criteria 2, 3 and 8: the codecs verify checksums, and say so without quoting the input.

These tests are pure. No fixture, no database, no clock, no network -- which is the
property the whole issue turns on, because "validation never costs a round-trip" is what
lets a mistyped address be refused at the moment it is pasted rather than an hour later
when a balance read comes back empty.

## The one assertion that matters

A validator that checks the prefix, the length and the alphabet passes every happy-path
test here and still accepts a typo. So for every valid vector this module corrupts **every
character, to every other character of that address's own alphabet**, and requires all of
them to be refused -- roughly twenty thousand strings. Bech32 guarantees detection of up
to four substitutions, and Base58Check's four-byte checksum makes a survivor a
one-in-four-billion event, so a single survivor means a checksum is not being computed.

The vectors and their provenance live in `tests/address_vectors.py`; none of them was
produced by the code under test.
"""

from __future__ import annotations

import string
from typing import TYPE_CHECKING, Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from portfolio.domain.addresses import (
    BECH32_CHARSET,
    AddressInvalidError,
    AddressRejection,
    ValidatedAddress,
    _kaspa_checksum,
    bech32_decode,
)
from portfolio.domain.chains import ChainKey, validate_address
from tests.address_vectors import (
    BASE58_VECTORS,
    BECH32_VECTORS,
    BIP173_MIXED_CASE,
    BIP173_NON_ZERO_PADDING,
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WPKH_UPPERCASE,
    BIP173_UNKNOWN_HRP,
    BIP350_MIXED_CASE,
    BIP350_NON_ZERO_PADDING,
    BIP350_TESTNET_V1,
    BIP350_UNKNOWN_HRP,
    BIP350_V0_WITH_BECH32M,
    BIP350_V2_WITH_BECH32,
    BITCOIN_VECTORS,
    BLANK_INPUTS,
    CORE_INVALID_BASE58,
    CORE_INVALID_SHORT_PROGRAM,
    CORE_SIGNET_P2PKH,
    CORE_TESTNET4_P2SH,
    CORE_UNKNOWN_VERSION_BYTE,
    DERIVED_BAD_WITNESS_VERSION,
    DERIVED_EMPTY_DATA_SECTION,
    DERIVED_OVER_BECH32_LENGTH_LIMIT,
    DERIVED_PROGRAM_TOO_LONG,
    DERIVED_PROGRAM_TOO_SHORT,
    DERIVED_V0_WRONG_PROGRAM_LENGTH,
    DERIVED_V1_WITH_BECH32,
    HOMOGLYPH_KELVIN,
    KASPA_DATA_TOO_SHORT,
    KASPA_MIXED_CASE,
    KASPA_NAMED_CORRUPTIONS,
    KASPA_NO_PREFIX,
    KASPA_OVERLONG_PAYLOAD,
    KASPA_PAYLOAD_TOO_SHORT,
    KASPA_POLYMOD_VECTORS,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V1_KEY,
    KASPA_TESTNET_V1_ZERO,
    KASPA_UNKNOWN_PREFIX,
    KASPA_UNKNOWN_VERSION_BYTE,
    KASPA_VECTORS,
    KASPA_WRONG_NETWORK_PREFIX,
    KASPA_WRONG_PAYLOAD_LENGTH,
    KELVIN_SIGN,
    NAMED_CORRUPTIONS,
    SYNTHETIC_TPUB,
    TWO_HUNDRED_CHARACTERS,
    Vector,
    corruptions_of,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

BITCOIN: Final = ChainKey.BITCOIN
KASPA: Final = ChainKey.KASPA


def reject(address: str, chain: ChainKey = BITCOIN) -> AddressInvalidError:
    """Validate, require a refusal, and hand back the error for further assertions."""
    with pytest.raises(AddressInvalidError) as caught:
        validate_address(chain, address)
    return caught.value


def outcome_of(chain: ChainKey, raw: str) -> ValidatedAddress | AddressInvalidError:
    """Whichever of the two legitimate answers the domain gave, as a value.

    The property tests below feed in generated text, for which *both* answers are correct
    and the claim being made is that there is no third one -- no `IndexError` from a codec
    indexing a string it did not measure, no `UnicodeDecodeError`, nothing that becomes an
    unhandled exception and a 500 where the contract promises a 422.

    Returning the exception rather than asserting inside an `except` block is not a style
    preference: an assertion in an `except` block reads as though the exception were the
    expected outcome, which here it is not.
    """
    try:
        return validate_address(chain, raw)
    except AddressInvalidError as error:
        return error


def ids_of(vectors: Iterable[Vector]) -> list[str]:
    return [vector.id for vector in vectors]


def expected_canonical(address: str) -> str:
    """The canonical form the vector table records for an address.

    Looked up rather than computed. `address.lower()` would be right for bech32 and wrong
    for Base58Check, and writing it that way once made four of these tests assert the very
    corruption they exist to forbid.
    """
    for vector in (*BITCOIN_VECTORS, *KASPA_VECTORS):
        if vector.address == address:
            return vector.canonical
    raise AssertionError(f"{address!r} is not a vector in tests/address_vectors.py")


# --------------------------------------------------------------------------------------
# Criterion 2: a valid address is accepted, and stored in both forms
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("vector", BITCOIN_VECTORS, ids=ids_of(BITCOIN_VECTORS))
def test_a_published_valid_vector_is_accepted(vector: Vector) -> None:
    """Every published testnet vector round-trips into the two forms the schema stores."""
    validated = validate_address(BITCOIN, vector.address)

    assert isinstance(validated, ValidatedAddress)
    assert validated.canonical == vector.canonical
    assert validated.display == vector.display


def test_bech32_uppercase_canonicalises_and_preserves_display() -> None:
    """Criterion 2: the same address typed in capitals is the same address.

    Bech32 is case insensitive, and some wallets render an address in upper case because
    it makes for a denser QR code. The canonical form is what the unique constraint and
    the provider calls use, so it has to be the lower case one; the display form is what
    the user has to be able to recognise, so it has to be what they pasted.
    """
    upper = validate_address(BITCOIN, BIP173_TESTNET_P2WPKH_UPPERCASE)
    lower = validate_address(BITCOIN, BIP173_TESTNET_P2WPKH)

    assert upper.display == BIP173_TESTNET_P2WPKH_UPPERCASE
    assert upper.display != upper.canonical
    assert upper.canonical == BIP173_TESTNET_P2WPKH
    # The whole point: two renderings of one address collide on the canonical column.
    assert upper.canonical == lower.canonical
    assert lower.display == BIP173_TESTNET_P2WPKH


def test_base58check_is_case_sensitive_and_unchanged() -> None:
    """Criterion 2: a legacy address must reach the database byte for byte.

    Base58Check encodes the hash directly, so `1A` and `1a` name different scripts.
    Lower-casing one on the way in -- the obvious thing to do if bech32 taught you that
    case does not matter -- silently stores an address nobody owns. The canonical form of
    a base58 address is therefore the input, unchanged.
    """
    validated = validate_address(BITCOIN, CORE_SIGNET_P2PKH)

    assert validated.canonical == CORE_SIGNET_P2PKH
    assert validated.display == CORE_SIGNET_P2PKH
    # Not merely "not lowercased": the vector really does contain both cases, so an
    # implementation that lowercased it would produce a different string.
    assert CORE_SIGNET_P2PKH.lower() != CORE_SIGNET_P2PKH

    # And the case-flipped strings are different addresses, so they must be refused.
    assert reject(CORE_SIGNET_P2PKH.lower()).reason is AddressRejection.BAD_CHECKSUM
    assert reject(CORE_TESTNET4_P2SH.upper()).reason in {
        AddressRejection.BAD_CHECKSUM,
        AddressRejection.INVALID_CHARACTER,
        AddressRejection.MALFORMED,
    }


@pytest.mark.parametrize("vector", BASE58_VECTORS, ids=ids_of(BASE58_VECTORS))
def test_base58_canonical_and_display_are_the_input_untouched(vector: Vector) -> None:
    """The asymmetry with bech32, asserted on every legacy vector rather than just one."""
    validated = validate_address(BITCOIN, vector.address)

    assert validated.canonical == vector.address
    assert validated.display == vector.address


@pytest.mark.parametrize("vector", BECH32_VECTORS, ids=ids_of(BECH32_VECTORS))
def test_bech32_canonical_is_always_lower_case(vector: Vector) -> None:
    """Whatever the user typed, the column the constraint is on holds one spelling."""
    validated = validate_address(BITCOIN, vector.address)

    assert validated.canonical == validated.canonical.lower()
    assert validated.canonical == vector.address.lower()


def test_validation_of_a_canonical_form_is_a_fixed_point() -> None:
    """Re-validating what was stored must not change it again.

    `#6`-`#8` will read the canonical column back and hand it to a provider. If a second
    pass through the validator produced a third string, the address in the database and
    the address being queried would diverge on the first refactor that added one.
    """
    for vector in BITCOIN_VECTORS:
        once = validate_address(BITCOIN, vector.address)
        twice = validate_address(BITCOIN, once.canonical)

        assert twice.canonical == once.canonical, vector.id
        assert twice.display == once.canonical, vector.id


# --------------------------------------------------------------------------------------
# Checksums, not shapes
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("vector", BITCOIN_VECTORS, ids=ids_of(BITCOIN_VECTORS))
def test_every_single_character_corruption_is_rejected(vector: Vector) -> None:
    """The test that tells a checksum from a shape.

    One hand-picked corruption proves that one string is refused. This sweeps every
    position against every other character of the address's own alphabet -- between 1,300
    and 2,000 strings per vector -- and requires all of them to be refused. Replacements
    come from the alphabet the address is already written in, deliberately: a character
    from *outside* it can be refused without computing anything, which is the weaker test.

    A survivor is printed with its position, because "the validator accepts a typo at
    index 27" is a debuggable statement and "some corruption passed" is not.
    """
    survivors: list[tuple[int, str]] = []
    examined = 0
    for position, corrupted in corruptions_of(vector.address, vector.alphabet):
        examined += 1
        try:
            validate_address(BITCOIN, corrupted)
        except AddressInvalidError:
            continue
        survivors.append((position, corrupted))

    assert examined > 1000, "the sweep generated almost nothing, so it proves nothing"
    assert survivors == []


@pytest.mark.parametrize(
    ("name", "original", "corrupted"),
    NAMED_CORRUPTIONS,
    ids=[name for name, _original, _corrupted in NAMED_CORRUPTIONS],
)
def test_a_named_single_character_corruption_is_rejected(
    name: str,
    original: str,
    corrupted: str,
) -> None:
    """The readable companion to the sweep: one typo per vector, refused by checksum."""
    del name  # It is the parametrisation id; the assertion is on the pair.
    assert validate_address(BITCOIN, original).canonical == expected_canonical(original)

    assert reject(corrupted).reason is AddressRejection.BAD_CHECKSUM


@pytest.mark.parametrize(
    ("name", "original", "corrupted"),
    NAMED_CORRUPTIONS,
    ids=[name for name, _original, _corrupted in NAMED_CORRUPTIONS],
)
def test_the_named_corruptions_really_are_single_character(
    name: str,
    original: str,
    corrupted: str,
) -> None:
    """Guard the fixture data itself.

    `test_a_named_single_character_corruption_is_rejected` would pass just as happily if
    an edit turned one of these pairs into "a valid address and a completely different
    string", and it would then prove nothing at all. This is what stops that.
    """
    del name
    assert len(original) == len(corrupted)
    differences = [i for i, (a, b) in enumerate(zip(original, corrupted, strict=True)) if a != b]
    assert len(differences) == 1
    # In the payload, not in the checksum tail: a mistyped payload is what a human makes.
    assert differences[0] < len(original) - 6


# --------------------------------------------------------------------------------------
# Bech32 against bech32m: the bug that ships quietly
# --------------------------------------------------------------------------------------


def test_witness_version_zero_with_a_bech32m_checksum_is_rejected() -> None:
    """BIP-350's own counterexample: everything right except the generator constant."""
    assert reject(BIP350_V0_WITH_BECH32M).reason is AddressRejection.BAD_CHECKSUM


def test_witness_version_one_with_a_bech32_checksum_is_rejected() -> None:
    """The mirror image, and the half the published lists only have for mainnet.

    `DERIVED_V1_WITH_BECH32` is `BIP350_TESTNET_V1` with the same human-readable part, the
    same witness version and the same witness program, re-checksummed with the bech32
    constant instead of the bech32m one -- six characters different and nothing else. An
    implementation that accepts either constant for any version accepts this.
    """
    assert validate_address(BITCOIN, BIP350_TESTNET_V1).canonical == BIP350_TESTNET_V1
    # Compared through a set: mypy narrows two `Final` literals to their values and
    # calls `!=` between them a non-overlapping check.
    assert len({DERIVED_V1_WITH_BECH32, BIP350_TESTNET_V1}) == 2
    assert DERIVED_V1_WITH_BECH32[:-6] == BIP350_TESTNET_V1[:-6]

    assert reject(DERIVED_V1_WITH_BECH32).reason is AddressRejection.BAD_CHECKSUM


def test_witness_version_two_with_a_bech32_checksum_is_rejected() -> None:
    """A published non-zero version carrying the version-0 constant."""
    assert reject(BIP350_V2_WITH_BECH32).reason is AddressRejection.BAD_CHECKSUM


# --------------------------------------------------------------------------------------
# The rest of the shapes the spec names
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [BIP173_MIXED_CASE, BIP350_MIXED_CASE],
    ids=["bip173 lower with one capital", "bip350 upper-case tail"],
)
def test_mixed_case_bech32_is_rejected(address: str) -> None:
    """Bech32 is case insensitive but not case *mixing*: the checksum is defined on one."""
    assert reject(address).reason is AddressRejection.MIXED_CASE


def test_a_unicode_homoglyph_is_rejected() -> None:
    """The case-folding trap the mixed-case guard cannot see.

    U+212A KELVIN SIGN lowercases to `k` and uppercases to itself, so a string containing
    one satisfies `raw.upper() == raw` and is not "mixed case" by any test written in
    terms of that question. It then folds to a valid address and the checksum verifies --
    so every other assertion in this module passes on it.

    What it corrupts is the form the owner is shown. `canonical` is the folded ASCII
    address, so uniqueness and provider calls were never at risk; `display` keeps the
    Kelvin sign, and `GET /api/wallets` hands the owner a string that is not an address on
    any network. Refusing non-ASCII outright is the tractable fix, because every encoding
    here is ASCII by construction and Unicode has more than one character with this
    property -- enumerating them would be an arms race.
    """
    # The properties that make this vector pathological, asserted so the test cannot
    # quietly stop being about a homoglyph if the constant is ever edited.
    assert HOMOGLYPH_KELVIN.upper() == HOMOGLYPH_KELVIN
    assert HOMOGLYPH_KELVIN.lower() == BIP173_TESTNET_P2WPKH
    assert not HOMOGLYPH_KELVIN.isascii()
    assert len(HOMOGLYPH_KELVIN) == len(BIP173_TESTNET_P2WPKH_UPPERCASE)

    assert reject(HOMOGLYPH_KELVIN).reason is AddressRejection.INVALID_CHARACTER


def test_a_homoglyph_in_a_kaspa_address_is_rejected() -> None:
    """The same trap on the other chain, which folds case in the same way."""
    homoglyph = KASPA_TESTNET_V0.upper().replace("K", KELVIN_SIGN, 1)
    assert not homoglyph.isascii()

    assert reject(homoglyph, KASPA).reason is AddressRejection.INVALID_CHARACTER


def test_a_case_flip_of_a_valid_bech32_address_is_rejected() -> None:
    """Derived from a vector rather than published, so it cannot go stale silently."""
    flipped = BIP173_TESTNET_P2WPKH[:10] + BIP173_TESTNET_P2WPKH[10:].upper()

    assert reject(flipped).reason is AddressRejection.MIXED_CASE


@pytest.mark.parametrize(
    "address",
    [BIP173_UNKNOWN_HRP, BIP350_UNKNOWN_HRP],
    ids=["bip173 tc1", "bip350 tc1"],
)
def test_an_unknown_human_readable_part_is_rejected(address: str) -> None:
    """`tc` is not a Bitcoin network. The prefix is an allowlist, not a hint."""
    assert reject(address).reason is AddressRejection.UNKNOWN_PREFIX


@pytest.mark.parametrize(
    "address",
    [BIP173_NON_ZERO_PADDING, BIP350_NON_ZERO_PADDING],
    ids=["bip173", "bip350"],
)
def test_non_zero_padding_in_the_8_to_5_conversion_is_rejected(address: str) -> None:
    """The bits past the end of the witness program have to be zero, or it is not one."""
    assert reject(address).reason in {
        AddressRejection.MALFORMED,
        AddressRejection.BAD_PROGRAM_LENGTH,
    }


def test_a_witness_program_that_is_too_short_is_rejected() -> None:
    assert reject(CORE_INVALID_SHORT_PROGRAM).reason in {
        AddressRejection.BAD_PROGRAM_LENGTH,
        AddressRejection.MALFORMED,
    }


def test_a_witness_version_above_sixteen_is_rejected() -> None:
    """BIP-141 defines versions 0 to 16; 17 is a string that decodes and is not an address.

    The checksum on this vector is *correct*, which is the point: the refusal has to come
    from reading the version, not from the checksum failing first.
    """
    assert reject(DERIVED_BAD_WITNESS_VERSION).reason is AddressRejection.BAD_WITNESS_VERSION


@pytest.mark.parametrize(
    "address",
    [
        DERIVED_V0_WRONG_PROGRAM_LENGTH,
        DERIVED_PROGRAM_TOO_SHORT,
        DERIVED_PROGRAM_TOO_LONG,
        DERIVED_EMPTY_DATA_SECTION,
    ],
    ids=["v0 with 21 bytes", "one byte", "forty-one bytes", "no data at all"],
)
def test_a_witness_program_of_the_wrong_length_is_rejected(address: str) -> None:
    """Four lengths BIP-141 does not define, each with a checksum that verifies.

    The version-0 case is the one that matters most and is the easiest to miss: 20 bytes
    and 32 bytes are the only two lengths version 0 has, and a codec that only enforced
    the general 2-to-40 range would accept the other nineteen.
    """
    assert reject(address).reason is AddressRejection.BAD_PROGRAM_LENGTH


def test_a_bech32_string_over_ninety_characters_is_rejected() -> None:
    """BIP-173 caps an address at 90 characters, and that is not the registry's ceiling.

    The registry refuses anything over 128 characters before a codec sees it. This vector
    sits between the two limits on purpose, so it is the codec's own check that answers --
    a check a 200-character string can never reach.
    """
    assert 90 < len(DERIVED_OVER_BECH32_LENGTH_LIMIT) < 128

    assert reject(DERIVED_OVER_BECH32_LENGTH_LIMIT).reason is AddressRejection.TOO_LONG


# --------------------------------------------------------------------------------------
# The low-level decoders, for the refusals no address can reach
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["qyrz8wqd2c9m", "1qyrz8wqd2c9m"],
    ids=["no separator", "empty human-readable part"],
)
def test_bech32_decode_refuses_a_string_that_is_not_shaped_like_one(raw: str) -> None:
    """Quoted from BIP-350's invalid bech32m list. Neither is an address of any kind.

    `validate_bitcoin_address` cannot route either of these into `bech32_decode` -- its
    discriminator requires a separator with an alphabetic part in front of it, so both go
    to the Base58Check branch and are refused there. The guards are still worth having and
    still worth testing: `bech32_decode` is a public function of the module, and the next
    caller may not come through the discriminator.
    """
    with pytest.raises(AddressInvalidError) as caught:
        bech32_decode(raw)

    assert caught.value.reason is AddressRejection.MALFORMED
    assert raw not in caught.value.message


def test_bech32_decode_refuses_a_character_outside_its_charset() -> None:
    """`b`, `i`, `o` and `1` are the four characters bech32 leaves out of its alphabet.

    Reached directly for the same reason as above: a Bitcoin address carrying one of them
    fails the discriminator and is refused by the base58 branch instead, so this is the
    only way to exercise the bech32 charset check itself.
    """
    with pytest.raises(AddressInvalidError) as caught:
        bech32_decode(BIP173_TESTNET_P2WPKH[:20] + "b" + BIP173_TESTNET_P2WPKH[21:])

    assert caught.value.reason is AddressRejection.INVALID_CHARACTER


def test_bech32_decode_reports_which_constant_verified() -> None:
    """The decoder is not merely refusing everything, and it does not absorb the answer.

    Which of the two constants verified is the caller's business, because only the caller
    knows the witness version and therefore which one *should* have verified. A decoder
    that returned "valid" for either is the bug BIP-350 exists to prevent, and it is
    invisible to every test that feeds it only well-formed input.
    """
    version_zero = bech32_decode(BIP173_TESTNET_P2WPKH)
    version_one = bech32_decode(BIP350_TESTNET_V1)

    assert version_zero.hrp == "tb"
    assert version_zero.is_bech32m is False
    assert version_one.is_bech32m is True
    # The counterexamples decode too -- they are well formed, and wrong about which
    # constant belongs to their version. That is precisely why the flag is returned.
    assert bech32_decode(BIP350_V0_WITH_BECH32M).is_bech32m is True
    assert bech32_decode(DERIVED_V1_WITH_BECH32).is_bech32m is False


@pytest.mark.parametrize(("prefix", "data"), KASPA_POLYMOD_VECTORS)
def test_the_kaspa_polymod_matches_the_published_vectors(prefix: str, data: str) -> None:
    """Twelve published checksums, with every address-level rule out of the way.

    These rows come from the same table as the `kaspatest:` vectors but use rusty-kaspa's
    test-only `a:` and `b:` prefixes, whose payloads are ASCII strings rather than public
    keys. `kaspa_decode` refuses them on the prefix, so the only way to use them is to
    check the polymod directly -- which is exactly what makes them valuable: a one-
    character prefix exercises the prefix expansion differently from `kaspatest`, and a
    wrong generator constant has nothing else to hide behind.
    """
    values = [BECH32_CHARSET.index(character) for character in data]
    payload, tail = values[:-8], values[-8:]
    stated = 0
    for value in tail:
        stated = (stated << 5) | value

    assert _kaspa_checksum(prefix, payload) == stated


def test_an_unknown_base58_version_byte_is_rejected() -> None:
    """A Base58Check string whose checksum *verifies* and which is still not an address.

    This is the vector that separates "decoded successfully" from "is a Bitcoin address".
    A codec that stops as soon as the four checksum bytes match accepts it.
    """
    assert reject(CORE_UNKNOWN_VERSION_BYTE).reason is AddressRejection.UNKNOWN_VERSION_BYTE


@pytest.mark.parametrize("address", CORE_INVALID_BASE58)
def test_published_invalid_base58_strings_are_rejected(address: str) -> None:
    """Rejected; which reason applies is the codec's business, not this test's."""
    reject(address)


@pytest.mark.parametrize("character", ["0", "O", "I", "l"], ids=["zero", "oh", "eye", "ell"])
def test_a_character_outside_the_base58_alphabet_is_rejected(character: str) -> None:
    """The four characters base58 omits precisely because they are misread by humans."""
    address = CORE_SIGNET_P2PKH[:17] + character + CORE_SIGNET_P2PKH[18:]

    assert reject(address).reason is AddressRejection.INVALID_CHARACTER


@pytest.mark.parametrize("character", ["b", "i", "o", "1"], ids=["bee", "eye", "oh", "one"])
def test_a_character_outside_the_bech32_alphabet_is_rejected(character: str) -> None:
    """The four characters bech32 omits. `1` is also the separator, which is the trap."""
    address = BIP173_TESTNET_P2WPKH[:20] + character + BIP173_TESTNET_P2WPKH[21:]

    reject(address)


def test_an_extended_public_key_is_rejected() -> None:
    """A `tpub` is not an address, and it is the most damaging thing to accept as one.

    The vector is deliberately *well formed* -- correct version bytes, correct Base58Check
    checksum -- so that the refusal has to be a decision rather than a side effect of the
    checksum failing. Deriving addresses from one is #24; until then, half-supporting it
    by storing it in a column the providers will read is worse than refusing it.
    """
    assert reject(SYNTHETIC_TPUB).reason is AddressRejection.EXTENDED_KEY


@pytest.mark.parametrize("raw", BLANK_INPUTS, ids=["empty", "space", "tab", "newline", "mixed"])
def test_a_blank_address_is_rejected(raw: str) -> None:
    """The empty string and the several ways a failed paste arrives."""
    assert reject(raw).reason is AddressRejection.EMPTY


def test_a_two_hundred_character_string_is_rejected() -> None:
    """Refused on length, before any codec is asked to think about it."""
    assert len(TWO_HUNDRED_CHARACTERS) == 200

    assert reject(TWO_HUNDRED_CHARACTERS).reason is AddressRejection.TOO_LONG


def test_surrounding_whitespace_does_not_make_a_valid_address_invalid() -> None:
    """A pasted address usually arrives with a newline stuck to it."""
    validated = validate_address(BITCOIN, f"  {BIP173_TESTNET_P2WPKH}\n")

    assert validated.canonical == BIP173_TESTNET_P2WPKH
    assert validated.display == BIP173_TESTNET_P2WPKH


# --------------------------------------------------------------------------------------
# Kaspa
# --------------------------------------------------------------------------------------
#
# The vectors are the testnet rows of `rusty-kaspa`'s own published case table, re-derived
# from the version and public key that table states for each, by an independent CashAddr
# implementation validated against all fourteen of its non-mainnet rows. See
# `tests/address_vectors.py` for the provenance in full. Nothing here was produced by the
# code under test, which would have proved only that the code agrees with itself.


@pytest.mark.parametrize("vector", KASPA_VECTORS, ids=ids_of(KASPA_VECTORS))
def test_a_published_kaspa_vector_is_accepted(vector: Vector) -> None:
    """Both forms keep the network prefix: it is part of the address, not decoration."""
    validated = validate_address(KASPA, vector.address)

    assert validated.canonical == vector.canonical
    assert validated.display == vector.display
    assert validated.canonical.startswith("kaspatest:")


@pytest.mark.parametrize("vector", KASPA_VECTORS, ids=ids_of(KASPA_VECTORS))
def test_every_single_character_kaspa_corruption_is_rejected(vector: Vector) -> None:
    """The same sweep as the Bitcoin one, and for the same reason.

    It matters more here, not less: the Bitcoin codecs are covered by two BIPs' worth of
    published counterexamples, while Kaspa's only published negative cases are the five in
    rusty-kaspa's `test_errors`. Whatever the provenance of a vector, corrupting every
    character of it is what proves a checksum is being computed over the whole string.
    """
    survivors: list[tuple[int, str]] = []
    examined = 0
    for position, corrupted in corruptions_of(vector.address, vector.alphabet):
        examined += 1
        try:
            validate_address(KASPA, corrupted)
        except AddressInvalidError:
            continue
        survivors.append((position, corrupted))

    assert examined > 2000, "the sweep generated almost nothing, so it proves nothing"
    assert survivors == []


@pytest.mark.parametrize(
    ("name", "original", "corrupted"),
    KASPA_NAMED_CORRUPTIONS,
    ids=[name for name, _original, _corrupted in KASPA_NAMED_CORRUPTIONS],
)
def test_a_named_single_character_kaspa_corruption_is_rejected(
    name: str,
    original: str,
    corrupted: str,
) -> None:
    del name
    assert len(original) == len(corrupted)
    assert sum(a != b for a, b in zip(original, corrupted, strict=True)) == 1
    assert validate_address(KASPA, original).canonical == original

    assert reject(corrupted, KASPA).reason is AddressRejection.BAD_CHECKSUM


def test_the_kaspa_checksum_covers_the_network_prefix() -> None:
    """The property that stops a testnet address being stored as a mainnet one.

    `KASPA_WRONG_NETWORK_PREFIX` is the accepted payload of `KASPA_TESTNET_V0` behind a
    *different but still supported* prefix. The refusal therefore has to be a checksum
    failure: an implementation that checksummed the payload alone would find nothing wrong
    with it, and an implementation that simply refused unknown prefixes would never get
    far enough to notice, because `kaspadev` is one it accepts.
    """
    assert KASPA_WRONG_NETWORK_PREFIX.split(":", 1)[1] == KASPA_TESTNET_V0.split(":", 1)[1]
    assert validate_address(KASPA, KASPA_TESTNET_V0).canonical == KASPA_TESTNET_V0

    assert reject(KASPA_WRONG_NETWORK_PREFIX, KASPA).reason is AddressRejection.BAD_CHECKSUM


def test_an_unsupported_kaspa_network_prefix_is_rejected() -> None:
    """`kaspasim` is a real prefix, and deliberately not one this application accepts."""
    assert reject(KASPA_UNKNOWN_PREFIX, KASPA).reason is AddressRejection.UNKNOWN_PREFIX


def test_a_kaspa_address_without_its_prefix_is_rejected() -> None:
    """Without the prefix the checksum cannot be computed at all, so it is not guessed."""
    assert reject(KASPA_NO_PREFIX, KASPA).reason is AddressRejection.MALFORMED


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        (KASPA_DATA_TOO_SHORT, AddressRejection.MALFORMED),
        (KASPA_PAYLOAD_TOO_SHORT, AddressRejection.MALFORMED),
        (KASPA_UNKNOWN_VERSION_BYTE, AddressRejection.UNKNOWN_VERSION_BYTE),
        (KASPA_WRONG_PAYLOAD_LENGTH, AddressRejection.BAD_PROGRAM_LENGTH),
        (KASPA_OVERLONG_PAYLOAD, AddressRejection.BAD_PROGRAM_LENGTH),
    ],
    ids=["no payload", "half a byte", "version 2", "31 bytes", "one extra character"],
)
def test_a_well_checksummed_kaspa_string_that_is_not_an_address_is_rejected(
    address: str,
    reason: AddressRejection,
) -> None:
    """Five strings whose checksums verify and which are still not addresses.

    A correct checksum is what makes these worth having: each one forces the refusal to
    come from the rule named in its id rather than from the checksum failing first, which
    is how a decoder that stops at the checksum would look identical to a correct one.

    `KASPA_PAYLOAD_TOO_SHORT` carries half a byte, and a decoder that read a version byte
    without checking there was one raises `IndexError` -- a 500 where a 422 belongs. The
    reference implementation written to mint these vectors had that exact bug.
    """
    assert reject(address, KASPA).reason is reason


def test_an_extra_character_does_not_produce_a_second_spelling_of_one_address() -> None:
    """Criterion 5's quiet dependency: one address must have exactly one encoding.

    `KASPA_OVERLONG_PAYLOAD` is `KASPA_TESTNET_V0` with one more five-bit character and a
    recomputed checksum. Fifty-four five-bit values regroup into the same thirty-three
    bytes as fifty-three, so the version byte and the payload length both come out
    correct, and a decoder that checked only the decoded bytes would call it the same
    address. It would then be storable alongside the real one: two rows, one address, and
    a unique constraint with no way to see it.
    """
    accepted = validate_address(KASPA, KASPA_TESTNET_V0)

    assert accepted.canonical != KASPA_OVERLONG_PAYLOAD
    assert len(KASPA_OVERLONG_PAYLOAD) == len(KASPA_TESTNET_V0) + 1

    assert reject(KASPA_OVERLONG_PAYLOAD, KASPA).reason is AddressRejection.BAD_PROGRAM_LENGTH


def test_mixed_case_kaspa_is_rejected() -> None:
    assert reject(KASPA_MIXED_CASE, KASPA).reason is AddressRejection.MIXED_CASE


def test_an_uppercase_kaspa_address_canonicalises_to_lower_case() -> None:
    """The same rule as bech32: one spelling in the column the constraint is on."""
    validated = validate_address(KASPA, KASPA_TESTNET_V0.upper())

    assert validated.display == KASPA_TESTNET_V0.upper()
    assert validated.canonical == KASPA_TESTNET_V0


def test_the_two_kaspa_versions_have_different_payload_lengths() -> None:
    """Version 0 carries 32 bytes and version 1 carries 33, so the check is per version.

    Asserted through the vectors rather than by reading the table, because the pair of
    published addresses is the thing that would catch a length check written against a
    single constant: they differ by exactly one character.
    """
    assert len(KASPA_TESTNET_V1_ZERO) == len(KASPA_TESTNET_V0) + 2
    assert validate_address(KASPA, KASPA_TESTNET_V0).canonical == KASPA_TESTNET_V0
    assert validate_address(KASPA, KASPA_TESTNET_V1_ZERO).canonical == KASPA_TESTNET_V1_ZERO


@pytest.mark.parametrize("raw", BLANK_INPUTS, ids=["empty", "space", "tab", "newline", "mixed"])
def test_a_blank_kaspa_address_is_rejected(raw: str) -> None:
    assert reject(raw, KASPA).reason is AddressRejection.EMPTY


def test_a_kaspa_address_is_not_a_bitcoin_address() -> None:
    """The mirror of the cross-chain test in `test_chains.py`, from the Kaspa side."""
    assert validate_address(KASPA, KASPA_TESTNET_V1_KEY).canonical == KASPA_TESTNET_V1_KEY

    reject(KASPA_TESTNET_V1_KEY, BITCOIN)


@pytest.mark.parametrize(
    "address",
    [
        KASPA_WRONG_NETWORK_PREFIX,
        KASPA_UNKNOWN_PREFIX,
        KASPA_NO_PREFIX,
        KASPA_MIXED_CASE,
        *(corrupted for _name, _original, corrupted in KASPA_NAMED_CORRUPTIONS),
    ],
)
def test_a_kaspa_refusal_never_quotes_the_address(address: str) -> None:
    """The same rule as for Bitcoin, asserted on the codec that has its own message paths."""
    error = reject(address, KASPA)

    assert address not in error.message
    assert address not in str(error)
    assert address[:20] not in error.message


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(raw=st.text(max_size=300))
def test_kaspa_validation_refuses_arbitrary_text_without_crashing(raw: str) -> None:
    """The Kaspa codec regroups bits and indexes a charset, so it is the likelier to crash."""
    outcome = outcome_of(KASPA, raw)

    if isinstance(outcome, AddressInvalidError):
        assert isinstance(outcome.reason, AddressRejection)
        return
    assert isinstance(outcome, ValidatedAddress)


# --------------------------------------------------------------------------------------
# The error never carries the input back
# --------------------------------------------------------------------------------------

#: Every invalid string this module knows about, which is every string whose refusal
#: produces a message that a router will put in a 422 body.
REJECTED_INPUTS: Final[tuple[str, ...]] = (
    BIP350_V0_WITH_BECH32M,
    BIP350_V2_WITH_BECH32,
    DERIVED_V1_WITH_BECH32,
    BIP173_MIXED_CASE,
    BIP350_MIXED_CASE,
    BIP173_UNKNOWN_HRP,
    BIP350_UNKNOWN_HRP,
    BIP173_NON_ZERO_PADDING,
    BIP350_NON_ZERO_PADDING,
    CORE_INVALID_SHORT_PROGRAM,
    CORE_UNKNOWN_VERSION_BYTE,
    SYNTHETIC_TPUB,
    TWO_HUNDRED_CHARACTERS,
    *CORE_INVALID_BASE58,
    *(corrupted for _name, _original, corrupted in NAMED_CORRUPTIONS),
)


@pytest.mark.parametrize("address", REJECTED_INPUTS)
def test_the_refusal_never_quotes_the_address(address: str) -> None:
    """Criterion 3, at its source.

    A 422 is the one response that carries user input back to the client, and from there
    into any log the browser keeps. If the message interpolates the address -- which is
    the natural way to write a helpful error -- the rule that addresses never appear in a
    log is broken on the client side by the server's own error text. The message names the
    field and the reason; it never names the value.
    """
    error = reject(address)

    assert address not in error.message
    assert address not in str(error)
    assert address not in repr(error)
    # A long prefix is just as good as the whole string to anyone reading a log.
    assert address[:20] not in error.message
    assert error.message, "a reason with no message cannot be rendered into a 422"


# --------------------------------------------------------------------------------------
# Properties, over inputs nobody thought to write down
# --------------------------------------------------------------------------------------

ALPHANUMERIC: Final = string.ascii_letters + string.digits


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
@given(raw=st.text(max_size=300))
def test_validation_refuses_arbitrary_text_without_crashing(raw: str) -> None:
    """Whatever is pasted, the answer is an address or a domain refusal -- never a 500.

    A codec that indexes into a string it has not measured raises `IndexError`, and a
    codec that decodes bytes raises `UnicodeDecodeError`; both become an unhandled
    exception and a 500 rather than the 422 the contract promises. Generated text finds
    those far more reliably than a list of inputs somebody thought of.
    """
    outcome = outcome_of(BITCOIN, raw)

    if isinstance(outcome, AddressInvalidError):
        assert isinstance(outcome.reason, AddressRejection)
        return
    assert isinstance(outcome, ValidatedAddress)
    assert outcome.canonical
    assert outcome.display


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(raw=st.text(alphabet=ALPHANUMERIC, min_size=16, max_size=120))
def test_no_generated_input_is_echoed_back_in_its_own_refusal(raw: str) -> None:
    """The same rule as above, over strings nobody curated.

    Sixteen characters minimum, drawn from an alphabet with no spaces in it: no English
    sentence contains such a run, so a hit is an interpolated value rather than a
    coincidence.
    """
    outcome = outcome_of(BITCOIN, raw)

    if isinstance(outcome, AddressInvalidError):
        assert raw not in outcome.message
        assert raw not in str(outcome)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(raw=st.text(alphabet=ALPHANUMERIC, max_size=120))
def test_an_accepted_address_always_survives_a_second_validation(raw: str) -> None:
    """Anything the validator accepts, it accepts again in its canonical form."""
    once = outcome_of(BITCOIN, raw)
    if isinstance(once, AddressInvalidError):
        return
    twice = validate_address(BITCOIN, once.canonical)

    assert twice.canonical == once.canonical
