"""Address codecs: bech32 and bech32m, Base58Check, and Kaspa's CashAddr variant.

Pure by construction, and that is the whole point of the module. The addresses this
application tracks are copied by hand out of a hardware wallet that exposes no API, so the
only thing standing between a mistyped character and a wallet that reports zero forever is
a checksum -- and a checksum that cost a network round-trip is a checksum somebody
eventually skips. Nothing here opens a socket, reads a clock or touches a database.

**The generator constants are transcribed from published specifications, and each one
names its source in a comment beside it.** They are deliberately not reconstructed from a
sample address: an implementation fitted to its own sample proves only that it agrees with
itself, and would carry a systematic error into every address the product ever accepts.

Three encodings, three different failure modes worth knowing about:

* **bech32 and bech32m** (BIP-173, BIP-350) differ only in the constant xored into the
  checksum. Witness version 0 uses bech32 and versions 1 to 16 use bech32m; a decoder that
  accepts either constant for either version passes every happy-path test while letting a
  corrupted address through. BIP-350 puts the cost plainly: permitting both leaves a
  checksum worth 29 bits rather than 30.
* **Base58Check is case sensitive.** `1A` and `1a` decode to different bytes, so unlike
  bech32 it is never normalised, and lowercasing one would silently produce a different
  address. That asymmetry is why the registry stores a canonical form and a display form
  as two columns rather than one column and a `lower()` at query time.
* **Kaspa** is a CashAddr-style encoding whose 40-bit checksum covers the network prefix
  as well as the payload, so the same payload under `kaspa:` and under `kaspatest:` has
  different checksums and a testnet address cannot be misread as a mainnet one.

No exception raised from this module contains the address. A rejection travels as an
`AddressRejection` member and renders as a fixed sentence, because a 422 body is the one
response that would otherwise carry the owner's address back out of the process.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

MAX_ADDRESS_LENGTH: Final = 128
"""The longest string any codec here will look at.

Comfortably above every supported form: BIP-173 caps bech32 at 90 characters, a
Base58Check address is 34 or 35, and the longest Kaspa address is a `kaspatest:` prefix
plus 63 characters. It is here so that a pasted paragraph is refused by its length rather
than walked character by character.
"""


class AddressRejection(StrEnum):
    """Why an address was refused, as a value a caller can branch on.

    A member rather than a message, so the wording can change without breaking a caller --
    and so nothing is ever tempted to build a reason string out of the address itself.
    """

    UNKNOWN_CHAIN = "unknown_chain"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    MIXED_CASE = "mixed_case"
    BAD_CHECKSUM = "bad_checksum"
    UNKNOWN_PREFIX = "unknown_prefix"
    BAD_WITNESS_VERSION = "bad_witness_version"
    BAD_PROGRAM_LENGTH = "bad_program_length"
    INVALID_CHARACTER = "invalid_character"
    UNKNOWN_VERSION_BYTE = "unknown_version_byte"
    EXTENDED_KEY = "extended_key"
    MALFORMED = "malformed"
    WRONG_NETWORK = "wrong_network"


REJECTION_MESSAGES: Final[Mapping[AddressRejection, str]] = {
    AddressRejection.UNKNOWN_CHAIN: "Unknown chain.",
    AddressRejection.EMPTY: "An address is required.",
    AddressRejection.TOO_LONG: "The address is longer than any address of this kind.",
    AddressRejection.MIXED_CASE: "The address mixes upper and lower case.",
    AddressRejection.BAD_CHECKSUM: "The checksum does not match.",
    AddressRejection.UNKNOWN_PREFIX: "Unknown human-readable prefix.",
    AddressRejection.BAD_WITNESS_VERSION: "The witness version is outside the range 0 to 16.",
    AddressRejection.BAD_PROGRAM_LENGTH: "The payload is the wrong length for its version.",
    AddressRejection.INVALID_CHARACTER: "A character appears that this encoding never uses.",
    AddressRejection.UNKNOWN_VERSION_BYTE: "The version byte is not one this chain uses.",
    AddressRejection.EXTENDED_KEY: "This is an extended public key, not an address.",
    AddressRejection.MALFORMED: "The address is not shaped like an address for this chain.",
    AddressRejection.WRONG_NETWORK: "The address belongs to a different network of this chain.",
}
"""One fixed sentence per reason. **Not one of them interpolates anything.**

