"""RIPEMD-160, written out by hand, because `hashlib` cannot be relied on to have it.

A Bitcoin address of every form this application derives is built on `HASH160`, which is
RIPEMD-160 over SHA-256. SHA-256 is in every `hashlib`. RIPEMD-160 is not:
`hashlib.new("ripemd160")` is served by OpenSSL, and OpenSSL 3.0.0 to 3.0.6 moved it to the
legacy provider, which a distribution's build may or may not load. Whether the production
image's interpreter has it is something nothing proves before the merge (spec 031, R9), and a
missing hash would surface as every extended-key wallet failing its first sync on the Pi.

So the function is here, in about a hundred lines, pinned by the published test vectors in
`tests/domain/test_ripemd160.py` and by a property test against `hashlib` wherever `hashlib`
does have it. Everything hashed is public data -- a public key, a script -- so constant-time
execution is not a requirement.

**Every table below is transcribed from the algorithm's specification**: H. Dobbertin,
A. Bosselaers and B. Preneel, "RIPEMD-160: A Strengthened Version of RIPEMD" (1996), the
pseudocode in its appendix, as republished at https://homes.esat.kuleuven.be/~bosselae/ripemd160.html.
None is derived from a sample digest, for the reason `domain/addresses.py` gives about its
generator constants: an implementation fitted to its own sample proves only that it agrees
with itself.

Pure: `struct` and integer arithmetic, no I/O.
"""

from __future__ import annotations

import struct
from typing import Final

__all__ = ["DIGEST_SIZE", "ripemd160"]

DIGEST_SIZE: Final = 20
"""Bytes in a digest: five 32-bit words."""

_BLOCK_SIZE: Final = 64
_WORD_MASK: Final = 0xFFFFFFFF
_ROUNDS: Final = 80

_INITIAL_STATE: Final = (0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0)
"""h0 to h4, the chaining variables before the first block."""

# The message word each of the 80 steps reads, left line then right line: r(j) and r'(j).
_LEFT_WORDS: Final = (
    *(0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15),
    *(7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8),
    *(3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12),
    *(1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2),
    *(4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13),
)
_RIGHT_WORDS: Final = (
    *(5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12),
    *(6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2),
    *(15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13),
    *(8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14),
    *(12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11),
)

# The left rotation each step applies, left line then right line: s(j) and s'(j).
_LEFT_SHIFTS: Final = (
    *(11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8),
    *(7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12),
    *(11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5),
    *(11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12),
    *(9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6),
)
_RIGHT_SHIFTS: Final = (
    *(8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6),
    *(9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11),
    *(9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5),
    *(15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8),
    *(8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11),
)

# The additive constant of each round of sixteen steps: K(j) and K'(j).
_LEFT_CONSTANTS: Final = (0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E)
_RIGHT_CONSTANTS: Final = (0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000)

_STEPS_PER_ROUND: Final = 16


def _rotate_left(value: int, shift: int) -> int:
    """Rotate a 32-bit word left by `shift` bits."""
    return ((value << shift) | (value >> (32 - shift))) & _WORD_MASK


def _boolean(round_index: int, x: int, y: int, z: int) -> int:
    """The specification's f(j, x, y, z), selected by round rather than by step.

    The left line runs the five functions in the order 1 to 5 and the right line in the
    order 5 to 1, which `_compress` expresses by passing `4 - round_index` for the right line.
    `~` on a Python int is a negation rather than a 32-bit complement, so every result is
    masked back to a word.
    """
    if round_index == 0:
        result = x ^ y ^ z
    elif round_index == 1:
        result = (x & y) | (~x & z)
    elif round_index == 2:
        result = (x | ~y) ^ z
    elif round_index == 3:
        result = (x & z) | (y & ~z)
    else:
        result = x ^ (y | ~z)
    return result & _WORD_MASK


def _compress(
    state: tuple[int, int, int, int, int], block: bytes
) -> tuple[int, int, int, int, int]:
    """Run one 64-byte block through both lines and fold them into the chaining state."""
    words = struct.unpack("<16I", block)
    h0, h1, h2, h3, h4 = state
    left_a, left_b, left_c, left_d, left_e = state
    right_a, right_b, right_c, right_d, right_e = state

    for step in range(_ROUNDS):
        round_index = step // _STEPS_PER_ROUND

        total = (
            left_a
            + _boolean(round_index, left_b, left_c, left_d)
            + words[_LEFT_WORDS[step]]
            + _LEFT_CONSTANTS[round_index]
        ) & _WORD_MASK
        rotated = (_rotate_left(total, _LEFT_SHIFTS[step]) + left_e) & _WORD_MASK
        left_a, left_e, left_d, left_c, left_b = (
            left_e,
            left_d,
            _rotate_left(left_c, 10),
            left_b,
            rotated,
        )

        total = (
            right_a
            + _boolean(4 - round_index, right_b, right_c, right_d)
            + words[_RIGHT_WORDS[step]]
            + _RIGHT_CONSTANTS[round_index]
        ) & _WORD_MASK
        rotated = (_rotate_left(total, _RIGHT_SHIFTS[step]) + right_e) & _WORD_MASK
        right_a, right_e, right_d, right_c, right_b = (
            right_e,
            right_d,
            _rotate_left(right_c, 10),
            right_b,
            rotated,
        )

    return (
        (h1 + left_c + right_d) & _WORD_MASK,
        (h2 + left_d + right_e) & _WORD_MASK,
        (h3 + left_e + right_a) & _WORD_MASK,
        (h4 + left_a + right_b) & _WORD_MASK,
        (h0 + left_b + right_c) & _WORD_MASK,
    )


def _padded(data: bytes) -> bytes:
    """MD4-style padding: a one bit, zeros to 56 mod 64, then the bit length, little-endian.

    The length is taken modulo 2**64, as the specification says, so no input is refused.
    """
    bit_length = (len(data) * 8) & 0xFFFFFFFFFFFFFFFF
    zeros = (55 - len(data)) % _BLOCK_SIZE
    return data + b"\x80" + b"\x00" * zeros + struct.pack("<Q", bit_length)


def ripemd160(data: bytes) -> bytes:
    """The 20-byte RIPEMD-160 digest of `data`.

    Args:
        data: the message, of any length.

    Returns:
        The digest, the five chaining words written little-endian, as the specification
        and every published test vector render it.
    """
    message = _padded(bytes(data))
    state = _INITIAL_STATE
    for offset in range(0, len(message), _BLOCK_SIZE):
        state = _compress(state, message[offset : offset + _BLOCK_SIZE])
    return struct.pack("<5I", *state)
