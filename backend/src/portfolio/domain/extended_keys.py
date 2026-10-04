"""Bitcoin extended public keys: reading one, deriving its addresses, and the gap limit.

Spec 031. A wallet that hands out a fresh address for every receive and every change output
cannot be tracked one pasted address at a time, and neither public Esplora instance serves a
lookup by extended key, so the derivation happens here: offline, from the key alone.

## What is read, and what is refused

A serialised extended key is Base58Check over 78 bytes (BIP32, "Serialization format"):

| Bytes | Field |
|---|---|
| 4 | version, which SLIP-0132 assigns per network and script type |
| 1 | depth |
| 4 | parent fingerprint |
| 4 | child number |
| 32 | chain code |
| 33 | public key, SEC 1 compressed |

`parse_extended_public_key` refuses, in this order, and each refusal is a fixed sentence that
never quotes the key:

1. **a private prefix, by prefix and before any decoding** (R2): `private_key`. A private key
   with a typo in it is still a private key, and the owner has to be told what it is rather
   than that its checksum failed;
2. **a multisig public prefix, by prefix** (R2): `extended_key_multisig`;
3. a Base58Check decode of exactly 82 bytes: `invalid_character`, `malformed`,
   `bad_checksum`;
4. a version outside the six of `EXTENDED_PUBLIC_KEY_VERSIONS`: `unknown_version_byte`. For
   an 82-byte payload the version fixes the four-character prefix, so a multisig version
   cannot arrive here: it was refused by its prefix in step 2;
5. BIP32's depth-0 rule, a zero parent fingerprint and a zero child number: `malformed`;
6. a public key that is not a point on the curve: `invalid_public_key`.

**The depth is not otherwise enforced** (R4). Electrum exports a depth-1 key and a BIP44
account key is depth 3; derivation is always `/branch/index` below the key as given.

## The script type comes from the prefix, and nothing else (R2)

SLIP-0132's version bytes, https://github.com/satoshilabs/slips/blob/master/slip-0132.md:
`xpub`/`tpub` derive P2PKH (BIP44), `ypub`/`upub` P2SH-P2WPKH (BIP49), `zpub`/`vpub` P2WPKH
(BIP84). A key exported as `xpub` for a segwit account therefore derives P2PKH addresses and
shows zero -- a known limitation, documented in `docs/providers.md`, and the reason the
documentation tells the owner to re-export as `zpub` or `ypub`.

## Derivation

BIP32 "Public parent key -> public child key", non-hardened only, since hardened derivation
needs the private key: `I = HMAC-SHA512(c_par, serP(K_par) || ser32(i))`, `K_i = point(IL) +
K_par`, `c_i = IR`. BIP32 says an index whose `IL >= n` or whose `K_i` is the point at
infinity has no key, and the caller proceeds with the next index. `derive_child` returns
`None` for it; the scan skips it, persists nothing for it, and does not count it toward the
gap (R5). The probability is below 2^-127 per index, so only an injected HMAC reaches it --
which is why `hmac_sha512` is a module-level function looked up at call time.

## Pure

`hashlib` and `hmac` are pure functions of their input. Nothing here opens a socket, reads a
clock or touches a database, and the configured network arrives as an argument.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from portfolio.domain.addresses import (
    BITCOIN_HRP_BY_NETWORK,
    P2PKH_VERSION_BYTE_BY_NETWORK,
    P2SH_VERSION_BYTE_BY_NETWORK,
    AddressInvalidError,
    AddressRejection,
    BitcoinNetwork,
    base58check_decode,
    base58check_encode,
    encode_segwit_address,
)
from portfolio.domain.ripemd160 import ripemd160
from portfolio.domain.secp256k1 import (
    CURVE_ORDER,
    InvalidPointError,
    add,
    compress,
    decompress,
    multiply_generator,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "ALL_EXTENDED_KEY_PREFIXES",
    "CHANGE_BRANCH",
    "EXTENDED_PUBLIC_KEY_VERSIONS",
    "GAP_LIMIT",
    "HARDENED_INDEX",
    "MAX_ADDRESSES_PER_BRANCH",
    "MULTISIG_PUBLIC_PREFIXES",
    "NETWORK_FAMILY_BY_NETWORK",
    "PRIVATE_KEY_PREFIXES",
    "PRIVATE_KEY_RUN_PATTERN",
    "RECEIVE_BRANCH",
    "SINGLE_SIG_PUBLIC_PREFIXES",
    "DerivedKey",
    "ExtendedKeyVersion",
    "ExtendedPublicKey",
    "NetworkFamily",
    "ScriptType",
    "address_of",
    "addresses_to_extend",
    "canonical_extended_key",
    "derive_child",
    "hash160",
    "hmac_sha512",
    "looks_like_private_key",
    "parse_extended_public_key",
]


class NetworkFamily(StrEnum):
    """Which networks an extended key's version admits.

    Two members, not three: SLIP-0132 gives testnet, signet and regtest one set of versions,
    so a `tpub` is a test-network key on all of them and the configured network decides the
    encoding of what it derives (R3).
    """

    MAIN = "main"
    TEST = "test"


class ScriptType(StrEnum):
    """The output script an extended key's addresses pay to, fixed by its prefix (R2)."""

    P2PKH = "p2pkh"
    P2SH_P2WPKH = "p2sh-p2wpkh"
    P2WPKH = "p2wpkh"


