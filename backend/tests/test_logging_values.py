"""Spec 030 (#23), criteria 1, 2 and 4: the value rule, `ValueRedactor`, on its own.

The processor is exercised directly, so each rule is pinned at its edges without a pipeline
in the way: the eight-character threshold, longest first, overlapping secrets merged (R13),
the three spellings of a secret, each extended-key prefix and each address form at its length
limits and between its separators (R14), the URL query (R13), the repetition until a pass
changes nothing and its bound (R12, R15, R17), every rule's time on 200 KB of adversarial text
(R13, M1), the walk through containers, keys included, and the `repr` of an object. The
pipeline that runs it is `tests/security/test_log_pipeline.py`'s.

## Testnet only

Every address and key below is a testnet vector from `tests/address_vectors.py` or a slice of
one. The **mainnet** alternatives are proven on the pattern's own source -- that the bech32
rule's human-readable parts include `bc`, that the Base58 rule's first characters include `1`
and `3`, that the Kaspa rule's prefixes include `kaspa`, that the extended-key rule's prefixes
include `xpub` -- never by committing or assembling a mainnet-shaped string (rule 3).
"""

from __future__ import annotations

import json
import re
import string
import time
from typing import Any, Final

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

from portfolio.config import Settings
from portfolio.logging import (
    ADDRESS_ENDS,
    ADDRESS_PATTERNS,
    ADDRESS_STARTS,
    EXTENDED_KEY_PATTERN,
    EXTENDED_PRIVATE_KEY_PREFIXES,
    EXTENDED_PUBLIC_KEY_PREFIXES,
    MAX_REDACTION_PASSES,
    MIN_SUBSTRING_SECRET_LENGTH,
    REDACTED,
    REQUEST_ID_KEY,
    ValueRedactor,
    redact_url_queries,
    secret_values,
)
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WPKH_UPPERCASE,
    BIP173_TESTNET_P2WSH,
    BIP350_MIXED_CASE,
    BIP350_TESTNET_V1,
    CORE_REGTEST_P2SH,
    CORE_REGTEST_P2WPKH,
    CORE_REGTEST_V1,
    CORE_SIGNET_P2PKH,
    CORE_TESTNET4_P2PKH,
    CORE_TESTNET4_P2SH,
    CORE_TESTNET4_V1,
    KASPA_MIXED_CASE,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V0_ASPECTRON,
    KASPA_TESTNET_V1_KEY,
    KASPA_TESTNET_V1_ZERO,
    SYNTHETIC_TPUB,
)

#: Every address form the application accepts, in its testnet spelling.
ADDRESSES: Final = (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WSH,
    BIP173_TESTNET_P2WPKH_UPPERCASE,
    BIP350_TESTNET_V1,
    BIP350_MIXED_CASE,
    CORE_TESTNET4_V1,
    CORE_REGTEST_P2WPKH,
    CORE_REGTEST_V1,
    CORE_TESTNET4_P2PKH,
    CORE_SIGNET_P2PKH,
    CORE_TESTNET4_P2SH,
    CORE_REGTEST_P2SH,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V0_ASPECTRON,
    KASPA_TESTNET_V1_ZERO,
    KASPA_TESTNET_V1_KEY,
    KASPA_MIXED_CASE,
)

#: A secret of exactly the threshold, and one a character short of it. Synthetic.
EIGHT: Final = "s3ntinel"
SEVEN: Final = "s3ntine"

#: The `Settings` fields that hold a credential, written out rather than read off the model.
SECRET_FIELDS: Final = frozenset(
    {
        "bootstrap_password",
        "coingecko_api_key",
    }
)


def redacted(text: str, *secrets: str) -> str:
    return ValueRedactor(secrets).redact_text(text)


# --------------------------------------------------------------------------------------
# The secret set
# --------------------------------------------------------------------------------------


def secret_settings(**values: str) -> Settings:
    """Settings from these values alone: no `.env` a developer happens to have is read."""
    return Settings(
        _env_file=None,
        environment="dev",
        **{name: SecretStr(value) for name, value in values.items()},  # type: ignore[arg-type]
    )


def test_every_secret_field_is_found_and_unwrapped() -> None:
    """Each field set to a distinct value: the set is exactly those values, unwrapped."""
    values = {name: f"sentinel-{index:02d}-{name}" for index, name in enumerate(SECRET_FIELDS)}

    assert secret_values(secret_settings(**values)) == frozenset(values.values())


def test_the_fields_are_every_secret_str_on_the_model_and_no_other() -> None:
    """Read off the annotations independently: a new credential field joins `SECRET_FIELDS`."""
    annotated = {
        name
        for name, field in Settings.model_fields.items()
        if "SecretStr" in str(field.annotation)
    }

    assert annotated == SECRET_FIELDS


def test_a_secret_field_added_later_is_found_without_an_edit() -> None:
    """By introspection: a subclass's new field is in the set, with no list to update."""

    class Extended(Settings):
        future_vendor_api_key: SecretStr | None = None

    extended = Extended(
        _env_file=None,
        environment="dev",
        future_vendor_api_key=SecretStr("sentinel-from-a-later-field"),
        coingecko_api_key=SecretStr("sentinel-coingecko-key"),
    )

    assert secret_values(extended) == frozenset(
        {"sentinel-from-a-later-field", "sentinel-coingecko-key"}
    )


def test_an_unset_or_empty_secret_is_not_in_the_set() -> None:
    """An empty secret would be a pattern that matches everywhere."""
    assert secret_values(Settings(_env_file=None, environment="dev")) == frozenset()
    assert secret_values(secret_settings(coingecko_api_key="")) == frozenset()


def test_an_empty_secret_given_to_the_redactor_redacts_nothing() -> None:
    assert redacted("ordinary words", "") == "ordinary words"
    assert redacted("", "") == ""


# --------------------------------------------------------------------------------------
# Secrets: the threshold, longest first, the spellings
# --------------------------------------------------------------------------------------


