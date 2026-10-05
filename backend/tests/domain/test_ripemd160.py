"""Spec 031, criterion 1 (hash): RIPEMD-160, written by hand because `hashlib` may lack it.

The vectors are the ones published with the algorithm, on the RIPEMD-160 page of its authors
(https://homes.esat.kuleuven.be/~bosselae/ripemd160.html, "test values"). The property test
compares against `hashlib` where this interpreter's OpenSSL still serves RIPEMD-160, and is
skipped where it does not -- which is exactly the situation R9 says production may be in.
"""

from __future__ import annotations

import hashlib
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from portfolio.domain.ripemd160 import DIGEST_SIZE, ripemd160


def _hashlib_has_ripemd160() -> bool:
    try:
        hashlib.new("ripemd160")
    except ValueError:
        return False
    return True


HASHLIB_HAS_RIPEMD160: Final = _hashlib_has_ripemd160()

#: The published test values: message -> digest.
PUBLISHED: Final[tuple[tuple[bytes, str], ...]] = (
    (b"", "9c1185a5c5e9fc54612808977ee8f548b2258d31"),
    (b"a", "0bdc9d2d256b3ee9daae347be6f4dc835a467ffe"),
    (b"abc", "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc"),
    (b"message digest", "5d0689ef49d2fae572b881b123a85ffa21595f36"),
    (b"abcdefghijklmnopqrstuvwxyz", "f71c27109c692c1b56bbdceb5b9d2865b3708dbc"),
    (
        b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
        "12a053384a9c0c88e405a06c27dcf49ada62eb2b",
    ),
    (
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
        "b0e20b6e3116640286ed3a87a5713079b21f5189",
    ),
    (b"1234567890" * 8, "9b752e45573d4b39f4dbd3323cab82bf63326bfb"),
)

#: The published million-`a` value: 15,625 blocks, which exercises the chaining between
#: blocks far more than any short message can.
MILLION_A_DIGEST: Final = "52783243c1697bdbe16d37f97f68f08325dc1528"


@pytest.mark.parametrize(
    ("message", "digest"), PUBLISHED, ids=[repr(m[:16]) for m, _d in PUBLISHED]
)
def test_the_published_vectors(message: bytes, digest: str) -> None:
    assert ripemd160(message).hex() == digest


def test_the_published_million_a_vector() -> None:
    assert ripemd160(b"a" * 1_000_000).hex() == MILLION_A_DIGEST


def test_the_digest_is_twenty_bytes() -> None:
    assert DIGEST_SIZE == 20
    assert len(ripemd160(b"")) == 20
    assert isinstance(ripemd160(b"abc"), bytes)


def test_the_hash160_of_a_published_public_key() -> None:
    """BIP-49's published `keyhash = HASH160(account0recvPublicKeyHex)`, composed by hand."""
    public_key = bytes.fromhex("03a1af804ac108a8a51782198c2d034b28bf90c8803f5a53f76276fa69a4eae77f")
    keyhash = ripemd160(hashlib.sha256(public_key).digest())
    assert keyhash.hex() == "38971f73930f6c141d977ac4fd4a727c854935b3"


#: Every length around the padding boundaries: 55 is the last that fits the length in the
#: same block, 56 the first that does not, and 64 and 128 are whole blocks.
BOUNDARY_LENGTHS: Final = (0, 1, 54, 55, 56, 57, 63, 64, 65, 111, 119, 120, 127, 128, 129, 191, 192)


@pytest.mark.skipif(not HASHLIB_HAS_RIPEMD160, reason="this OpenSSL does not serve ripemd160")
@pytest.mark.parametrize("length", BOUNDARY_LENGTHS)
def test_padding_boundaries_agree_with_hashlib(length: int) -> None:
    message = bytes((index * 7 + 3) % 256 for index in range(length))
    assert ripemd160(message) == hashlib.new("ripemd160", message).digest()


@pytest.mark.skipif(not HASHLIB_HAS_RIPEMD160, reason="this OpenSSL does not serve ripemd160")
@settings(max_examples=200)
@given(message=st.binary(max_size=300))
def test_any_message_agrees_with_hashlib(message: bytes) -> None:
    assert ripemd160(message) == hashlib.new("ripemd160", message).digest()
