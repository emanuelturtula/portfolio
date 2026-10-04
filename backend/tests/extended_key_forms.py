"""Other serialisations of one extended key, built byte by byte from BIP32's format.

Review finding S1: two exports of one account that differ only in depth, parent fingerprint
or child number derive exactly the same addresses, so they must register as one wallet. The
application's canonical form re-serialises a key at depth 0 with the other two zeroed. These
helpers compute that form **independently** of `canonical_extended_key` -- from the layout
of the 78-byte payload, not by calling the function under test -- so a test comparing the
two is a comparison and not a tautology.

Everything they produce is in the test network form it was given (R11): the version bytes
are kept unless a test-network version is passed explicitly.
"""

from __future__ import annotations

from typing import Final

from portfolio.domain.addresses import base58check_decode, base58check_encode

#: BIP32's serialisation: version (4), depth (1), parent fingerprint (4), child number (4),
#: chain code (32), public key (33) -- 78 bytes, 82 with the checksum.
SERIALISED_LENGTH: Final = 82
POSITION_START: Final = 4
POSITION_END: Final = 13

#: SLIP-0132's test-network single-signature public versions.
TPUB_VERSION: Final = 0x043587CF
UPUB_VERSION: Final = 0x044A5262
VPUB_VERSION: Final = 0x045F1CF6

_TEST_NETWORK_VERSIONS: Final = frozenset({TPUB_VERSION, UPUB_VERSION, VPUB_VERSION})


def payload_of(key: str) -> bytes:
    """The 78 bytes a serialised key carries, checksum verified and removed."""
    return base58check_decode(key, length=SERIALISED_LENGTH)


def reserialised(key: str, *, version: int | None = None, position: bytes = bytes(9)) -> str:
    """`key` with its tree position (and optionally its version) replaced.

    The chain code and the public key -- everything derivation reads -- are kept.
    """
    if len(position) != POSITION_END - POSITION_START:
        message = "a tree position is nine bytes: depth, parent fingerprint, child number"
        raise ValueError(message)
    if version is not None and version not in _TEST_NETWORK_VERSIONS:
        message = "only a test-network version may be written (R11)"
        raise ValueError(message)
    payload = payload_of(key)
    head = payload[:POSITION_START] if version is None else version.to_bytes(4, "big")
    return base58check_encode(head + position + payload[POSITION_END:])


def depth_zero(key: str) -> str:
    """S1's canonical form: depth 0, a zero parent fingerprint, child number 0."""
    return reserialised(key)


#: The Base58 alphabet: no `0`, `O`, `I` or `l`.
BASE58_ALPHABET: Final = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

#: The only prefixes a key-shaped run may be built with (R11): the test-network private ones.
TEST_PRIVATE_PREFIXES: Final = ("tprv", "uprv", "vprv")


def private_key_shaped_run(prefix: str, length: int = 100) -> str:
    """A test prefix and `length` Base58 characters, assembled in memory (spec 031, R2b).

    R11: no key-shaped string is written to a file, and only a test-network prefix is used
    at key length. The body is the alphabet in a fixed stride, so it is deterministic and is
    no key at all -- no checksum, no structure. The mainnet prefixes are proven on the
    pattern, never on a string of this shape.
    """
    if prefix not in TEST_PRIVATE_PREFIXES:
        message = "a key-shaped run is built with a test-network prefix only (R11)"
        raise ValueError(message)
    body = "".join(BASE58_ALPHABET[(index * 7) % len(BASE58_ALPHABET)] for index in range(length))
    return prefix + body


def _base58_value(text: str) -> int:
    value = 0
    for character in text:
        value = value * 58 + BASE58_ALPHABET.index(character)
    return value


def with_run_inside(key: str, run_prefix: str, at: int) -> str:
    """A **valid** public key like `key` whose characters at `at` spell `run_prefix`.

    The R2b false positive, made on purpose: the string is overwritten at `at`, read back as
    a number, and its top 13 bytes -- version, depth, fingerprint, child number -- are kept
    while the chain code and public key are put back from `key` and the checksum is
    recomputed. The lower bytes cannot reach the leading characters, so the run survives,
    and the result is a well-formed public key on the curve. Built in memory, test network
    only.

    Raises:
        ValueError: no such key exists at that position (the version would change, or a
            carry moved the run); pick another position.
    """
    if run_prefix not in TEST_PRIVATE_PREFIXES:
        message = "the run is built with a test-network prefix only (R11)"
        raise ValueError(message)
    payload = payload_of(key)
    crafted = key[:at] + run_prefix + key[at + len(run_prefix) :]
    top = _base58_value(crafted).to_bytes(SERIALISED_LENGTH, "big")[:POSITION_END]
    result = base58check_encode(top + payload[POSITION_END:])
    if top[:POSITION_START] != payload[:POSITION_START] or result[at : at + 4] != run_prefix:
        message = f"no key like this one carries {run_prefix!r} at {at}"
        raise ValueError(message)
    return result