def test_the_threshold_is_eight() -> None:
    assert MIN_SUBSTRING_SECRET_LENGTH == 8
    assert len(EIGHT) == 8
    assert len(SEVEN) == 7


def test_a_secret_of_eight_characters_is_redacted_inside_a_string() -> None:
    assert redacted(f"key={EIGHT}&x", EIGHT) == f"key={REDACTED}&x"
    assert redacted(f"{EIGHT}{EIGHT}", EIGHT) == REDACTED * 2


def test_a_shorter_secret_is_redacted_only_as_the_whole_string() -> None:
    assert redacted(SEVEN, SEVEN) == REDACTED
    assert redacted(f"key={SEVEN}", SEVEN) == f"key={SEVEN}"
    assert redacted(f" {SEVEN}", SEVEN) == f" {SEVEN}"


def test_a_secret_that_contains_another_is_replaced_whole() -> None:
    """Longest first: the shorter one never leaves the longer one's tail behind."""
    longer = EIGHT + "-and-more"
    for order in ((EIGHT, longer), (longer, EIGHT)):
        assert redacted(f"a {longer} b {EIGHT} c", *order) == f"a {REDACTED} b {REDACTED} c"


def test_two_secrets_of_the_same_length_are_both_replaced() -> None:
    assert redacted("aaaaaaaa bbbbbbbb", "bbbbbbbb", "aaaaaaaa") == f"{REDACTED} {REDACTED}"


#: Two synthetic secrets that overlap by six characters, and the text holding both (R13).
OVERLAP_LEFT: Final = "XXXXYYYYZZ"
OVERLAP_RIGHT: Final = "YYYYZZZZWW"
OVERLAPPING: Final = "XXXXYYYYZZZZWW"


def test_two_overlapping_secrets_leave_no_fragment_of_either() -> None:
    """R13: one replacement per secret left `[REDACTED]ZZWW` -- the end of the second.

    Merged spans become one marker, whichever secret is given first.
    """
    assert OVERLAP_LEFT in OVERLAPPING
    assert OVERLAP_RIGHT in OVERLAPPING
    for order in ((OVERLAP_LEFT, OVERLAP_RIGHT), (OVERLAP_RIGHT, OVERLAP_LEFT)):
        out = redacted(f"<{OVERLAPPING}>", *order)

        assert out == f"<{REDACTED}>"
        assert not set("XYZW") & set(out)


def test_overlapping_secrets_apart_are_two_markers() -> None:
    assert redacted(f"{OVERLAP_LEFT} and {OVERLAP_RIGHT}", OVERLAP_LEFT, OVERLAP_RIGHT) == (
        f"{REDACTED} and {REDACTED}"
    )


def test_a_secret_that_overlaps_itself_is_one_marker() -> None:
    """`ABABABAB` occurs at 1 and at 3 in `xABABABABABx`: one span, merged."""
    assert redacted("xABABABABABx", "ABABABAB") == f"x{REDACTED}x"


def test_secrets_that_only_touch_stay_two_markers() -> None:
    """A span that ends where the next starts is not merged: the count of secrets survives."""
    assert redacted(f"{OVERLAP_LEFT}{OVERLAP_RIGHT}", OVERLAP_LEFT, OVERLAP_RIGHT) == (REDACTED * 2)
    assert redacted(f"[{EIGHT}{EIGHT}{EIGHT}]", EIGHT) == f"[{REDACTED * 3}]"


def test_a_secret_inside_another_inside_a_string_leaves_nothing() -> None:
    """The shorter secret's spans fall inside the longer one's, and are merged into it."""
    longer = "ABCDEFGHIJKL"
    inner = "DEFGHIJK"
    assert redacted(f"<{longer}> {inner}", inner, longer) == f"<{REDACTED}> {REDACTED}"


#: A secret that JSON and `repr` each spell differently: a quote, a backslash, a non-ASCII
#: letter and a control character. Synthetic.
AWKWARD: Final = 'pa"ss\\w\u00f6rd\t-sentinel'


@pytest.mark.parametrize(
    "spelling",
    [AWKWARD, json.dumps(AWKWARD)[1:-1], repr(AWKWARD)[1:-1], json.dumps(AWKWARD)],
    ids=["raw", "json", "repr", "json-quoted"],
)
def test_a_secret_is_redacted_in_each_of_its_spellings(spelling: str) -> None:
    out = redacted(f"before {spelling} after", AWKWARD)

    assert "sentinel" not in out
    assert out.startswith("before ")
    assert out.endswith(" after")


def test_the_three_spellings_differ_so_each_is_a_case_of_its_own() -> None:
    assert len({AWKWARD, json.dumps(AWKWARD)[1:-1], repr(AWKWARD)[1:-1]}) == 3


def test_a_secret_is_matched_literally_not_as_a_pattern() -> None:
    """A secret full of metacharacters: escaped, so it matches itself and nothing else."""
    metacharacters = "a.b*c+d?(e)[f]"
    assert redacted(f"x {metacharacters} y", metacharacters) == f"x {REDACTED} y"
    assert redacted("x aXbbbcddde y", metacharacters) == "x aXbbbcddde y"


# --------------------------------------------------------------------------------------
# Extended public keys
# --------------------------------------------------------------------------------------


