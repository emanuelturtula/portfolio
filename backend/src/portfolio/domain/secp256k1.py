"""The secp256k1 group operations BIP32 public derivation needs, and nothing more.

Public derivation of a child key is `point(IL) + K_parent`: one multiplication of the
generator by a scalar, and one point addition. Reading an extended key needs a compressed
point turned back into coordinates, and writing an address needs the reverse. Those four
operations are this module, written out by hand because spec 031 (R9) refuses a dependency for
about a hundred lines that the published vectors pin exactly.

**Everything handled here is public**: a public key, a chain code's tweak, a child public key.
No private scalar ever enters this module, so constant-time execution is not a requirement
and the arithmetic is the plainest correct form rather than a hardened one.

**The curve constants are transcribed from SEC 2, "Recommended Elliptic Curve Domain
Parameters", version 2.0 (Certicom Research, 2010), section 2.4.1**:
https://www.secg.org/sec2-v2.pdf. They are not derived from a sample point, for the reason
`domain/addresses.py` gives about its generator constants.

The point at infinity is `None` rather than a sentinel `Point`, so that every function that
can produce it says so in its return type and a caller cannot forget to look.

Pure: integer arithmetic only. No I/O, no clock, no randomness.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "COMPRESSED_POINT_LENGTH",
    "CURVE_B",
    "CURVE_ORDER",
    "FIELD_PRIME",
    "GENERATOR",
    "InvalidPointError",
    "Point",
    "add",
    "compress",
    "decompress",
    "multiply",
    "multiply_generator",
]

FIELD_PRIME: Final = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
"""SEC 2, 2.4.1: p = 2^256 - 2^32 - 2^9 - 2^8 - 2^7 - 2^6 - 2^4 - 1."""

CURVE_B: Final = 7
"""SEC 2, 2.4.1: the curve is y^2 = x^3 + 7 over F_p. Its `a` is zero, so it appears nowhere."""

CURVE_ORDER: Final = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
"""SEC 2, 2.4.1: n, the order of the generator. The cofactor is 1, so every point is in it."""

_GENERATOR_X: Final = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GENERATOR_Y: Final = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

COMPRESSED_POINT_LENGTH: Final = 33
"""SEC 1, 2.3.3: a parity byte, `02` or `03`, then x as 32 big-endian bytes."""

_EVEN_PREFIX: Final = 0x02
_ODD_PREFIX: Final = 0x03
_COORDINATE_LENGTH: Final = 32

_SQUARE_ROOT_EXPONENT: Final = (FIELD_PRIME + 1) // 4
"""p is 3 mod 4, so a square root of a quadratic residue r is r^((p + 1) / 4)."""


class InvalidPointError(ValueError):
    """Bytes that are not a compressed point on the curve.

    The message is a fixed sentence. It never carries the bytes: they are a public key, and
    a public key identifies a wallet.
    """

    def __init__(self) -> None:
        super().__init__("The bytes are not a compressed secp256k1 point.")


@dataclass(frozen=True, slots=True)
class Point:
    """A point on the curve, in affine coordinates. Never the point at infinity: that is `None`."""

    x: int
    y: int


GENERATOR: Final = Point(_GENERATOR_X, _GENERATOR_Y)
"""SEC 2, 2.4.1: G, given there in its uncompressed form."""


def _curve_rhs(x: int) -> int:
    """x^3 + 7 mod p: what y^2 must equal for (x, y) to be on the curve."""
    return (pow(x, 3, FIELD_PRIME) + CURVE_B) % FIELD_PRIME


def decompress(data: bytes) -> Point:
    """The point a 33-byte SEC 1 compressed encoding names.

    Three refusals, each of which a BIP32 parser must make (BIP32, "Serialization format":
    the key must be a valid point): a length other than 33, a prefix other than `02` or
    `03`, and an x coordinate that is not below p or has no point above it. The last is the
    one that matters in practice: about half of all 32-byte strings are not the x of any
    point, so a key body that was corrupted rather than mistyped is caught here rather than
    deriving addresses nobody can spend to.

    Raises:
        InvalidPointError: any of the three.
    """
    if len(data) != COMPRESSED_POINT_LENGTH or data[0] not in (_EVEN_PREFIX, _ODD_PREFIX):
        raise InvalidPointError
    x = int.from_bytes(data[1:], "big")
    if x >= FIELD_PRIME:
        raise InvalidPointError
    rhs = _curve_rhs(x)
    y = pow(rhs, _SQUARE_ROOT_EXPONENT, FIELD_PRIME)
    # The exponentiation always returns something; only squaring it back says whether that
    # something is a root, which is the on-curve check itself.
    if y * y % FIELD_PRIME != rhs:
        raise InvalidPointError
    if y % 2 != data[0] % 2:
        y = FIELD_PRIME - y
    return Point(x, y)


def compress(point: Point) -> bytes:
    """The 33-byte SEC 1 compressed encoding of a point: parity, then x."""
    prefix = _ODD_PREFIX if point.y % 2 else _EVEN_PREFIX
    return bytes([prefix]) + point.x.to_bytes(_COORDINATE_LENGTH, "big")


def add(left: Point | None, right: Point | None) -> Point | None:
    """The group law: `left + right`, where `None` is the point at infinity.

    The textbook affine formulas, with the three special cases every one of them needs:
    infinity is the identity, a point plus its negation is infinity, and a point plus itself
    is a doubling, whose slope is the tangent's rather than the chord's.
    """
    if left is None:
        return right
    if right is None:
        return left
    if left.x == right.x:
        if (left.y + right.y) % FIELD_PRIME == 0:
            return None
        slope = 3 * left.x * left.x * pow(2 * left.y, -1, FIELD_PRIME) % FIELD_PRIME
    else:
        slope = (right.y - left.y) * pow(right.x - left.x, -1, FIELD_PRIME) % FIELD_PRIME
    x = (slope * slope - left.x - right.x) % FIELD_PRIME
    y = (slope * (left.x - x) - left.y) % FIELD_PRIME
    return Point(x, y)


def multiply(scalar: int, point: Point) -> Point | None:
    """`scalar * point`, by double-and-add over the scalar's bits.

    The scalar is reduced modulo n first, so zero and every multiple of n give the point at
    infinity rather than an error: the group has order n, and that is what those products
    are. A negative scalar is reduced the same way, which is the negation it means.

    Not constant time, and it does not need to be: see the module docstring.
    """
    remaining = scalar % CURVE_ORDER
    result: Point | None = None
    addend: Point | None = point
    while remaining:
        if remaining & 1:
            result = add(result, addend)
        addend = add(addend, addend)
        remaining >>= 1
    return result


def multiply_generator(scalar: int) -> Point | None:
    """`scalar * G`: the public point of a scalar, and the tweak BIP32 adds to a parent key."""
    return multiply(scalar, GENERATOR)