@dataclass(frozen=True, slots=True)
class ExtendedKeyVersion:
    """One row of the R2 table: the prefix a version renders as, its family and its script."""

    prefix: str
    network_family: NetworkFamily
    script_type: ScriptType


# Transcribed from SLIP-0132's "Registered HD version bytes" table:
# https://github.com/satoshilabs/slips/blob/master/slip-0132.md
EXTENDED_PUBLIC_KEY_VERSIONS: Final[Mapping[int, ExtendedKeyVersion]] = {
    0x0488B21E: ExtendedKeyVersion("xpub", NetworkFamily.MAIN, ScriptType.P2PKH),
    0x049D7CB2: ExtendedKeyVersion("ypub", NetworkFamily.MAIN, ScriptType.P2SH_P2WPKH),
    0x04B24746: ExtendedKeyVersion("zpub", NetworkFamily.MAIN, ScriptType.P2WPKH),
    0x043587CF: ExtendedKeyVersion("tpub", NetworkFamily.TEST, ScriptType.P2PKH),
    0x044A5262: ExtendedKeyVersion("upub", NetworkFamily.TEST, ScriptType.P2SH_P2WPKH),
    0x045F1CF6: ExtendedKeyVersion("vpub", NetworkFamily.TEST, ScriptType.P2WPKH),
}
"""The six single-signature public versions this application derives from, keyed by version.

**Mainnet support is proven on this table, never on a mainnet key** (R11): rule 3 keeps
every mainnet extended key out of the repository, test fixtures included.
"""

_VERSION_BY_KIND: Final[Mapping[tuple[NetworkFamily, ScriptType], int]] = {
    (version.network_family, version.script_type): number
    for number, version in EXTENDED_PUBLIC_KEY_VERSIONS.items()
}
"""The inverse of `EXTENDED_PUBLIC_KEY_VERSIONS`, for re-serialising a parsed key.

A bijection: each of the six versions is a distinct pair of family and script type, which
is what lets `canonical_extended_key` recover the version from a parsed key alone.
"""

SINGLE_SIG_PUBLIC_PREFIXES: Final[tuple[str, ...]] = tuple(
    version.prefix for version in EXTENDED_PUBLIC_KEY_VERSIONS.values()
)
"""`xpub`, `ypub`, `zpub`, `tpub`, `upub`, `vpub`: the keys registration accepts."""

MULTISIG_PUBLIC_PREFIXES: Final[tuple[str, ...]] = ("Ypub", "Zpub", "Upub", "Vpub")
"""SLIP-0132's multisig public versions, refused by prefix with `extended_key_multisig`.

Capitalised, and the case is the whole difference: `Ypub` and `ypub` are different version
bytes, and Base58 is case sensitive.
"""

PRIVATE_KEY_PREFIXES: Final[tuple[str, ...]] = (
    "xprv",
    "yprv",
    "zprv",
    "tprv",
    "uprv",
    "vprv",
    "Yprv",
    "Zprv",
    "Uprv",
    "Vprv",
)
"""Every SLIP-0132 private prefix, single-signature and multisig, mainnet and test.

Refused by prefix, before any decoding, with `private_key` (R2). Nothing in this
application has a use for a private key, and the owner who pasted one has to be told so in
words that cannot be mistaken for "try again".
"""