def test_every_listed_prefix_is_an_alternative_of_the_pattern() -> None:
    """The mainnet prefixes proven on the pattern's source, not on a committed key.

    Spec 031 widened the pattern to the private prefixes as well, public first and in the
    order each tuple declares them, so a private key that reaches a log is redacted like a
    public one. The pattern was renamed `EXTENDED_KEY_PATTERN` with it.
    """
    assert EXTENDED_PUBLIC_KEY_PREFIXES == (
        "xpub",
        "ypub",
        "zpub",
        "tpub",
        "upub",
        "vpub",
        "Ypub",
        "Zpub",
        "Upub",
        "Vpub",
    )
    assert EXTENDED_PRIVATE_KEY_PREFIXES == (
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
    alternation = re.match(r"\(\?:([A-Za-z|]+)\)", EXTENDED_KEY_PATTERN.pattern)
    assert alternation is not None
    assert alternation.group(1).split("|") == [
        *EXTENDED_PUBLIC_KEY_PREFIXES,
        *EXTENDED_PRIVATE_KEY_PREFIXES,
    ]
    assert EXTENDED_KEY_PATTERN.pattern.endswith("[1-9A-HJ-NP-Za-km-z]{100,}")
    assert not EXTENDED_KEY_PATTERN.flags & re.IGNORECASE


def test_a_testnet_extended_key_is_redacted_wherever_it_occurs() -> None:
    assert redacted(SYNTHETIC_TPUB) == REDACTED
    assert redacted(f"derive from {SYNTHETIC_TPUB}/0/1 now") == f"derive from {REDACTED}/0/1 now"


def test_an_extended_key_needs_a_hundred_characters_after_its_prefix() -> None:
    body = SYNTHETIC_TPUB[4:]
    assert len(body) >= 100

    assert redacted("tpub" + body[:100]) == REDACTED
    assert redacted("tpub" + body[:99]) == "tpub" + body[:99]


def private_shaped(prefix: str) -> str:
    """A string the pattern reads as a private key: a private prefix over a *public* key's body.

    Assembled at run time with `join`, which CPython does not fold, so no file -- source or
    `.pyc` -- ever holds a private-key-shaped literal (spec 031, R11). It is no key at all:
    the version is wrong for the bytes and the checksum fails.
    """
    return "".join((prefix, SYNTHETIC_TPUB[4:]))


@pytest.mark.parametrize(
    "prefix", ["xprv", "yprv", "zprv", "tprv", "uprv", "vprv", "Yprv", "Zprv", "Uprv", "Vprv"]
)
def test_a_private_extended_key_is_redacted_by_value(prefix: str) -> None:
    """Spec 031, criterion 7: a private key that reaches a log is redacted like a public one."""
    value = private_shaped(prefix)
    assert redacted(value) == REDACTED
    assert redacted(f"refused {value} at registration") == f"refused {REDACTED} at registration"


def test_a_private_prefix_needs_a_hundred_characters_after_it_too() -> None:
    body = SYNTHETIC_TPUB[4:]
    assert redacted("".join(("tprv", body[:100]))) == REDACTED
    assert redacted("".join(("tprv", body[:99]))) == "".join(("tprv", body[:99]))


def test_an_extended_key_prefix_is_matched_in_its_own_case() -> None:
    """`TPUB` is no version byte; the case is part of the prefix."""
    shouted = "TPUB" + SYNTHETIC_TPUB[4:]
    assert redacted(shouted) == shouted


# --------------------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("address", ADDRESSES)
def test_every_testnet_address_form_is_redacted_in_running_text(address: str) -> None:
    assert redacted(f"read {address}, done") == f"read {REDACTED}, done"
    assert redacted(address) == REDACTED
    assert redacted(f"/addresses/{address}/utxo") == f"/addresses/{REDACTED}/utxo"


def test_the_bech32_rule_lists_the_mainnet_part_and_ignores_case() -> None:
    bech32 = ADDRESS_PATTERNS[0]
    parts = re.search(r"\(\?:([a-z|]+)\)1", bech32.pattern)

    assert parts is not None
    assert parts.group(1).split("|") == ["bc", "tb", "bcrt"]
    assert bech32.flags & re.IGNORECASE
    assert bech32.pattern.startswith(ADDRESS_STARTS)


def test_the_base58_rule_lists_the_mainnet_first_characters() -> None:
    base58 = ADDRESS_PATTERNS[1]
    first = re.search(re.escape(ADDRESS_STARTS) + r"\[([0-9a-z]+)\]", base58.pattern)

    assert first is not None
    assert set(first.group(1)) == {"1", "3", "m", "n", "2"}
    assert base58.pattern.endswith("{25,34}" + ADDRESS_ENDS)
    assert not base58.flags & re.IGNORECASE


def test_the_kaspa_rule_lists_the_mainnet_prefix_and_ignores_case() -> None:
    kaspa = ADDRESS_PATTERNS[2]
    prefixes = re.search(r"\(\?:([a-z|]+)\):", kaspa.pattern)

    assert prefixes is not None
    assert prefixes.group(1).split("|") == ["kaspa", "kaspatest", "kaspasim", "kaspadev"]
    assert kaspa.flags & re.IGNORECASE
    assert kaspa.pattern.startswith(ADDRESS_STARTS)


def test_every_address_rule_is_anchored_on_letters_and_digits_not_on_a_word_boundary() -> None:
    r"""R14: `\b` counts `_` as part of a word, so `wallet_<address>` was never redacted.

    The anchors are pinned as written: a letter or a digit is what glues, and nothing else.
    """
    assert ADDRESS_STARTS == r"(?<![0-9A-Za-z])"
    assert ADDRESS_ENDS == r"(?![0-9A-Za-z])"
    for pattern in ADDRESS_PATTERNS:
        assert pattern.pattern.startswith(ADDRESS_STARTS)
        assert r"\b" not in pattern.pattern


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (f"wallet_{BIP173_TESTNET_P2WPKH}", f"wallet_{REDACTED}"),
        (f"snapshot_{CORE_TESTNET4_P2PKH}.json", f"snapshot_{REDACTED}.json"),
        (f"cache_{KASPA_TESTNET_V0}", f"cache_{REDACTED}"),
        (f"{CORE_TESTNET4_P2PKH}_balance", f"{REDACTED}_balance"),
        (f"{CORE_REGTEST_P2SH}_balance", f"{REDACTED}_balance"),
        (f"_{KASPA_TESTNET_V1_KEY}_", f"_{REDACTED}_"),
        (f"__{BIP350_TESTNET_V1}__", f"__{REDACTED}__"),
    ],
    ids=[
        "wallet_bech32",
        "snapshot_base58.json",
        "cache_kaspa",
        "p2pkh_balance",
        "p2sh_balance",
        "_kaspa_",
        "__taproot__",
    ],
)
def test_an_underscore_separates_an_address_from_its_neighbours(given: str, expected: str) -> None:
    """R14: the shapes a hurried f-string produces, which `\\b` left whole."""
    assert redacted(given) == expected


