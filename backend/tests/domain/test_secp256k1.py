"""Spec 031, criterion 1 (curve): the secp256k1 operations public derivation rests on.

Pure and fast: no fixtures, no I/O. The known multiples of G below are the widely republished
table of small and large multiples of the generator. Each was also reproduced by an
independent implementation, written for this suite and sharing nothing with
`portfolio.domain`, before it was committed here.

The property tests check the group law itself rather than a sample of its outputs: a wrong
doubling formula or a wrong sign in the chord slope produces points that are still on the
curve, so "the result is on the curve" alone proves very little.
"""

from __future__ import annotations

import dataclasses
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from portfolio.domain.secp256k1 import (
    COMPRESSED_POINT_LENGTH,
    CURVE_B,
    CURVE_ORDER,
    FIELD_PRIME,
    GENERATOR,
    InvalidPointError,
    Point,
    add,
    compress,
    decompress,
    multiply,
    multiply_generator,
)

# --------------------------------------------------------------------------------------
# Independent references
# --------------------------------------------------------------------------------------

#: SEC 2, 2.4.1, written as the expression the standard gives rather than copied from the
#: module under test: p = 2^256 - 2^32 - 977.
SEC2_P: Final = 2**256 - 2**32 - 977
SEC2_N: Final = int("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16)
SEC2_G_COMPRESSED: Final = bytes.fromhex(
    "0279BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798"
)

#: k -> (x, y) of k*G.
KNOWN_MULTIPLES: Final[dict[int, tuple[str, str]]] = {
    1: (
        "79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798",
        "483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8",
    ),
    2: (
        "C6047F9441ED7D6D3045406E95C07CD85C778E4B8CEF3CA7ABAC09B95C709EE5",
        "1AE168FEA63DC339A3C58419466CEAEEF7F632653266D0E1236431A950CFE52A",
    ),
    3: (
        "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9",
        "388F7B0F632DE8140FE337E62A37F3566500A99934C2231B6CB9FD7584B8E672",
    ),
    4: (
        "E493DBF1C10D80F3581E4904930B1404CC6C13900EE0758474FA94ABE8C4CD13",
        "51ED993EA0D455B75642E2098EA51448D967AE33BFBDFE40CFE97BDC47739922",
    ),
    5: (
        "2F8BDE4D1A07209355B4A7250A5C5128E88B84BDDC619AB7CBA8D569B240EFE4",
        "D8AC222636E5E3D6D4DBA9DDA6C9C426F788271BAB0D6840DCA87D3AA6AC62D6",
    ),
    20: (
        "4CE119C96E2FA357200B559B2F7DD5A5F02D5290AFF74B03F3E471B273211C97",
        "12BA26DCB10EC1625DA61FA10A844C676162948271D96967450288EE9233DC3A",
    ),
    112233445566778899: (
        "A90CC3D3F3E146DAADFC74CA1372207CB4B725AE708CEF713A98EDD73D99EF29",
        "5A79D6B289610C68BC3B47F3D72F9788A26A06868B4D8E433E1E2AD76FB7DC76",
    ),
    SEC2_N - 1: (
        "79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798",
        "B7C52588D95C3B9AA25B0403F1EEF75702E84BB7597AABE663B82F6F04EF2777",
    ),
}


def point_of(k: int) -> Point:
    x, y = KNOWN_MULTIPLES[k]
    return Point(int(x, 16), int(y, 16))


def on_curve(point: Point) -> bool:
    return (point.y * point.y - point.x**3 - 7) % SEC2_P == 0


def is_quadratic_residue(value: int) -> bool:
    """Euler's criterion, as the independent oracle for "this x has a point above it"."""
    return value % SEC2_P == 0 or pow(value, (SEC2_P - 1) // 2, SEC2_P) == 1


def negate(point: Point) -> Point:
    return Point(point.x, (-point.y) % SEC2_P)


SCALARS: Final = st.integers(min_value=1, max_value=SEC2_N - 1)
SMALL_SCALARS: Final = st.integers(min_value=1, max_value=2**40)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------


def test_the_constants_are_the_sec2_ones() -> None:
    assert FIELD_PRIME == SEC2_P
    assert CURVE_ORDER == SEC2_N
    assert CURVE_B == 7
    assert compress(GENERATOR) == SEC2_G_COMPRESSED
    assert point_of(1) == GENERATOR
    assert COMPRESSED_POINT_LENGTH == 33


def test_the_generator_is_on_the_curve_and_has_order_n() -> None:
    assert on_curve(GENERATOR)
    assert multiply(SEC2_N, GENERATOR) is None
    assert multiply(SEC2_N - 1, GENERATOR) == negate(GENERATOR)


def test_a_point_is_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        GENERATOR.x = 1  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# multiply_generator against known multiples
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("k", list(KNOWN_MULTIPLES), ids=[f"k={k:x}" for k in KNOWN_MULTIPLES])
def test_multiply_generator_matches_the_known_multiples(k: int) -> None:
    assert multiply_generator(k) == point_of(k)
    assert multiply(k, GENERATOR) == point_of(k)


@pytest.mark.parametrize("k", [0, SEC2_N, 2 * SEC2_N, -SEC2_N])
def test_a_multiple_of_the_order_is_the_point_at_infinity(k: int) -> None:
    assert multiply_generator(k) is None


def test_the_scalar_is_reduced_modulo_the_order() -> None:
    assert multiply_generator(SEC2_N + 1) == GENERATOR
    assert multiply_generator(SEC2_N + 5) == point_of(5)
    assert multiply_generator(-1) == point_of(SEC2_N - 1)


def test_multiply_reaches_the_known_multiples_from_another_base() -> None:
    """`multiply` with a base other than G: 4 * (5G) is 20G."""
    assert multiply(4, point_of(5)) == point_of(20)
    assert multiply(1, point_of(3)) == point_of(3)
    assert multiply(0, point_of(3)) is None


# --------------------------------------------------------------------------------------
# The group law
# --------------------------------------------------------------------------------------


def test_add_on_known_points() -> None:
    assert add(point_of(1), point_of(1)) == point_of(2)
    assert add(point_of(1), point_of(2)) == point_of(3)
    assert add(point_of(2), point_of(1)) == point_of(3)
    assert add(point_of(2), point_of(2)) == point_of(4)
    assert add(point_of(2), point_of(3)) == point_of(5)
    assert add(point_of(5), point_of(SEC2_N - 1)) == point_of(4)


def test_infinity_is_the_identity_and_a_point_plus_its_negation_is_infinity() -> None:
    assert add(None, None) is None
    assert add(point_of(3), None) == point_of(3)
    assert add(None, point_of(3)) == point_of(3)
    assert add(point_of(3), negate(point_of(3))) is None
    assert add(point_of(1), point_of(SEC2_N - 1)) is None


def test_two_different_points_with_the_same_x_sum_to_infinity_not_to_a_doubling() -> None:
    """The branch that decides between the chord, the tangent and infinity, from both sides."""
    left = point_of(2)
    assert add(left, negate(left)) is None
    assert add(negate(left), left) is None
    assert add(negate(left), negate(left)) == negate(point_of(4))


@settings(max_examples=40, deadline=None)
@given(a=SCALARS, b=SCALARS)
def test_addition_is_the_sum_of_scalars(a: int, b: int) -> None:
    """aG + bG == (a + b)G, for any a and b: the homomorphism derivation relies on."""
    left = multiply_generator(a)
    right = multiply_generator(b)
    total = add(left, right)
    assert total == multiply_generator(a + b)
    assert add(right, left) == total
    if total is not None:
        assert on_curve(total)


@settings(max_examples=25, deadline=None)
@given(a=SMALL_SCALARS, b=SMALL_SCALARS, c=SMALL_SCALARS)
def test_addition_is_associative(a: int, b: int, c: int) -> None:
    p, q, r = multiply_generator(a), multiply_generator(b), multiply_generator(c)
    assert add(add(p, q), r) == add(p, add(q, r))


@settings(max_examples=30, deadline=None)
@given(k=SCALARS)
def test_doubling_agrees_with_multiplication(k: int) -> None:
    point = multiply_generator(k)
    assert point is not None
    assert on_curve(point)
    assert add(point, point) == multiply_generator(2 * k)
    assert multiply(2, point) == multiply_generator(2 * k)


# --------------------------------------------------------------------------------------
# Compression and decompression
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("k", list(KNOWN_MULTIPLES), ids=[f"k={k:x}" for k in KNOWN_MULTIPLES])
def test_decompression_of_known_points(k: int) -> None:
    point = point_of(k)
    prefix = b"\x03" if point.y % 2 else b"\x02"
    encoded = prefix + point.x.to_bytes(32, "big")

    assert compress(point) == encoded
    assert decompress(encoded) == point


def test_the_two_parities_decompress_to_a_point_and_its_negation() -> None:
    even = decompress(b"\x02" + point_of(2).x.to_bytes(32, "big"))
    odd = decompress(b"\x03" + point_of(2).x.to_bytes(32, "big"))
    assert even.y % 2 == 0
    assert odd.y % 2 == 1
    assert odd == negate(even)


@settings(max_examples=60, deadline=None)
@given(k=SCALARS)
def test_compress_then_decompress_is_the_identity(k: int) -> None:
    point = multiply_generator(k)
    assert point is not None
    encoded = compress(point)
    assert len(encoded) == 33
    assert encoded[0] in (2, 3)
    assert decompress(encoded) == point


@settings(max_examples=200)
@given(x=st.integers(min_value=0, max_value=SEC2_P - 1), odd=st.booleans())
def test_decompress_accepts_exactly_the_x_coordinates_with_a_point(x: int, odd: bool) -> None:
    """Euler's criterion is the oracle: a point exists above x iff x^3 + 7 is a square."""
    encoded = (b"\x03" if odd else b"\x02") + x.to_bytes(32, "big")
    if is_quadratic_residue(x**3 + 7):
        point = decompress(encoded)
        assert point.x == x
        assert point.y % 2 == int(odd)
        assert on_curve(point)
    else:
        with pytest.raises(InvalidPointError):
            decompress(encoded)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(SEC2_G_COMPRESSED[:32], id="32 bytes"),
        pytest.param(SEC2_G_COMPRESSED + b"\x00", id="34 bytes"),
        pytest.param(
            b"\x04" + point_of(1).x.to_bytes(32, "big") + point_of(1).y.to_bytes(32, "big"),
            id="uncompressed 65 bytes",
        ),
        pytest.param(b"\x04" + SEC2_G_COMPRESSED[1:], id="prefix 04"),
        pytest.param(b"\x00" + SEC2_G_COMPRESSED[1:], id="prefix 00"),
        pytest.param(b"\x01" + SEC2_G_COMPRESSED[1:], id="prefix 01"),
        pytest.param(b"\x05" + SEC2_G_COMPRESSED[1:], id="prefix 05"),
        pytest.param(b"\xff" + SEC2_G_COMPRESSED[1:], id="prefix ff"),
        pytest.param(b"\x02" + SEC2_P.to_bytes(32, "big"), id="x equal to p"),
        pytest.param(b"\x03" + (SEC2_P + 2).to_bytes(32, "big"), id="x above p"),
        pytest.param(b"\x02" + (2**256 - 1).to_bytes(32, "big"), id="x all ones"),
        # BIP-32 test vector 5, "invalid pubkey 020000...0007".
        pytest.param(b"\x02" + (7).to_bytes(32, "big"), id="x = 7, no point (BIP-32 TV5)"),
    ],
)
def test_decompress_refuses_what_is_not_a_compressed_point(data: bytes) -> None:
    with pytest.raises(InvalidPointError):
        decompress(data)


def test_x_equal_to_p_would_otherwise_alias_a_real_point() -> None:
    """Why `x >= p` is refused rather than reduced: x = p would read as x = 0.

    Whether 0^3 + 7 is a square decides whether the alias exists; either way the encoding
    with x = p is not a canonical point and must not decompress.
    """
    with pytest.raises(InvalidPointError):
        decompress(b"\x02" + SEC2_P.to_bytes(32, "big"))
    with pytest.raises(InvalidPointError):
        decompress(b"\x03" + (SEC2_P + 1).to_bytes(32, "big"))


def test_the_refusal_is_a_value_error_that_carries_no_bytes() -> None:
    data = b"\x02" + (7).to_bytes(32, "big")
    with pytest.raises(InvalidPointError) as caught:
        decompress(data)

    assert isinstance(caught.value, ValueError)
    assert data.hex() not in str(caught.value)
    assert "07" not in str(caught.value).replace(" ", "")
    assert caught.value.args == (str(caught.value),)