That is a rule rather than a coincidence. These strings are rendered into the `msg` of a
422 problem document, and a message that quoted the offending address would put it in the
response body, from where any client-side logger would record it.
"""


class AddressInvalidError(ValueError):
    """An address that failed validation, carrying the reason as a value.

    `str(exc)` is the fixed sentence for the reason and never the input, so a handler that
    renders the exception the lazy way still cannot leak an address.
    """

    def __init__(self, reason: AddressRejection) -> None:
        self.reason = reason
        self.message = REJECTION_MESSAGES[reason]
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class ValidatedAddress:
    """An address that verified, in the two forms the registry stores.

    `canonical` is what uniqueness is decided on and what a chain provider will be asked
    about. `display` is what the owner typed, so that a wallet which renders an address in
    uppercase does not come back looking like a different string from the one pasted in.
    """

    canonical: str
    display: str


def _five_bit_length(byte_length: int) -> int:
    """How many charset characters a payload of this many bytes encodes to.

    The ceiling of `byte_length * 8 / 5`, written as integer arithmetic because true
    division of two integers is a float and floats are banned in this layer.
    """
    return (byte_length * 8 + 4) // 5


def _normalise_case(raw: str) -> str:
    """Lowercase a case-insensitive encoding, refusing a string that mixes the two.

    BIP-173 is explicit that a decoder must not accept a mixed-case string, and the reason
    is the checksum rather than tidiness: the lowercase form is what the checksum is
    computed over, so allowing a mixed-case string through would mean two different strings
    verifying as the same address.

    **Non-ASCII is refused first, and the ASCII check is load bearing rather than
    defensive.** The mixed-case test below asks whether `raw` equals its own lower or upper
    form, and Unicode has characters for which that question gives the wrong answer.
    U+212A KELVIN SIGN lowercases to `k` but uppercases to itself, so an otherwise
    uppercase string containing one satisfies `raw.upper() == raw`, sails past the
    mixed-case guard, and then folds to a perfectly valid address. The checksum verifies,
    because it is computed over the folded form -- and `display`, which is the form
    `GET /api/wallets` returns and the owner copies, keeps the Kelvin sign. That string is
    not an address on any network. Uniqueness and provider calls key off `canonical` and
    were never at risk, so this was never a double-counted balance; it was an address the
    product would have shown back to its owner as if it were theirs.

    An address in every encoding here is ASCII by construction, so nothing legitimate is
    refused by this.

    Raises:
        AddressInvalidError: the string contains a non-ASCII character, or both uppercase
            and lowercase letters.
    """
    if not raw.isascii():
        raise AddressInvalidError(AddressRejection.INVALID_CHARACTER)
    if raw.lower() != raw and raw.upper() != raw:
        raise AddressInvalidError(AddressRejection.MIXED_CASE)
    return raw.lower()


# ---------------------------------------------------------------------------------------
# bech32 and bech32m
# ---------------------------------------------------------------------------------------

BECH32_CHARSET: Final = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
"""The data-part alphabet, from BIP-173's character table.

`1`, `b`, `i` and `o` are absent, which is what makes `1` usable as the separator and
removes the characters most often confused when an address is copied by hand.
"""

_BECH32_CHARSET_INDEX: Final[Mapping[str, int]] = {
    character: value for value, character in enumerate(BECH32_CHARSET)
}

# Transcribed from BIP-173, section "Bech32", the `GEN` list inside `bech32_polymod`:
# https://github.com/bitcoin/bips/blob/master/bip-0173.mediawiki
# BIP-350 restates the identical list, because bech32m changes only the final constant.
_BECH32_GENERATOR: Final = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)

# BIP-173: `bech32_verify_checksum` holds when the polymod comes out as 1.
_BECH32_CONST: Final = 1
# BIP-350: `BECH32M_CONST = 0x2bc830a3`, the constant that replaces the 1 above.
# https://github.com/bitcoin/bips/blob/master/bip-0350.mediawiki
_BECH32M_CONST: Final = 0x2BC830A3

_BECH32_MAX_LENGTH: Final = 90
"""BIP-173: the whole string is at most 90 characters, hrp and checksum included."""

_BECH32_CHECKSUM_LENGTH: Final = 6

BITCOIN_HRPS: Final[frozenset[str]] = frozenset({"bc", "tb", "bcrt"})
"""The human-readable parts Bitcoin Core configures, in `src/kernel/chainparams.cpp`:
`bc` on mainnet, `tb` on testnet, testnet4 and signet, `bcrt` on regtest.
"""

_WITNESS_V0_PROGRAM_LENGTHS: Final[frozenset[int]] = frozenset({20, 32})
"""BIP-350's decoder: a version 0 program is exactly 20 bytes (a key hash) or 32 (a script
hash). Versions 1 to 16 accept anything from 2 to 40."""

_WITNESS_MIN_PROGRAM_LENGTH: Final = 2
_WITNESS_MAX_PROGRAM_LENGTH: Final = 40
_WITNESS_MAX_VERSION: Final = 16

_MIN_WITNESS_DATA_LENGTH: Final = 1 + _five_bit_length(_WITNESS_MIN_PROGRAM_LENGTH)
"""The shortest data part any segwit address can have: the witness version, plus the four
characters a two-byte program encodes to. `BC1SW50QGDZ25J` in BIP-350 sits exactly here."""


def _bech32_polymod(values: Iterable[int]) -> int:
    """BIP-173's `bech32_polymod`, transposed from the Python in the specification."""
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = (checksum & 0x1FFFFFF) << 5 ^ value
        for index, generator in enumerate(_BECH32_GENERATOR):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def _bech32_hrp_expand(hrp: str) -> list[int]:
    """BIP-173's `bech32_hrp_expand`: the high bits, a zero, then the low bits.

    The order is load bearing rather than arbitrary. Feeding the high bits first means an
    error that only disturbs the low five bits of a character -- swapping one letter for
    another, overwhelmingly the common case -- stays inside the part of the input the BCH
    code's guarantees actually cover.
    """
    high = [ord(character) >> 5 for character in hrp]
    low = [ord(character) & 31 for character in hrp]
    return [*high, 0, *low]