@pytest.mark.parametrize("address", ADDRESSES)
def test_every_punctuation_character_separates_every_address_form(address: str) -> None:
    """Whatever is not a letter or a digit is a separator, on either side (R14)."""
    for mark in string.punctuation:
        assert redacted(f"a{mark}{address}{mark}b") == f"a{mark}{REDACTED}{mark}b", mark


def test_there_are_exactly_three_address_rules() -> None:
    assert len(ADDRESS_PATTERNS) == 3


def test_a_bech32_address_needs_eleven_data_characters() -> None:
    eleven = BIP173_TESTNET_P2WPKH[:14]
    assert eleven == "tb1" + BIP173_TESTNET_P2WPKH[3:14]

    assert redacted(eleven) == REDACTED
    assert redacted(eleven[:-1]) == eleven[:-1]


def test_a_letter_or_digit_before_an_address_is_not_a_separator() -> None:
    """The anchor's edge, and the residual R14 accepts and documents: an address glued to a
    letter or a digit is not recognised. Pinned so that a rule which drops the anchor -- and
    then redacts inside every long run of letters and digits -- is seen."""
    for glue in ("x", "7"):
        assert redacted(glue + BIP173_TESTNET_P2WPKH) == glue + BIP173_TESTNET_P2WPKH
        assert redacted(glue + CORE_TESTNET4_P2SH) == glue + CORE_TESTNET4_P2SH
        assert redacted(glue + KASPA_TESTNET_V0) == glue + KASPA_TESTNET_V0


@settings(max_examples=300)
@given(digest=st.text(alphabet="0123456789abcdef", min_size=64, max_size=64))
def test_a_transaction_id_or_a_hash_is_not_an_address(digest: str) -> None:
    """Precision: what the anchors are for. Without them the Base58 rule finds an address
    inside almost any 64-character hex string -- a `1`, `2` or `3` followed by 25 characters
    with no `0`. One that starts `bc1` is the one hex string the bech32 rule takes whole, so it
    is left out."""
    assume(not digest.startswith("bc1"))

    assert redacted(f"tx {digest} confirmed") == f"tx {digest} confirmed"
    assert redacted(f"/tx/{digest}/status") == f"/tx/{digest}/status"


def test_a_regtest_bech32_address_is_redacted_from_its_long_prefix() -> None:
    """`bcrt1...`: neither `bc` nor `tb` matches here, since `r` is not the separator."""
    assert redacted(CORE_REGTEST_P2WPKH) == REDACTED
    assert redacted(CORE_REGTEST_P2WPKH[:16]) == REDACTED
    assert redacted(CORE_REGTEST_P2WPKH[:15]) == CORE_REGTEST_P2WPKH[:15]


def test_a_base58_address_is_twenty_six_to_thirty_five_characters_between_separators() -> None:
    """The first character, then 25 to 34. Shorter, or longer -- a letter or a digit after
    the 35th -- is not one, and is left alone (R14's `ADDRESS_ENDS`)."""
    p2sh = CORE_TESTNET4_P2SH
    assert len(p2sh) == 35

    assert redacted(p2sh) == REDACTED
    assert redacted(p2sh[:26]) == REDACTED
    assert redacted(p2sh[:25]) == p2sh[:25]
    assert redacted(p2sh + "A") == p2sh + "A"
    assert redacted(p2sh + "7") == p2sh + "7"
    assert redacted(f"({p2sh})") == f"({REDACTED})"
    assert redacted(f"{p2sh}_") == f"{REDACTED}_"


def test_a_base58_address_never_contains_a_character_outside_the_alphabet() -> None:
    p2pkh = CORE_TESTNET4_P2PKH
    with_zero = p2pkh[:10] + "0" + p2pkh[11:]

    assert redacted(with_zero) == with_zero


def test_a_kaspa_address_needs_sixty_one_characters_after_its_prefix() -> None:
    prefix, data = KASPA_TESTNET_V0.split(":")
    assert len(data) == 61
    assert len(KASPA_TESTNET_V1_KEY.split(":")[1]) == 63

    assert redacted(f"{prefix}:{data[:60]}") == f"{prefix}:{data[:60]}"
    assert redacted(f"x{prefix}:{data}") == f"x{prefix}:{data}"
    assert redacted(KASPA_TESTNET_V0.upper()) == REDACTED


# --------------------------------------------------------------------------------------
# The repetition (R12, R15, R17): until a pass changes nothing, at most four passes
# --------------------------------------------------------------------------------------

#: A synthetic secret that ends in the marker's last two characters, and a string that holds
#: it only once an address before it has become the marker (R15).
MARKER_TAIL: Final = "D]-tail0"
BEFORE_THE_MARKER: Final = f"{BIP173_TESTNET_P2WPKH}-tail0"


def test_one_pass_can_leave_a_secret_that_the_next_pass_redacts() -> None:
    """R15: the address becomes `[REDACTED]`, and `[REDACTED]-tail0` holds the secret.

    One pass -- the secrets first, then the address -- leaves it; the repetition does not. A
    mutant that makes one pass is seen here, with a secret, which is what the loop is for.
    """
    redactor = ValueRedactor([MARKER_TAIL])
    assert REDACTED.endswith("D]")
    assert MARKER_TAIL not in BEFORE_THE_MARKER

    once = redactor._redact_once(BEFORE_THE_MARKER)

    assert once == f"{REDACTED}-tail0"
    assert MARKER_TAIL in once
    assert redactor.redact_text(BEFORE_THE_MARKER) == REDACTED.removesuffix("D]") + REDACTED
    assert MARKER_TAIL not in redactor.redact_text(BEFORE_THE_MARKER)