PRIVATE_KEY_RUN_PATTERN: Final = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])(?:[xyztuv]|[YZUV])prv[1-9A-HJ-NP-Za-km-z]{100,}"
)
"""A private-key-shaped run in a string: a private prefix that does not continue a Base58
run, then 100+ Base58 characters (R2b).

The two character classes after the lookbehind spell exactly the ten `PRIVATE_KEY_PREFIXES`.
No address of either chain contains a Base58 run that long, so it has no false positive on
an address.

**The left boundary is what keeps it off an extended public key.** A public key's Base58
body is 107 characters long, so without the lookbehind a private prefix that happened to
start at one of the four positions right after the key's own prefix would be followed by
100 or more Base58 characters and would match: a valid public key, roughly one in a few
hundred thousand, refused as `private_key`. The lookbehind makes a
run start only at the beginning of the string or after a character that is not Base58
(whitespace, a quote, punctuation), which a public key's body never contains. The cost is
that a key glued to preceding Base58 text with nothing in between is not found by this run;
it is still refused, because no address or public key contains it, only not by this name.

**Linear.** A one-character lookbehind, one alternation of two classes and a single
bounded-below quantifier, nothing nested: an attempt at a position fails at the first
character that is not Base58, so a string of length n costs at most about 100 * n steps
whatever it holds.
"""

ALL_EXTENDED_KEY_PREFIXES: Final[tuple[str, ...]] = (
    SINGLE_SIG_PUBLIC_PREFIXES + MULTISIG_PUBLIC_PREFIXES + PRIVATE_KEY_PREFIXES
)
"""Every prefix `domain.chains.classify_wallet_key` routes to this module rather than to an
address codec: twenty, public and private, single- and multisig."""

NETWORK_FAMILY_BY_NETWORK: Final[Mapping[BitcoinNetwork, NetworkFamily]] = {
    BitcoinNetwork.MAINNET: NetworkFamily.MAIN,
    BitcoinNetwork.TESTNET: NetworkFamily.TEST,
    BitcoinNetwork.REGTEST: NetworkFamily.TEST,
}
"""The family a configured network admits, for the provider's R3 check before any request."""

GAP_LIMIT: Final = 20
"""BIP44's address gap limit: a branch ends after this many consecutive unused addresses."""

MAX_ADDRESSES_PER_BRANCH: Final = 1000
"""The most addresses a scan will hold on one branch before it refuses (R5).

The only plausible way to reach it in a single-owner tracker is a vendor reporting history
for every address, which is an answer we cannot use -- and must not become an endless scan.
"""

RECEIVE_BRANCH: Final = 0
"""BIP44's external chain: the addresses a wallet hands out to be paid."""

CHANGE_BRANCH: Final = 1
"""BIP44's internal chain: where a wallet sends the change of its own payments."""

HARDENED_INDEX: Final = 2**31
"""BIP32: indices from here up are hardened, and hardened derivation needs the private key."""

_SERIALIZED_KEY_LENGTH: Final = 82
"""BIP32's 78-byte serialisation plus the four-byte Base58Check checksum."""

_VERSION_END: Final = 4
_DEPTH_INDEX: Final = 4
_FINGERPRINT_END: Final = 9
_CHILD_NUMBER_END: Final = 13
_CHAIN_CODE_END: Final = 45
_HALF_DIGEST: Final = 32

_P2WPKH_WITNESS_VERSION: Final = 0
_P2WPKH_REDEEM_PREFIX: Final = b"\x00\x14"
"""BIP141's P2WPKH witness program as BIP49 nests it in P2SH: `OP_0`, then a 20-byte push."""


@dataclass(frozen=True, slots=True)
class ExtendedPublicKey:
    """A parsed and validated extended public key.

    `public_key` is the 33-byte compressed point exactly as serialised, already proven to be
    on the curve. `parent_fingerprint` and `child_number` are carried because BIP32's depth-0
    rule is stated in terms of them, not because derivation reads them.

    **`chain_code` and `public_key` are left out of the `repr`.** Together they are the key
    in substance -- everything derivation reads -- and a `repr` is what a traceback, a
    debugger and a careless f-string print. The value redactor recognises a serialised key
    by its prefix and cannot recognise these as bytes, so they must not reach a log at all.
    """

    network_family: NetworkFamily
    script_type: ScriptType
    depth: int
    parent_fingerprint: bytes
    child_number: int
    chain_code: bytes = field(repr=False)
    public_key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class DerivedKey:
    """A non-hardened child: the index it was derived at, its chain code and its public key.

    The two fields derivation reads have the same names as on `ExtendedPublicKey`, so a
    branch key derived from an account key is derived from again in exactly the same way.
    They are left out of the `repr` for the reason `ExtendedPublicKey` gives: a branch key
    derives every address on its branch.
    """

    index: int
    chain_code: bytes = field(repr=False)
    public_key: bytes = field(repr=False)