def _convert_bits(values: Sequence[int], from_bits: int, to_bits: int) -> list[int] | None:
    """Regroup small integers into a different word size, or return `None` if it does not fit.

    BIP-173's `convertbits` with `pad=False`, which is the only direction a decoder needs.
    Leftover bits are not merely dropped: the function refuses when there are enough of them
    to have held another whole input word, and when the padding they carry is not zero.
    Dropping them quietly is how a decoder comes to accept several distinct strings as the
    same address -- BIP-173's own invalid-address vectors include exactly that case.
    """
    accumulator = 0
    bits = 0
    result: list[int] = []
    max_value = (1 << to_bits) - 1
    for value in values:
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((accumulator >> bits) & max_value)
    if bits >= from_bits or (accumulator << (to_bits - bits)) & max_value:
        return None
    return result


def _charset_values(data: str) -> list[int]:
    """Map a data part to its five-bit values, refusing a character outside the charset."""
    values: list[int] = []
    for character in data:
        value = _BECH32_CHARSET_INDEX.get(character)
        if value is None:
            raise AddressInvalidError(AddressRejection.INVALID_CHARACTER)
        values.append(value)
    return values


@dataclass(frozen=True, slots=True)
class Bech32Decoded:
    """A verified bech32 string: its prefix, its data values, and which constant verified."""

    hrp: str
    data: list[int]
    is_bech32m: bool


def bech32_decode(raw: str) -> Bech32Decoded:
    """Decode and verify a bech32 or bech32m string, without interpreting its payload.

    Which of the two constants verified is **returned rather than absorbed**, because only
    the caller knows which one should have been used. A decoder that quietly accepts either
    is the bug BIP-350 exists to prevent, and it is invisible to every test that feeds it
    only well-formed input.

    Raises:
        AddressInvalidError: over the length limit, mixed case, no separator, a data
            character outside the charset, or a checksum that does not verify.
    """
    if len(raw) > _BECH32_MAX_LENGTH:
        raise AddressInvalidError(AddressRejection.TOO_LONG)
    lowered = _normalise_case(raw)

    separator = lowered.rfind("1")
    if separator < 1 or separator + _BECH32_CHECKSUM_LENGTH >= len(lowered):
        raise AddressInvalidError(AddressRejection.MALFORMED)

    hrp = lowered[:separator]
    values = _charset_values(lowered[separator + 1 :])
    checksum = _bech32_polymod([*_bech32_hrp_expand(hrp), *values])
    if checksum == _BECH32_CONST:
        is_bech32m = False
    elif checksum == _BECH32M_CONST:
        is_bech32m = True
    else:
        raise AddressInvalidError(AddressRejection.BAD_CHECKSUM)

    return Bech32Decoded(hrp=hrp, data=values[:-_BECH32_CHECKSUM_LENGTH], is_bech32m=is_bech32m)


def _validate_segwit_address(raw: str) -> ValidatedAddress:
    """Verify a segwit address: checksum constant, witness version, then program length.

    The checks are separate on purpose. The checksum proves the string was not mistyped;
    the version-to-constant rule is what BIP-350 added and what an either-will-do decoder
    throws away; the program length is what tells a real version 0 output from a
    plausible-looking one.
    """
    decoded = bech32_decode(raw)
    if decoded.hrp not in BITCOIN_HRPS:
        raise AddressInvalidError(AddressRejection.UNKNOWN_PREFIX)
    # One value for the witness version plus the four a two-byte program needs. This is
    # also what makes reading `data[0]` below safe, rather than a separate emptiness guard
    # -- BIP-350's "empty data section" vector is the shortest case of "too short", not a
    # different kind of failure, and two branches for it would mean two vectors to cover
    # one rule.
    if len(decoded.data) < _MIN_WITNESS_DATA_LENGTH:
        raise AddressInvalidError(AddressRejection.BAD_PROGRAM_LENGTH)

    version = decoded.data[0]
    if version > _WITNESS_MAX_VERSION:
        raise AddressInvalidError(AddressRejection.BAD_WITNESS_VERSION)

    # The constant that verified must be the one this witness version mandates. Accepting
    # the other is the most expensive mistake available in this file: every well-formed
    # address still validates, so nothing looks wrong until a corrupted one validates too.
    if decoded.is_bech32m != (version != 0):
        raise AddressInvalidError(AddressRejection.BAD_CHECKSUM)

    program = _convert_bits(decoded.data[1:], 5, 8)
    if program is None:
        raise AddressInvalidError(AddressRejection.BAD_PROGRAM_LENGTH)
    if not _WITNESS_MIN_PROGRAM_LENGTH <= len(program) <= _WITNESS_MAX_PROGRAM_LENGTH:
        raise AddressInvalidError(AddressRejection.BAD_PROGRAM_LENGTH)
    if version == 0 and len(program) not in _WITNESS_V0_PROGRAM_LENGTHS:
        raise AddressInvalidError(AddressRejection.BAD_PROGRAM_LENGTH)

    return ValidatedAddress(canonical=raw.lower(), display=raw)