def test_the_repetition_stops_at_its_bound_when_the_marker_holds_a_secret() -> None:
    """I1 (R17): a secret that is part of the marker makes every pass change the string.

    `REDACTED` holds `REDACTED`, so each pass wraps the last one's marker in another, and only
    `MAX_REDACTION_PASSES` ends it: four passes, four pairs of brackets, then it returns.
    """
    assert MAX_REDACTION_PASSES == 4
    redactor = ValueRedactor(["REDACTED"])
    bounded = "[" * MAX_REDACTION_PASSES + "REDACTED" + "]" * MAX_REDACTION_PASSES

    assert redactor.redact_text("REDACTED") == bounded
    assert redactor.redact_text(BIP173_TESTNET_P2WPKH) == bounded
    assert redactor.redact_text("nothing here") == "nothing here"


def test_a_string_with_nothing_to_redact_costs_one_pass() -> None:
    """The repetition stops at the first pass that changes nothing."""
    calls: list[str] = []

    class Counting(ValueRedactor):
        def _redact_once(self, text: str) -> str:
            calls.append(text)
            return super()._redact_once(text)

    assert Counting().redact_text("plain words") == "plain words"
    assert calls == ["plain words"]

    calls.clear()
    assert Counting().redact_text(BIP173_TESTNET_P2WPKH) == REDACTED
    assert calls == [BIP173_TESTNET_P2WPKH, REDACTED]


#: Every token the rules redact without a secret: the addresses, the key, and URLs.
RULE_TOKENS: Final = (
    *ADDRESSES,
    SYNTHETIC_TPUB,
    "https://h.example/p?signature=abc",
    "https://h.example/p",
    "wss://h.example/s?token=t#f?x",
)

#: Every separator worth a case: whitespace, every ASCII punctuation character -- `_`
#: included -- and two that are not ASCII. `""`, two tokens glued, is `GLUED`.
SEPARATED: Final = (" ", "\n", "\t", *string.punctuation, "\u00e9", "\u00a0")
GLUED: Final = ""


def every_pair(separator: str) -> list[str]:
    return [left + separator + right for left in RULE_TOKENS for right in RULE_TOKENS]


@pytest.mark.parametrize("separator", SEPARATED)
def test_with_a_separator_one_pass_is_the_fixpoint(separator: str) -> None:
    """R15 and R17, by brute force: separated tokens need no second pass. Every pair."""
    redactor = ValueRedactor()
    differ = [
        text
        for text in every_pair(separator)
        if redactor._redact_once(text) != redactor.redact_text(text)
    ]

    assert differ == []


def test_glued_tokens_reach_a_fixpoint_within_the_bound() -> None:
    """R17: with `""` the second pass is what redacts the second of two glued addresses, so
    one pass is not the fixpoint. The repetition reaches one -- a pass changes nothing more --
    within `MAX_REDACTION_PASSES`, and a second call changes nothing."""
    redactor = ValueRedactor()
    needed_a_second_pass = 0
    for text in every_pair(GLUED):
        fixpoint = redactor.redact_text(text)

        assert redactor._redact_once(fixpoint) == fixpoint, text
        assert redactor.redact_text(fixpoint) == fixpoint, text
        needed_a_second_pass += redactor._redact_once(text) != fixpoint

    # Measured, so the case is not vacuous: the loop does work here, and only here.
    assert needed_a_second_pass > 0


@settings(max_examples=300)
@given(
    tokens=st.lists(st.sampled_from(RULE_TOKENS), min_size=1, max_size=5),
    separator=st.sampled_from(SEPARATED),
)
def test_with_a_separator_one_pass_is_the_fixpoint_for_any_run_of_tokens(
    tokens: list[str], separator: str
) -> None:
    redactor = ValueRedactor()
    text = separator.join(tokens)

    assert redactor._redact_once(text) == redactor.redact_text(text)


@settings(max_examples=300)
@given(tokens=st.lists(st.sampled_from(RULE_TOKENS), min_size=1, max_size=4))
def test_glued_tokens_reach_a_fixpoint_for_any_run_of_tokens(tokens: list[str]) -> None:
    redactor = ValueRedactor()
    fixpoint = redactor.redact_text(GLUED.join(tokens))

    assert redactor._redact_once(fixpoint) == fixpoint
    assert redactor.redact_text(fixpoint) == fixpoint


# --------------------------------------------------------------------------------------
# Runs of glued addresses (R16 N1, R18): one match, so one pass, however long the run
# --------------------------------------------------------------------------------------

#: The Kaspa forms: two 61-character payloads, three 63-character ones, one in mixed case.
KASPA_FORMS: Final = (
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V0_ASPECTRON,
    KASPA_TESTNET_V1_ZERO,
    KASPA_TESTNET_V1_KEY,
    KASPA_MIXED_CASE,
)

#: The bech32 forms, each of which a regtest address can follow in a run.
BECH32_FORMS: Final = (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WSH,
    BIP173_TESTNET_P2WPKH_UPPERCASE,
    BIP350_TESTNET_V1,
    BIP350_MIXED_CASE,
    CORE_TESTNET4_V1,
    CORE_REGTEST_P2WPKH,
    CORE_REGTEST_V1,
)


def glued(forms: tuple[str, ...], length: int) -> str:
    """`length` addresses glued together, cycling through `forms`."""
    return "".join(forms[index % len(forms)] for index in range(length))


@pytest.mark.parametrize("length", [2, 6, 20])
@pytest.mark.parametrize(
    "forms",
    [(KASPA_TESTNET_V0,), (KASPA_TESTNET_V1_KEY,), KASPA_FORMS],
    ids=["61", "63", "mixed"],
)
def test_a_run_of_glued_kaspa_addresses_is_one_marker_in_one_pass(
    forms: tuple[str, ...], length: int
) -> None:
    """R16 (N1): a chain lost one address per pass, and the fifth outlasted the bound.

    A 61-character payload is where the alphabet alone cannot cut: `ka` of the next prefix
    is in it, so each address must stop where another prefix follows.
    """
    run = glued(forms, length)
    redactor = ValueRedactor()

    assert redactor._redact_once(run) == REDACTED
    assert redactor._redact_once(f"sent {run}, done") == f"sent {REDACTED}, done"