def parse_extended_public_key(raw: str) -> ExtendedPublicKey:
    """Read and validate a serialised extended public key, offline.

    The steps and the reason each one raises are in the module docstring. The string is
    taken as given: `classify_wallet_key` strips whitespace before calling this, and Base58
    is case sensitive, so nothing is folded.

    Raises:
        AddressInvalidError: with `private_key`, `extended_key_multisig`,
            `invalid_character`, `malformed`, `bad_checksum`, `unknown_version_byte` or
            `invalid_public_key`. Neither the message nor the arguments contain `raw`.
    """
    if raw.startswith(PRIVATE_KEY_PREFIXES):
        raise AddressInvalidError(AddressRejection.PRIVATE_KEY)
    if raw.startswith(MULTISIG_PUBLIC_PREFIXES):
        raise AddressInvalidError(AddressRejection.EXTENDED_KEY_MULTISIG)

    payload = base58check_decode(raw, length=_SERIALIZED_KEY_LENGTH)
    version = EXTENDED_PUBLIC_KEY_VERSIONS.get(int.from_bytes(payload[:_VERSION_END], "big"))
    if version is None:
        raise AddressInvalidError(AddressRejection.UNKNOWN_VERSION_BYTE)

    depth = payload[_DEPTH_INDEX]
    parent_fingerprint = payload[_DEPTH_INDEX + 1 : _FINGERPRINT_END]
    child_number = int.from_bytes(payload[_FINGERPRINT_END:_CHILD_NUMBER_END], "big")
    # BIP32: a master key, depth 0, has no parent and is no parent's child.
    if depth == 0 and (any(parent_fingerprint) or child_number != 0):
        raise AddressInvalidError(AddressRejection.MALFORMED)

    public_key = payload[_CHAIN_CODE_END:]
    try:
        decompress(public_key)
    except InvalidPointError:
        raise AddressInvalidError(AddressRejection.INVALID_PUBLIC_KEY) from None

    return ExtendedPublicKey(
        network_family=version.network_family,
        script_type=version.script_type,
        depth=depth,
        parent_fingerprint=parent_fingerprint,
        child_number=child_number,
        chain_code=payload[_CHILD_NUMBER_END:_CHAIN_CODE_END],
        public_key=public_key,
    )


def looks_like_private_key(raw: str) -> bool:
    """Whether a value is, or carries, an extended private key (R2b).

    True when either holds:

    1. with every Unicode format character (category `Cf`: zero-width space, word joiner,
       directional marks, a byte-order mark) removed and surrounding whitespace stripped,
       the value starts with one of `PRIVATE_KEY_PREFIXES` -- which also keeps a short
       `xprv`-and-nothing-else a private key, as it always was;
    2. the value, with the same format characters removed, contains a
       `PRIVATE_KEY_RUN_PATTERN` run: a key pasted after other text and a separator, inside
       quotes, or behind an invisible character.

    The run is searched for with the format characters removed, so that an invisible
    character inside a key's body cannot split the run: a key interrupted by a zero-width
    space is still a key to anybody who pastes it. The removal has one other effect: a
    format character that was the only thing between Base58 text and a key no longer
    separates them, and the run's left boundary then does not see a start there -- the
    same answer as for a key glued to that text directly. **The removal is for this test
    only**; the caller validates and stores the value it was given, and an address carrying
    a format character is still refused as `invalid_character`.

    Run on the raw value, before any stripping or length cap, so that a key inside an
    over-long paste is still named for what it is.
    """
    visible = "".join(char for char in raw if unicodedata.category(char) != "Cf")
    return (
        visible.strip().startswith(PRIVATE_KEY_PREFIXES)
        or PRIVATE_KEY_RUN_PATTERN.search(visible) is not None
    )


def canonical_extended_key(key: ExtendedPublicKey) -> str:
    """The key re-serialised at depth 0: what makes two exports of one account one wallet.

    Derivation reads three things and only three: the version, which fixes the script type
    and the network family, the chain code and the public key. Depth, parent fingerprint and
    child number say where the key sits in somebody's tree, and tools disagree about them --
    one exports an account key at depth 3 with its parent's fingerprint, another the same
    bytes at depth 1 or as if it were a master key. Each of those strings derives exactly
    the same addresses, so keyed on the string as typed they register as two wallets and the
    total silently doubles.

    So the canonical form is BIP32's serialisation with the version, chain code and public
    key kept and the other three zeroed: depth 0, a zero parent fingerprint, child number 0.
    That satisfies the depth-0 rule, so it parses again (R4), and it is what the unique
    constraint and the duplicate check compare and what the provider is handed.

    **The version is kept.** A P2WPKH and a P2PKH version over the same chain code and key
    derive different addresses: two wallets, correctly.
    """
    payload = (
        _VERSION_BY_KIND[key.network_family, key.script_type].to_bytes(_VERSION_END, "big")
        + bytes([0])
        + bytes(_FINGERPRINT_END - _DEPTH_INDEX - 1)
        + bytes(_CHILD_NUMBER_END - _FINGERPRINT_END)
        + key.chain_code
        + key.public_key
    )
    return base58check_encode(payload)