# ---------------------------------------------------------------------------------------
# Base58Check
# ---------------------------------------------------------------------------------------

BASE58_ALPHABET: Final = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
"""Bitcoin's base58 alphabet: the digits and letters, minus `0`, `O`, `I` and `l`."""

_BASE58_INDEX: Final[Mapping[str, int]] = {
    character: value for value, character in enumerate(BASE58_ALPHABET)
}

BITCOIN_VERSION_BYTES: Final[frozenset[int]] = frozenset({0x00, 0x05, 0x6F, 0xC4})
"""`base58Prefixes[PUBKEY_ADDRESS]` and `[SCRIPT_ADDRESS]` from Bitcoin Core's
`src/kernel/chainparams.cpp`: 0 and 5 on mainnet, 111 and 196 on testnet, testnet4, signet
and regtest alike.
"""

_BASE58CHECK_LENGTH: Final = 25
"""One version byte, a twenty-byte hash, and the four-byte checksum."""

_BASE58CHECK_CHECKSUM_LENGTH: Final = 4

EXTENDED_KEY_PREFIXES: Final[tuple[str, ...]] = ("xpub", "ypub", "zpub", "tpub", "upub", "vpub")
"""Extended public keys, refused by name before anything tries to decode them.

An extended key is not an address, and deriving addresses from one is a separate change.
Rejecting it explicitly is what lets the owner be told that, rather than being told the
checksum failed -- because the checksum would pass: a serialised extended key is perfectly
valid Base58Check, just 82 bytes rather than 25.
"""