def test_six_glued_regtest_addresses_are_one_marker_in_one_pass() -> None:
    """R18: `b` is outside the bech32 alphabet, so a glued `bcrt1...` lost one per pass."""
    redactor = ValueRedactor()

    assert redactor._redact_once(CORE_REGTEST_P2WPKH * 6) == REDACTED
    assert redactor._redact_once(glued((CORE_REGTEST_P2WPKH, CORE_REGTEST_V1), 6)) == REDACTED


@pytest.mark.parametrize("first", BECH32_FORMS)
def test_any_bech32_address_then_regtest_ones_is_one_marker_in_one_pass(first: str) -> None:
    run = first + glued((CORE_REGTEST_P2WPKH, CORE_REGTEST_V1), 19)

    assert ValueRedactor()._redact_once(f"[{run}]") == f"[{REDACTED}]"


@pytest.mark.parametrize("kaspa", KASPA_FORMS)
@pytest.mark.parametrize(
    "follower",
    [
        BIP173_TESTNET_P2WPKH,
        BIP350_TESTNET_V1,
        CORE_REGTEST_P2WPKH,
        CORE_TESTNET4_P2PKH,
        CORE_SIGNET_P2PKH,
        CORE_TESTNET4_P2SH,
        CORE_REGTEST_P2SH,
    ],
)
def test_a_kaspa_payload_stops_where_a_bech32_or_base58_address_starts(
    kaspa: str, follower: str
) -> None:
    """R16 and R19, the third length a payload tries: the longest that the start of another
    address follows, left for its own rule on the next pass (R17). Taking the longest of all
    swallowed the follower's first characters, and the rest was printed; the shortest cut a
    63-character payload ending `2d` at 61, and `2d` glued in front of the follower printed it.

    Two markers in the end, for every vector and follower (R19). One pass stops exactly at
    the follower for a 63-character payload; a 61-character one tries 63 and 62 first, and a
    follower whose third character starts a Base58 address (`mfn...`) is cut there on pass 1
    and redacted on pass 2.
    """
    redactor = ValueRedactor()

    assert redactor.redact_text(kaspa + follower) == REDACTED * 2
    if len(kaspa.split(":")[1]) == 63:
        assert redactor._redact_once(kaspa + follower) == REDACTED + follower


# --------------------------------------------------------------------------------------
# URL queries
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (
            "GET https://api.example.test/v2/orders?apiKey=k&signature=abc HTTP/1.1",
            f"GET https://api.example.test/v2/orders?{REDACTED} HTTP/1.1",
        ),
        ("https://h.example/p?q=1#frag", f"https://h.example/p?{REDACTED}#frag"),
        ("'https://h.example/p?q=1' then", f"'https://h.example/p?{REDACTED}' then"),
        ('"https://h.example/p?q=1"', f'"https://h.example/p?{REDACTED}"'),
        ("http://h.example?q=1", f"http://h.example?{REDACTED}"),
        ("wss://h.example/s?token=t", f"wss://h.example/s?{REDACTED}"),
        ("https://h.example/p?", f"https://h.example/p?{REDACTED}"),
        (
            "a https://one.example/x?a=1 and https://two.example/y?b=2",
            f"a https://one.example/x?{REDACTED} and https://two.example/y?{REDACTED}",
        ),
        ("https://h.example/p no query", "https://h.example/p no query"),
        ("https://h.example/p#a?b", "https://h.example/p#a?b"),
        # S2 (R13): a URL glued behind a word is a token like any other.
        ("fetch_https://host.example/x?sign=abc", f"fetch_https://host.example/x?{REDACTED}"),
        ("GET:https://h.example/p?sign=abc", f"GET:https://h.example/p?{REDACTED}"),
        ("url=https://h.example/p?a=1#frag tail", f"url=https://h.example/p?{REDACTED}#frag tail"),
        # The query is the first `?` after `://` and before the first `#`.
        ("https://h.example/p?q=1#a?b", f"https://h.example/p?{REDACTED}#a?b"),
        ("https://h.example/p?q=1?r=2", f"https://h.example/p?{REDACTED}"),
        ("a?b=1://h.example", "a?b=1://h.example"),
        # A fragment that holds `://` is read as a URL in turn.
        (
            "https://a.example/p#https://b.example/q?sign=1",
            f"https://a.example/p#https://b.example/q?{REDACTED}",
        ),
        (
            "https://a.example/p?x=1#https://b.example/q?sign=1",
            f"https://a.example/p?{REDACTED}#https://b.example/q?{REDACTED}",
        ),
        # A second URL glued into the first's query is redacted with it: over-reading.
        ("https://a.example/p?x=1https://b.example/q", f"https://a.example/p?{REDACTED}"),
        # A token ends at whitespace or a quote, and each token is read on its own.
        (
            """a "https://h.example/p?k=1" 'wss://w.example/?t=2'""",
            f"""a "https://h.example/p?{REDACTED}" 'wss://w.example/?{REDACTED}'""",
        ),
        (
            "https://h.example/p?k=1\thttps://i.example/?t=2",
            f"https://h.example/p?{REDACTED}\thttps://i.example/?{REDACTED}",
        ),
    ],
)
def test_a_urls_query_is_replaced_and_its_base_kept(given: str, expected: str) -> None:
    assert redacted(given) == expected
    assert redact_url_queries(given) == expected


def test_a_path_without_a_scheme_keeps_its_query() -> None:
    """The documented residual: only `scheme://` URLs are rewritten."""
    assert redacted("/api/wallets?limit=5") == "/api/wallets?limit=5"


def test_the_query_rule_rewrites_its_own_output_to_itself() -> None:
    once = redacted("https://h.example/p?signature=abc")
    assert redacted(once) == once == f"https://h.example/p?{REDACTED}"


