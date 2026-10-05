"""Spec 031, R2a and R2b: no private prefix gets past `classify_wallet_key`'s first test.

`classify_wallet_key` refuses `looks_like_private_key(raw)` first of all, then strips the
value and routes it by prefix. Its Kaspa branch refuses every extended-key prefix as
`extended_key` without asking whether the prefix is a private one, and the Bitcoin parser
refuses a private prefix again only as a backstop. Both are right only because of the
implication pinned here: a value whose stripped form starts with a private prefix is always
`looks_like_private_key`, so it is refused as `private_key` before either branch sees it.

It holds because removing format characters never removes a prefix letter, and both sides
strip with the same `str.strip`. A change to `looks_like_private_key` that opened the gap --
dropping the strip, or the format-character removal -- would let a padded private key on
Kaspa be named a public one, and fails here first.

Nothing key-shaped is written: the prefixes come from the module under test, and every
generated value lives in memory only (R11).
"""

from __future__ import annotations

import sys
import unicodedata
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from portfolio.domain.extended_keys import PRIVATE_KEY_PREFIXES, looks_like_private_key

_WHITESPACE: Final = tuple(chr(code) for code in range(sys.maxunicode + 1) if chr(code).isspace())
_FORMAT: Final = tuple(
    chr(code) for code in range(sys.maxunicode + 1) if unicodedata.category(chr(code)) == "Cf"
)
_PADDING: Final = st.text(alphabet=st.sampled_from(_WHITESPACE + _FORMAT), max_size=6)


def test_the_padding_alphabet_is_what_it_claims() -> None:
    """Both halves are present, so the property below is not vacuous about either."""
    assert {" ", "\t", "\n", "　"} <= set(_WHITESPACE)
    assert {"​", "⁠", "﻿", "‎"} <= set(_FORMAT)


@settings(max_examples=500, deadline=None)
@given(
    lead=_PADDING,
    prefix=st.sampled_from(PRIVATE_KEY_PREFIXES),
    body=st.text(max_size=40),
    trail=_PADDING,
)
def test_a_private_prefix_behind_any_whitespace_or_format_character_is_a_private_key(
    lead: str, prefix: str, body: str, trail: str
) -> None:
    # Every value `classify_wallet_key` would strip to a private prefix is among these, so
    # the first test answers for all of them and no later branch ever sees one.
    assert looks_like_private_key(f"{lead}{prefix}{body}{trail}")