def hmac_sha512(key: bytes, data: bytes) -> bytes:
    """HMAC-SHA512, the function BIP32 derives every child from.

    Module level, and looked up at call time by `derive_child`, so that a test can inject a
    digest whose left half is at least n, or which sends the child to the point at
    infinity: the invalid-index cases R5 requires the scan to skip, and which no real key
    reaches.
    """
    return hmac.new(key, data, hashlib.sha512).digest()


def derive_child(parent: ExtendedPublicKey | DerivedKey, index: int) -> DerivedKey | None:
    """BIP32's public-parent-to-public-child derivation, non-hardened.

    Args:
        parent: an extended key as parsed, or a key derived from one -- a branch key.
        index: the child index, below 2^31.

    Returns:
        The child, or `None` when BIP32 says the index has no key: `IL >= n`, or the child
        is the point at infinity. A caller skips it and moves to the next index.

    Raises:
        ValueError: the index is negative or hardened. A programming error, never data:
            the scan only counts upward from zero.
    """
    if not 0 <= index < HARDENED_INDEX:
        message = "Only a non-hardened index, from 0 to 2^31 - 1, can be derived publicly."
        raise ValueError(message)
    digest = hmac_sha512(parent.chain_code, parent.public_key + index.to_bytes(4, "big"))
    tweak = int.from_bytes(digest[:_HALF_DIGEST], "big")
    if tweak >= CURVE_ORDER:
        return None
    child = add(multiply_generator(tweak), decompress(parent.public_key))
    if child is None:
        return None
    return DerivedKey(index=index, chain_code=digest[_HALF_DIGEST:], public_key=compress(child))


def hash160(data: bytes) -> bytes:
    """RIPEMD-160 of SHA-256: the hash every address form here commits to."""
    return ripemd160(hashlib.sha256(data).digest())


def address_of(public_key: bytes, script_type: ScriptType, network: BitcoinNetwork) -> str:
    """The address a derived public key pays to, encoded for the configured network (R3).

    | Script | Encoding |
    |---|---|
    | P2PKH (BIP44) | Base58Check, version `0x00` on mainnet, `0x6F` on test networks |
    | P2SH-P2WPKH (BIP49) | Base58Check of HASH160(`OP_0 <key hash>`), `0x05` / `0xC4` |
    | P2WPKH (BIP84) | bech32, witness version 0, hrp `bc`, `tb` or `bcrt` |

    The result is in canonical form -- Base58 as it is, bech32 lowercase -- so it validates
    through `validate_bitcoin_address` and comes back unchanged as its own canonical form,
    which is what lets a derived address be stored, compared and requested like a
    registered one.
    """
    key_hash = hash160(public_key)
    if script_type is ScriptType.P2PKH:
        return base58check_encode(bytes([P2PKH_VERSION_BYTE_BY_NETWORK[network]]) + key_hash)
    if script_type is ScriptType.P2SH_P2WPKH:
        script_hash = hash160(_P2WPKH_REDEEM_PREFIX + key_hash)
        return base58check_encode(bytes([P2SH_VERSION_BYTE_BY_NETWORK[network]]) + script_hash)
    return encode_segwit_address(BITCOIN_HRP_BY_NETWORK[network], _P2WPKH_WITNESS_VERSION, key_hash)


def addresses_to_extend(used_by_index: Sequence[bool]) -> int:
    """How many more addresses a branch needs before its gap is complete (R5).

    A branch is complete when its last `GAP_LIMIT` addresses, by index, are unused, counted
    after its highest used one. A branch with no used address is complete at `GAP_LIMIT`
    addresses.

    Args:
        used_by_index: whether each of the branch's addresses has been used, in ascending
            index order. An index skipped because it has no key is simply absent, so it
            never counts toward the gap.

    Returns:
        The number of further addresses to derive and read, zero when the branch is
        complete. The caller reads them and asks again, since one of them may be used.
    """
    trailing_unused = 0
    for used in reversed(used_by_index):
        if used:
            break
        trailing_unused += 1
    return max(0, GAP_LIMIT - trailing_unused)