# --------------------------------------------------------------------------------------
# M1 (R13): every rule in bounded time on 200 KB of what an outsider can send
# --------------------------------------------------------------------------------------

ADVERSARIAL_SIZE: Final = 200_000
TIME_BOUND_SECONDS: Final = 1.0
#: A URL with a query, appended so the URL rule's `://` shortcut cannot be what saves a case.
URL_TAIL: Final = " https://h.example.test/p?sign=abc"
#: A synthetic secret, long enough that its prefix repeated is a near miss at every position.
LONG_SENTINEL: Final = "S3NTINEL-LONG-SECRET-VALUE"


def adversarial(unit: str) -> str:
    return unit * (ADVERSARIAL_SIZE // len(unit) + 1)


ADVERSARIAL: Final = {
    # The old URL rule rescanned from every word boundary: 12 s for `a.`, 27 s for `a://`.
    "a.": adversarial("a."),
    "a. then a URL": adversarial("a.") + URL_TAIL,
    "a://": adversarial("a://"),
    "a://#": adversarial("a://#"),
    "a://?#": adversarial("a://?#"),
    "one token then a URL": adversarial("a") + URL_TAIL,
    "many tokens then a URL": adversarial("a ") + URL_TAIL,
    # The address rules: a prefix at every position, and near misses that fail late.
    "tb1": adversarial("tb1"),
    "bcrt1 then a URL": adversarial("bcrt1") + URL_TAIL,
    "kaspatest:": adversarial("kaspatest:"),
    "kaspatest: and 60 data characters": adversarial("kaspatest:" + "q" * 60 + "."),
    "kaspatest: and 64 data characters": adversarial("kaspatest:" + "q" * 64),
    "kaspatest: glued runs": adversarial("kaspatest:" + "q" * 61),
    "base58 runs one too long": adversarial("m" + "a" * 35 + "."),
    "a run of 1": adversarial("1"),
    # The two run rules (R16, R18) nest a quantifier, so their failing shapes: a glued run
    # whose end anchor fails, payloads one short and one long, and runs broken by one
    # character outside the alphabet, with and without a separator to start again after.
    "a glued Kaspa run then a letter": adversarial(KASPA_TESTNET_V0) + "x",
    "a glued Kaspa run then a digit": adversarial(KASPA_TESTNET_V1_KEY) + "7",
    "glued kaspatest: with 60 characters": adversarial("kaspatest:" + "q" * 60),
    "glued kaspatest: with 64 characters": adversarial("kaspatest:" + "q" * 64 + "kaspatest:"),
    "separated kaspatest: with 60 characters": adversarial(".kaspatest:" + "q" * 60),
    "Kaspa runs broken by b": adversarial(KASPA_TESTNET_V0 * 3 + "b"),
    "Kaspa runs broken by b after a separator": adversarial("." + KASPA_TESTNET_V0 * 3 + "b"),
    "a glued bcrt1 run then a letter": adversarial(CORE_REGTEST_P2WPKH) + "x",
    "tb1, eleven q, b": adversarial("tb1" + "q" * 11 + "b"),
    "tb1, a thousand q, b": adversarial("tb1" + "q" * 1000 + "b"),
    "tb1, ten q, b, after a separator": adversarial(".tb1" + "q" * 10 + "b"),
    "bcrt1 runs broken by o": adversarial(CORE_REGTEST_P2WPKH * 3 + "o"),
    "bcrt1 runs broken by o after a separator": adversarial("." + CORE_REGTEST_P2WPKH * 3 + "o"),
    "tb1 then bcrt1, ten q each": adversarial("tb1" + "q" * 10 + "bcrt1" + "q" * 10),
    # The extended-key rule. `tpub`, not `xpub`: `xpub` repeated is itself a mainnet-shaped
    # key -- a prefix and a hundred Base58 characters -- which rule 3 forbids assembling. The
    # prefixes are alternatives tried the same way at every position, so the cost is the same.
    "tpub": adversarial("tpub"),
    "tpub and 99 characters": adversarial("tpub" + "a" * 99 + "0"),
    # The secret rule: the secret but its last character, over and over.
    "a long secret's prefix": adversarial(LONG_SENTINEL[:-1]),
}


@pytest.mark.parametrize("text", list(ADVERSARIAL.values()), ids=list(ADVERSARIAL))
def test_every_rule_takes_bounded_time_on_200_kb_of_adversarial_text(text: str) -> None:
    """R13 (M1): a client with no session chooses the path `request_refused` logs, and the
    redaction runs on the event loop. One second is generous: each measured a few ms."""
    redactor = ValueRedactor([LONG_SENTINEL])
    assert len(text) >= ADVERSARIAL_SIZE

    started = time.perf_counter()
    out = redactor.redact_text(text)
    elapsed = time.perf_counter() - started

    assert elapsed < TIME_BOUND_SECONDS
    if text.endswith(URL_TAIL):
        assert out.endswith(f"?{REDACTED}")


def test_a_marker_in_the_replacement_is_literal() -> None:
    r"""A base containing `\1` or `\g<0>` would be read as a template by `re.sub` with a str."""
    given = r"https://h.example/\g<0>/\1?q=1"
    assert redacted(given) == r"https://h.example/\g<0>/\1?" + REDACTED


# --------------------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------------------


def run(event: dict[str, Any], *secrets: str) -> dict[str, Any]:
    return dict(ValueRedactor(secrets)(None, "info", event))


def test_every_value_is_walked_event_and_exception_included() -> None:
    out = run(
        {
            "event": f"read {BIP173_TESTNET_P2WPKH}",
            "exception": f"Traceback ...\nValueError: {KASPA_TESTNET_V0} {EIGHT}",
            "note": f"https://h.example/p?sig={EIGHT}",
        },
        EIGHT,
    )

    assert out == {
        "event": f"read {REDACTED}",
        "exception": f"Traceback ...\nValueError: {REDACTED} {REDACTED}",
        "note": f"https://h.example/p?{REDACTED}",
    }


def test_containers_are_walked_at_any_depth() -> None:
    out = run(
        {
            "nested": {
                "list": [BIP173_TESTNET_P2WPKH, {"deeper": (SYNTHETIC_TPUB,)}],
                "set": {CORE_TESTNET4_P2PKH},
                "frozen": frozenset({EIGHT}),
            }
        },
        EIGHT,
    )

    assert out == {
        "nested": {
            "list": [REDACTED, {"deeper": [REDACTED]}],
            "set": [REDACTED],
            "frozen": [REDACTED],
        }
    }


@pytest.mark.parametrize("value", [None, True, False, 0, 7, -3, 1.5])
def test_numbers_booleans_and_none_are_kept_as_they_are(value: object) -> None:
    out = run({"value": value})

    assert out["value"] is value or out["value"] == value
    assert type(out["value"]) is type(value)


class Carrier:
    """An object whose `repr` carries an address, as a model or a row would."""

    def __repr__(self) -> str:
        return f"Carrier(address={BIP173_TESTNET_P2WPKH!r}, key={EIGHT!r})"


class Broken:
    def __repr__(self) -> str:
        message = f"repr failed with {BIP173_TESTNET_P2WPKH}"
        raise RuntimeError(message)


def test_an_object_is_redacted_through_its_repr() -> None:
    out = run({"row": Carrier(), "rows": [Carrier()]}, EIGHT)

    assert out["row"] == f"Carrier(address='{REDACTED}', key='{REDACTED}')"
    assert out["rows"] == [out["row"]]


def test_an_object_whose_repr_raises_becomes_a_placeholder() -> None:
    out = run({"row": Broken(), "event": "kept"})

    assert out == {"row": "<Broken whose repr failed>", "event": "kept"}


def test_the_event_given_is_not_mutated() -> None:
    inner = [BIP173_TESTNET_P2WPKH]
    event: dict[str, Any] = {"event": BIP173_TESTNET_P2WPKH, "inner": inner}

    run(event)

    assert event == {"event": BIP173_TESTNET_P2WPKH, "inner": [BIP173_TESTNET_P2WPKH]}
    assert event["inner"] is inner


def test_a_non_string_key_is_kept() -> None:
    assert run({"batch": {1: "one", None: BIP173_TESTNET_P2WPKH}}) == {
        "batch": {1: "one", None: REDACTED}
    }


def test_a_key_of_any_other_type_is_redacted_through_its_repr() -> None:
    """R12: a tuple key carrying an address is printed as its `repr`, redacted; a key whose
    `repr` raises becomes the placeholder a value would. Both stay hashable."""
    out = run({"balances": {(BIP173_TESTNET_P2WPKH, "BTC"): "0.5", Broken(): "1"}})

    assert out == {"balances": {f"('{REDACTED}', 'BTC')": "0.5", "<Broken whose repr failed>": "1"}}


@pytest.mark.parametrize(
    "key",
    [BIP173_TESTNET_P2WPKH, KASPA_TESTNET_V0, SYNTHETIC_TPUB, EIGHT, "https://h.example/p?s=1"],
    ids=["bech32", "kaspa", "tpub", "secret", "url"],
)
def test_a_string_key_is_redacted_like_any_other_string(key: str) -> None:
    """Criterion 2: wherever it occurs. A mapping keyed by address is printed key and all."""
    out = run({"balances": {key: "0.5"}}, EIGHT)

    assert key not in json.dumps(out)
    assert list(out["balances"].values()) == ["0.5"]


@pytest.mark.parametrize(
    "key",
    [BIP173_TESTNET_P2WPKH, KASPA_TESTNET_V0, SYNTHETIC_TPUB, EIGHT, "https://h.example/p?s=1"],
    ids=["bech32", "kaspa", "tpub", "secret", "url"],
)
def test_the_records_own_key_is_redacted_like_any_other_string(key: str) -> None:
    """R12: `log.info("balance", **{address: "0.5"})` names the address in the record's own
    key, not in a nested mapping's, and the processor redacts it there too."""
    out = run({"event": "balance", key: "0.5"}, EIGHT)

    assert key not in json.dumps(out)
    assert out == {"event": "balance", redacted(key, EIGHT): "0.5"}


# --------------------------------------------------------------------------------------
# The request id (R5): redacted like every other value, and never matched
# --------------------------------------------------------------------------------------


def test_the_request_id_is_not_exempt() -> None:
    """A value bound under `request_id` that is an address is redacted like any other."""
    assert run({REQUEST_ID_KEY: BIP173_TESTNET_P2WPKH}) == {REQUEST_ID_KEY: REDACTED}


@settings(max_examples=500)
@given(identifier=st.uuids())
def test_no_hyphenated_uuid_matches_any_rule(identifier: object) -> None:
    """Its longest run without a hyphen is 12 characters; the shortest match is 14."""
    text = str(identifier)

    assert redacted(text) == text
    assert max(len(run_) for run_ in text.split("-")) == 12


# --------------------------------------------------------------------------------------
# Idempotence, on text whose tokens are separated as real log text is
# --------------------------------------------------------------------------------------

TOKENS: Final = st.sampled_from(
    [
        *ADDRESSES,
        SYNTHETIC_TPUB,
        EIGHT,
        "https://h.example/p?signature=abc",
        "https://h.example/p",
        REDACTED,
        "word",
        "0.5",
        "",
    ]
)
SEPARATORS: Final = st.sampled_from([" ", ", ", "\n", "/", "=", '"', "'", "(", ")"])


@settings(max_examples=300)
@given(tokens=st.lists(TOKENS, max_size=6), separator=SEPARATORS)
def test_a_second_pass_changes_nothing_and_no_address_survives(
    tokens: list[str], separator: str
) -> None:
    text = separator.join(tokens)
    redactor = ValueRedactor([EIGHT])

    once = redactor.redact_text(text)

    assert redactor.redact_text(once) == once
    for address in (*ADDRESSES, SYNTHETIC_TPUB, EIGHT, "signature=abc"):
        assert address not in once