def base58check_decode(raw: str) -> bytes:
    """Decode a Base58Check string and verify its four-byte double-SHA256 checksum.

    Returns the version byte and hash, with the checksum removed.

    Raises:
        AddressInvalidError: a character outside the alphabet, a payload that is not the
            25 bytes an address is, or a checksum that does not match.
    """
    number = 0
    for character in raw:
        value = _BASE58_INDEX.get(character)
        if value is None:
            raise AddressInvalidError(AddressRejection.INVALID_CHARACTER)
        number = number * 58 + value

    # Every leading `1` is a leading zero byte, which the integer above cannot carry: a
    # mainnet P2PKH address is exactly the case where dropping them changes the version.
    leading_zeros = len(raw) - len(raw.lstrip("1"))
    magnitude = number.to_bytes((number.bit_length() + 7) // 8, "big")
    payload = b"\x00" * leading_zeros + magnitude
    if len(payload) != _BASE58CHECK_LENGTH:
        raise AddressInvalidError(AddressRejection.MALFORMED)

    versioned = payload[:-_BASE58CHECK_CHECKSUM_LENGTH]
    expected = hashlib.sha256(hashlib.sha256(versioned).digest()).digest()
    if expected[:_BASE58CHECK_CHECKSUM_LENGTH] != payload[-_BASE58CHECK_CHECKSUM_LENGTH:]:
        raise AddressInvalidError(AddressRejection.BAD_CHECKSUM)
    return versioned


def _validate_base58_address(raw: str) -> ValidatedAddress:
    """Verify a legacy address and return it **unchanged in both forms**.

    Base58Check is case sensitive -- a character's case is part of the value it decodes to
    -- so there is no normalisation to perform and applying one would produce a different
    address. That is the asymmetry with bech32 that the two stored columns exist for.
    """
    versioned = base58check_decode(raw)
    if versioned[0] not in BITCOIN_VERSION_BYTES:
        raise AddressInvalidError(AddressRejection.UNKNOWN_VERSION_BYTE)
    return ValidatedAddress(canonical=raw, display=raw)


# ---------------------------------------------------------------------------------------
# Kaspa
# ---------------------------------------------------------------------------------------

KASPA_PREFIXES: Final[frozenset[str]] = frozenset({"kaspa", "kaspatest", "kaspadev"})
"""The network prefixes this application accepts, from `Prefix` in kaspanet/rusty-kaspa's
`crypto/addresses/src/lib.rs`. `kaspasim` exists there too and is deliberately left out: a
simulation network holds nothing worth tracking.
"""

# Transcribed from `polymod` in kaspanet/rusty-kaspa, `crypto/addresses/src/bech32.rs`:
# https://github.com/kaspanet/rusty-kaspa/blob/master/crypto/addresses/src/bech32.rs
# That function names its own source in a comment as the Bitcoin Cash CashAddr
# specification, https://bch.info/en/specifications -- Kaspa's encoding is CashAddr's with
# a different prefix set, so these five generators are the CashAddr generators unchanged.
# kaspanet/kaspad's `util/bech32/bech32.go` carries the identical five.
_KASPA_GENERATOR: Final = (0x98F2BC8E61, 0x79B76D99E2, 0xF33E5FB3C4, 0xAE2EABE2A8, 0x1E4F43E470)

_KASPA_CHECKSUM_LENGTH: Final = 8
"""Eight charset characters -- forty bits, against bech32's six characters and thirty."""

KASPA_PAYLOAD_LENGTHS: Final[Mapping[int, int]] = {0: 32, 1: 33, 8: 32}
"""Version byte to public key length, from `Version::public_key_len` in rusty-kaspa:
`PubKey` (0) and `ScriptHash` (8) carry 32 bytes, `PubKeyECDSA` (1) carries 33.
"""


def _kaspa_polymod(values: Iterable[int]) -> int:
    """The 40-bit CashAddr polymod, as rusty-kaspa's `polymod` computes it.

    Wider than bech32's in every dimension: a 35-bit shift rather than 25, five 40-bit
    generators rather than five 30-bit ones, and a final `^ 1` that bech32 folds into its
    comparison instead.
    """
    checksum = 1
    for value in values:
        top = checksum >> 35
        checksum = ((checksum & 0x07FFFFFFFF) << 5) ^ value
        for index, generator in enumerate(_KASPA_GENERATOR):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum ^ 1


def _kaspa_checksum(prefix: str, payload: Sequence[int]) -> int:
    """Checksum the prefix together with the payload, as rusty-kaspa's `checksum` does.

    The prefix contributes the low five bits of each of its characters, then a zero
    separator, then the payload, then eight zeros standing in for the checksum being
    computed. Including the prefix is what makes a `kaspatest:` address fail as a `kaspa:`
    one: the same payload checksums differently on each network.
    """
    expanded = [ord(character) & 0x1F for character in prefix]
    return _kaspa_polymod([*expanded, 0, *payload, *([0] * _KASPA_CHECKSUM_LENGTH)])


def _kaspa_five_to_eight(values: Sequence[int]) -> bytes:
    """Regroup five-bit values into bytes, dropping the trailing padding bits.

    Deliberately the truncating conversion rusty-kaspa's `conv5to8` performs, rather than
    the strict one used for bech32 above. Being bit-for-bit identical to the reference
    decoder is worth more here than the extra bit of error detection, and the exact
    character count is checked separately, which is the stronger of the two rules anyway.
    """
    accumulator = 0
    bits = 0
    result = bytearray()
    for value in values:
        accumulator = (accumulator << 5) | value
        bits += 5
        while bits >= 8:
            bits -= 8
            result.append((accumulator >> bits) & 0xFF)
    return bytes(result)


@dataclass(frozen=True, slots=True)
class KaspaDecoded:
    """A verified Kaspa address: its network prefix, its version byte and its payload."""

    prefix: str
    version: int
    payload: bytes


def kaspa_decode(raw: str) -> KaspaDecoded:
    """Decode and verify a Kaspa address, network prefix included.

    The `_normalise_case` call on the first line is where this decoder parts company with
    kaspanet/rusty-kaspa, which refuses an uppercase address rather than folding it. The
    argument for the difference, and why it cannot admit an address the reference would
    reject, is written out in `validate_kaspa_address`.

    Raises:
        AddressInvalidError: mixed case, no prefix, an unknown prefix, a character outside
            the charset, a checksum that does not verify, an unknown version byte, or a
            payload the wrong length for that version.
    """
    lowered = _normalise_case(raw)
    prefix, separator, data = lowered.partition(":")
    if not separator:
        raise AddressInvalidError(AddressRejection.MALFORMED)
    if prefix not in KASPA_PREFIXES:
        raise AddressInvalidError(AddressRejection.UNKNOWN_PREFIX)
    if len(data) <= _KASPA_CHECKSUM_LENGTH:
        raise AddressInvalidError(AddressRejection.MALFORMED)

    values = _charset_values(data)
    payload_values = values[:-_KASPA_CHECKSUM_LENGTH]
    stated = 0
    for value in values[-_KASPA_CHECKSUM_LENGTH:]:
        stated = (stated << 5) | value
    if stated != _kaspa_checksum(prefix, payload_values):
        raise AddressInvalidError(AddressRejection.BAD_CHECKSUM)

    decoded = _kaspa_five_to_eight(payload_values)
    if not decoded:
        raise AddressInvalidError(AddressRejection.MALFORMED)

    version, payload = decoded[0], decoded[1:]
    expected = KASPA_PAYLOAD_LENGTHS.get(version)
    if expected is None:
        raise AddressInvalidError(AddressRejection.UNKNOWN_VERSION_BYTE)
    # Both the byte count and the character count, because they are not the same check: a
    # version byte and 32 payload bytes encode to exactly 53 characters, and 54 characters
    # regroup back into the same 33 bytes. Append one character, recompute the checksum,
    # and the result verifies with the right version byte and the right payload length --
    # a second, longer spelling of an address that already exists.
    #
    # **That is a double-counted balance, not an untidy string.** `address_canonical` is
    # what `uq_wallets_user_chain_address` indexes, and two spellings are two different
    # values in that column, so the constraint cannot see them as the same wallet. The
    # owner registers one address twice, both rows are read, and the holding is counted
    # twice in the portfolio total -- silently, and in the one number the product exists
    # to report.
    #
    # **This is deliberately stricter than the reference implementations**, which is the
    # mirror image of the uppercase tolerance in `validate_kaspa_address` and is recorded
    # here for the same reason. rusty-kaspa's `conv5to8` truncates and does not check the
    # count; an independently written CashAddr decoder checked against rusty-kaspa's own
    # test vectors does not either, and accepts the overlong string as the same address.
    # Do not simplify this to the byte-length check alone. It looks redundant; the test
    # that proves it is not is, in `tests/domain/test_addresses.py`:
    # test_an_extra_character_does_not_produce_a_second_spelling_of_one_address
    if len(payload) != expected or len(payload_values) != _five_bit_length(expected + 1):
        raise AddressInvalidError(AddressRejection.BAD_PROGRAM_LENGTH)

    return KaspaDecoded(prefix=prefix, version=version, payload=payload)


# ---------------------------------------------------------------------------------------
# The per-chain entry points
# ---------------------------------------------------------------------------------------


def _looks_like_bech32(raw: str) -> bool:
    """Whether to read a Bitcoin address as bech32 rather than as Base58Check.

    The two encodings share enough of an alphabet that "try one, then the other" would
    report a mistyped legacy address as an unknown prefix, and a mistyped segwit address as
    a bad base58 checksum. The discriminator is the bech32 shape itself: a separator, an
    all-alphabetic human-readable part in front of it, and at least a checksum's worth of
    charset characters behind it.

    Case folded without the mixed-case check, deliberately -- a mixed-case *base58* address
    is perfectly legal and must reach the base58 branch, while a mixed-case bech32 one has
    to reach the bech32 branch in order to be rejected for the right reason.

    A Base58Check address cannot match. Verified rather than assumed, against every base58
    vector in Bitcoin Core's `src/test/data/key_io_valid.json`: not one satisfies all three
    conditions, because the part in front of a `1` in a base58 address always contains a
    digit or one of `b`, `i`, `o`, none of which the bech32 charset has.
    """
    lowered = raw.lower()
    separator = lowered.rfind("1")
    if separator < 1:
        return False
    hrp, data = lowered[:separator], lowered[separator + 1 :]
    return (
        hrp.isascii()
        and hrp.isalpha()
        and len(data) >= _BECH32_CHECKSUM_LENGTH
        and all(character in _BECH32_CHARSET_INDEX for character in data)
    )


def validate_bitcoin_address(raw: str) -> ValidatedAddress:
    """Verify a Bitcoin address in any of the forms this application accepts.

    Segwit (bech32 and bech32m) and legacy Base58Check, on mainnet, testnet, signet and
    regtest. An extended public key is refused by name rather than by checksum, because its
    checksum is valid and the owner would otherwise be told something untrue.

    Raises:
        AddressInvalidError: with the reason the address was refused.
    """
    if raw.startswith(EXTENDED_KEY_PREFIXES):
        raise AddressInvalidError(AddressRejection.EXTENDED_KEY)
    if _looks_like_bech32(raw):
        return _validate_segwit_address(raw)
    return _validate_base58_address(raw)


def validate_kaspa_address(raw: str) -> ValidatedAddress:
    """Verify a Kaspa address, keeping the network prefix in both stored forms.

    The prefix is part of the address rather than decoration: it is fed into the checksum,
    so an address without it cannot be checked at all, and one stored without it could not
    be told apart from the same payload on another network.

    **This deviates from the reference implementation, deliberately, in one place.**
    kaspanet/rusty-kaspa rejects an uppercase address outright: its `REV_CHARSET` table
    maps every uppercase byte to the sentinel that means "not a charset character", so
    `KASPATEST:Q...` is a decoding error there and a valid address here. Recorded in the
    code rather than left to be re-derived by whoever next reads both implementations.

    Accepting it is safe, and the reason is which of the two stored forms travels:

    * the checksum is verified over the **lowercased** string, so an uppercase rendering
      is accepted only if the address it spells is the one the checksum covers. Nothing is
      admitted that a lowercase reading would have refused;
    * `canonical` is the lowercased form, and canonical is what the unique constraint
      indexes and what a chain provider is handed. Nothing downstream ever sees the
      uppercase string, so no request leaves this process carrying a spelling rusty-kaspa
      would refuse;
    * the extra tolerance is in the direction that cannot go wrong. A *mixed*-case string
      is still rejected, because that is the case where folding would let two different
      strings verify as one address.

    The registry's canonical and display columns exist precisely so an uppercase rendering
    -- which is what a QR code or a hardware wallet screen often shows -- survives a round
    trip looking the way the owner typed it. Refusing it outright would make that column
    pair pointless for this chain.

    Raises:
        AddressInvalidError: with the reason the address was refused.
    """
    kaspa_decode(raw)
    return ValidatedAddress(canonical=raw.lower(), display=raw)


# ---------------------------------------------------------------------------------------
# Which Bitcoin network an address belongs to
# ---------------------------------------------------------------------------------------
#
# `validate_bitcoin_address` answers "is this a Bitcoin address", which is a question about
# the string, and it accepts every network on purpose. Which *network* the address is on is
# a separate question, and the first thing that ever needed it is the Esplora provider:
# one instance serves exactly one network, so an address from another one has to be refused
# before a URL is built out of it.
#
# It lives here rather than in `providers/` because it reads the same prefix and version
# tables the codecs above already own, and two copies of a prefix table is how they drift
# apart. It is pure, like everything else in this module: no settings, no clock, no socket.
# Which network the *provider* is configured for is the provider's business; this function
# only says what the address says about itself.


class BitcoinNetwork(StrEnum):
    """The Bitcoin networks an address can be read as belonging to.

    Three members rather than one per chain Bitcoin Core knows about, because an address
    cannot distinguish more than three. `tb` is testnet3, testnet4 and signet alike, and
    the base58 testnet version bytes cover regtest as well -- see `bitcoin_network_of` for
    what that costs.
    """

    MAINNET = "mainnet"
    TESTNET = "testnet"
    REGTEST = "regtest"


BITCOIN_NETWORK_BY_HRP: Final[Mapping[str, BitcoinNetwork]] = {
    "bc": BitcoinNetwork.MAINNET,
    "tb": BitcoinNetwork.TESTNET,
    "bcrt": BitcoinNetwork.REGTEST,
}
"""The human-readable part to its network, from the same `chainparams.cpp` as `BITCOIN_HRPS`.

Total over `BITCOIN_HRPS` by construction, and a test asserts it: an hrp the codec accepts
but this table does not know would make `bitcoin_network_of` raise on an address that had
just validated, which is the kind of contradiction that surfaces as a wallet nobody can
read.
"""

BITCOIN_NETWORK_BY_VERSION_BYTE: Final[Mapping[int, BitcoinNetwork]] = {
    0x00: BitcoinNetwork.MAINNET,
    0x05: BitcoinNetwork.MAINNET,
    0x6F: BitcoinNetwork.TESTNET,
    0xC4: BitcoinNetwork.TESTNET,
}
"""The base58 version byte to its network, total over `BITCOIN_VERSION_BYTES`.

**0x6F and 0xC4 map to `TESTNET` and there is no honest alternative.** Bitcoin Core gives
testnet, testnet4, signet and regtest the identical pair, so a legacy address on regtest
and the same address on testnet are the same 25 bytes. `REGTEST` is reachable from `bcrt1`
and from nothing else.
"""


def bitcoin_network_of(canonical: str) -> BitcoinNetwork:
    """Which Bitcoin network an address is on, offline, from the address alone.

    Reads the human-readable part for a segwit address and the version byte for a legacy
    one, through the same decoders `validate_bitcoin_address` uses -- so the checksum is
    verified again here rather than assumed. That makes the function safe on any string,
    not only on one that has already validated, which matters because the caller is a
    provider holding a value that came out of a database column.

    **Two networks are indistinguishable for a legacy address**, and the consequence is
    worth stating where it can be read rather than in a commit message. Testnet and regtest
    share both base58 version bytes, so a regtest P2PKH address answers `TESTNET`, and a
    provider configured for regtest therefore refuses it. That is a refusal rather than a
    wrong number, which is the direction this is allowed to be wrong in: a balance read
    against the wrong network is a number, not an error, and nothing downstream can tell it
    from a right one. `bcrt1` is the spelling that answers `REGTEST`.

    `tb` is testnet3, testnet4 and signet alike for the same reason, and that one has no
    refusal to hide behind: an operator pointing an instance at signet while holding
    testnet4 addresses gets confident, wrong answers. Recorded in `docs/providers.md`; the
    address does not carry the fact and no check built out of it can.

    Args:
        canonical: the canonical form of a Bitcoin address, as `ValidatedAddress` carries it.

    Returns:
        The network the address names.

    Raises:
        AddressInvalidError: the string is not a Bitcoin address at all, or its prefix or
            version byte is not one this chain uses. As everywhere in this module, neither
            the message nor the arguments contain the address.
    """
    if _looks_like_bech32(canonical):
        hrp = bech32_decode(canonical).hrp
        network = BITCOIN_NETWORK_BY_HRP.get(hrp)
        if network is None:
            raise AddressInvalidError(AddressRejection.UNKNOWN_PREFIX)
        return network

    version_byte = base58check_decode(canonical)[0]
    network = BITCOIN_NETWORK_BY_VERSION_BYTE.get(version_byte)
    if network is None:
        raise AddressInvalidError(AddressRejection.UNKNOWN_VERSION_BYTE)
    return network


# ---------------------------------------------------------------------------------------
# Which Kaspa network an address belongs to
# ---------------------------------------------------------------------------------------
#
# The same question `bitcoin_network_of` answers, for the same caller: a Kaspa REST server
# serves exactly one network, so an address from another one has to be refused before a URL
# is built out of it. Measured against the public instance on 2026-09-23, the vendor's own
# path validation hard-codes `^kaspa:[a-z0-9]{61,63}$` and answers 422 for anything else --
# which confirms one instance, one network, rather than leaving it assumed.
#
# **Worth reading as a contrast rather than as a copy.** Bitcoin's answer is approximate:
# `tb` is testnet3, testnet4 and signet alike, and the base58 testnet version bytes cover
# regtest too, so `bitcoin_network_of` records a residual nothing can close. Kaspa has no
# such collapse. `kaspa`, `kaspatest` and `kaspadev` are three distinct prefixes and each
# one is folded into the 40-bit checksum, so the same payload checksums differently on each
# network and the same string cannot be read as two of them. The check is exact here and
# approximate there; a reader comparing the two functions deserves to be told which is
# which.


class KaspaNetwork(StrEnum):
    """The Kaspa networks an address can belong to.

    Three members, matching `KASPA_PREFIXES` exactly. `kaspasim` is a real prefix in
    rusty-kaspa and is deliberately not one this application accepts -- a simulation
    network holds nothing worth tracking -- so it is absent from both.
    """

    MAINNET = "mainnet"
    TESTNET = "testnet"
    DEVNET = "devnet"


KASPA_NETWORK_BY_PREFIX: Final[Mapping[str, KaspaNetwork]] = {
    "kaspa": KaspaNetwork.MAINNET,
    "kaspatest": KaspaNetwork.TESTNET,
    "kaspadev": KaspaNetwork.DEVNET,
}
"""The network prefix to its network, from the same `Prefix` enum as `KASPA_PREFIXES`.

Total over `KASPA_PREFIXES` by construction, and a test asserts it: a prefix the codec
accepts but this table does not know would make `kaspa_network_of` raise on an address that
had just validated, which is the kind of contradiction that surfaces as a wallet nobody can
read.

A bijection, unlike `BITCOIN_NETWORK_BY_VERSION_BYTE`. That is a property of the chain
rather than of this table, and it is why the Kaspa wrong-network check has no residual to
record.
"""


def kaspa_network_of(canonical: str) -> KaspaNetwork:
    """Which Kaspa network an address is on, offline, from the address alone.

    **The checksum is verified again here rather than assumed**, by going through
    `kaspa_decode` instead of partitioning on `":"`. That is what makes the function safe
    on any string rather than only on one that has already validated, which matters because
    the caller is a provider holding a value that came out of a database column.

    It is also the difference between a refusal and a confident wrong answer. Kaspa folds
    the prefix into the checksum, so a `kaspa:` payload reprinted under `kaspadev:` is not
    a devnet address -- it is not an address at all. A function that read the prefix and
    looked it up would answer `DEVNET` for it, and a provider configured for devnet would
    then build a URL out of a string no Kaspa node would accept.

    `kaspa_decode` lowercases first, so an uppercase rendering -- what a QR code or a
    hardware wallet screen shows, and what the registry keeps in its `display` column --
    reads as the network it spells rather than as an unknown prefix.

    **A plain subscript, and there is deliberately no unknown-prefix arm**, which is where
    this parts company with `bitcoin_network_of`. That function needs one: `bech32_decode`
    accepts any well-formed human-readable part, so an hrp outside `BITCOIN_HRPS` reaches
    its table and has to be refused there. `kaspa_decode` refuses a prefix outside
    `KASPA_PREFIXES` before it returns, and `KASPA_NETWORK_BY_PREFIX` is total over that
    set -- `tests/domain/test_kaspa_network.py::test_every_prefix_maps_to_its_network` is
    what keeps it so. A guard here would therefore be a branch no test can reach and a
    claim no reader can check, which this codebase has decided is worse than none.

    The obligation that moves with it: a prefix added to `KASPA_PREFIXES` must be added to
    the table in the same change. That is the failing test, not a `KeyError` in production.

    Args:
        canonical: the canonical form of a Kaspa address, as `ValidatedAddress` carries it.
            An uppercase or already-validated string is accepted just the same.

    Returns:
        The network the address names.

    Raises:
        AddressInvalidError: the string is not a Kaspa address -- mixed case, no prefix, an
            unknown prefix, a bad checksum, an unknown version byte, a payload of the wrong
            length. As everywhere in this module, neither the message nor the arguments
            contain the address.
    """
    return KASPA_NETWORK_BY_PREFIX[kaspa_decode(canonical).prefix]
