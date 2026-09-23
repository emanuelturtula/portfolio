"""Criterion 4: a logged URL carries no query string -- and, for a chain, no path either.

Two functions because there are two questions. `strip_query` is criterion 4 verbatim and
is what a future exchange provider will use on a URL it signed. `request_target` is what
this module's transport actually logs, and it is stricter: both target chains put the
owner's address in the *path*, so stripping the query would meet the letter of the rule
and leak the address anyway.

The sentinel here is a fake signature and a fake key. Neither is a real credential, and
rule 3 means neither ever could be -- but a test that asserted on `"secret"` would pass
against output that happened not to contain that word, so the strings are distinctive
enough that their presence anywhere in a result is unambiguous.
"""

from __future__ import annotations

from typing import Final

import httpx
import pytest

from portfolio.providers.http import (
    ADDRESS_BALANCE,
    ADDRESS_BALANCES,
    BLOCK_TIP_HEIGHT,
    ENDPOINT_EXTENSION,
    ENDPOINT_LABELS,
    NODE_HEALTH,
    UNLABELLED,
    request_target,
    strip_query,
)
from portfolio.providers.http import ENDPOINT_LABEL as ENDPOINT_LABEL_PATTERN
from tests.address_vectors import BIP173_TESTNET_P2WPKH, KASPA_TESTNET_V1_KEY
from tests.providers.harness import ENDPOINT_LABEL, TEST_HOST, TEST_ORIGIN

#: Synthetic. Not a credential, never was one, and shaped so that a partial match is still
#: a match: every substring of eight characters or more is unique to it.
SENTINEL_SIGNATURE: Final = "SIGNATURE-c0ffee-DO-NOT-LOG-a1b2c3d4"
SENTINEL_KEY: Final = "APIKEY-deadbeef-DO-NOT-LOG"


# --------------------------------------------------------------------------------------
# Criterion 4, verbatim
# --------------------------------------------------------------------------------------


def test_the_query_string_is_removed() -> None:
    """The criterion as the issue words it, and the reason it is worded that way.

    One exchange signs its requests in the query string, so a URL that reached a log would
    carry a valid signature -- and, next to it, the key id that produced it. A signature
    is short-lived; the log line it is in is not.
    """
    url = httpx.URL(
        f"{TEST_ORIGIN}/v1/account?apiKey={SENTINEL_KEY}&signature={SENTINEL_SIGNATURE}"
    )

    stripped = strip_query(url)

    assert str(stripped) == f"{TEST_ORIGIN}/v1/account"
    assert SENTINEL_KEY not in str(stripped)
    assert SENTINEL_SIGNATURE not in str(stripped)
    assert stripped.query == b""


def test_the_fragment_and_any_credentials_are_removed_too() -> None:
    """A query string is not the only place a URL hides something.

    `https://key:secret@host/...` is a credential written into a URL and `httpx` will
    carry one happily. The fragment is never sent to the server at all, so it has even
    less business in a log than the query does.
    """
    url = httpx.URL(f"https://user:{SENTINEL_KEY}@{TEST_HOST}/v1/account?x=1#{SENTINEL_SIGNATURE}")

    stripped = strip_query(url)

    assert str(stripped) == f"{TEST_ORIGIN}/v1/account"
    assert stripped.userinfo == b""
    assert stripped.fragment == ""
    assert SENTINEL_KEY not in str(stripped)
    assert SENTINEL_SIGNATURE not in str(stripped)


def test_strip_query_accepts_a_string_as_well_as_a_url() -> None:
    """Callers hold both, and a `str` that silently skipped the scrub would be the leak."""
    stripped = strip_query(f"{TEST_ORIGIN}/v1/account?signature={SENTINEL_SIGNATURE}")

    assert str(stripped) == f"{TEST_ORIGIN}/v1/account"


def test_a_url_with_nothing_to_strip_is_returned_unchanged() -> None:
    """The scrub is not allowed to mangle the ordinary case.

    A function that returned the origin and dropped the path would pass every assertion
    above, because every one of them expects the secret to be gone.
    """
    assert str(strip_query(f"{TEST_ORIGIN}/v1/account")) == f"{TEST_ORIGIN}/v1/account"


def test_strip_query_keeps_the_path_which_is_why_it_is_not_what_the_transport_logs() -> None:
    """The gap criterion 4 does not close, asserted so nobody closes it by accident.

    `strip_query` is correct and insufficient: an Esplora URL is
    `/address/{address}/utxo`, so the address survives the scrub. This test exists to
    state that in executable form, next to `request_target`, so that a future provider
    author reaching for `strip_query` to build a log line meets the reason not to.
    """
    url = httpx.URL(f"{TEST_ORIGIN}/api/address/{BIP173_TESTNET_P2WPKH}/utxo?x=1")

    stripped = strip_query(url)

    assert BIP173_TESTNET_P2WPKH in str(stripped)


# --------------------------------------------------------------------------------------
# Criterion 4b: what the transport is allowed to call a request
# --------------------------------------------------------------------------------------


def test_the_logged_target_is_the_host_and_a_label_and_nothing_else() -> None:
    """Scheme, host, label. No path, no query, no port, no userinfo."""
    request = httpx.Request(
        "GET",
        f"{TEST_ORIGIN}/api/address/{BIP173_TESTNET_P2WPKH}/utxo?x=1",
        extensions={ENDPOINT_EXTENSION: ENDPOINT_LABEL},
    )

    assert request_target(request) == f"{TEST_ORIGIN}/{ENDPOINT_LABEL}"


@pytest.mark.parametrize(
    "address",
    [BIP173_TESTNET_P2WPKH, KASPA_TESTNET_V1_KEY],
    ids=["bitcoin in the path", "kaspa in the path"],
)
def test_an_address_in_the_path_never_reaches_the_logged_target(address: str) -> None:
    """Both target chains, because both put the address somewhere different in the path.

    Esplora's is `/address/{address}/utxo` and Kaspa's is `/addresses/{address}/balance`.
    A scrubber written against one shape would leave the other intact.
    """
    request = httpx.Request(
        "GET",
        f"{TEST_ORIGIN}/addresses/{address}/balance",
        extensions={ENDPOINT_EXTENSION: ENDPOINT_LABEL},
    )

    target = request_target(request)

    assert address not in target
    assert address.lower() not in target.lower()
    assert address[:20] not in target


@pytest.mark.parametrize(
    "extensions",
    [
        pytest.param({}, id="no extensions at all"),
        pytest.param({"timeout": {"connect": 1}}, id="extensions without a label"),
        pytest.param({ENDPOINT_EXTENSION: None}, id="an explicit None"),
        pytest.param({ENDPOINT_EXTENSION: 42}, id="a label that is not a string"),
        pytest.param({"ENDPOINT": "address_balance"}, id="the wrong key, wrong case"),
    ],
)
def test_an_unlabelled_request_logs_no_path_at_all(extensions: dict[str, object]) -> None:
    """Deny by default: a request that says nothing about itself is called nothing.

    The non-string rows matter as much as the missing one. A label read straight out of
    `extensions` and formatted into the target would render `42` -- harmless -- but would
    render any other object's `__str__`, and nothing constrains what a caller can put in
    an extensions dict. Falling back to `UNLABELLED` for anything that is not a `str` is
    what makes the safe outcome the default rather than the lucky one.
    """
    request = httpx.Request(
        "GET",
        f"{TEST_ORIGIN}/api/address/{BIP173_TESTNET_P2WPKH}/utxo",
        extensions=extensions,
    )

    target = request_target(request)

    assert target == f"{TEST_ORIGIN}/{UNLABELLED}"
    assert BIP173_TESTNET_P2WPKH not in target


def test_the_port_is_not_part_of_the_logged_target() -> None:
    """A port identifies a deployment rather than a call, and reads as part of the target."""
    request = httpx.Request(
        "GET",
        f"https://{TEST_HOST}:8443/anything",
        extensions={ENDPOINT_EXTENSION: ENDPOINT_LABEL},
    )

    assert request_target(request) == f"{TEST_ORIGIN}/{ENDPOINT_LABEL}"


def test_the_unlabelled_placeholder_is_a_constant_rather_than_a_repeated_literal() -> None:
    """Pinned once, so a test asserting an absence cannot drift from what is emitted.

    If the placeholder changed and the tests carried their own copy of the old string,
    every assertion about it would go on passing against a value nothing produces.
    """
    assert UNLABELLED == "<unlabelled>"
    assert ENDPOINT_EXTENSION == "endpoint"


# --------------------------------------------------------------------------------------
# The shapes that were never labels, and are still not
# --------------------------------------------------------------------------------------
#
# `request_target` could not keep its docstring's promise while the label was anything a
# caller put in `extensions`. A provider author wanting more detail writes
# `{"endpoint": request.url.path}` -- helpfulness, not malice -- and the address is in
# every retry and failure line, out of the one function that says it cannot be.
#
# #6 answered with `ENDPOINT_LABEL`, a shape check, and documented what it could not
# cover. #7 replaced the gate with membership in `ENDPOINT_LABELS`, which covers those
# too. The rows below are kept exactly as they were: they must still render `UNLABELLED`,
# and if the allowlist were ever loosened back into a pattern they would be the first
# things through.


@pytest.mark.parametrize(
    "label",
    [
        pytest.param("/api/address/tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx/utxo", id="a path"),
        pytest.param("address/tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", id="a path, no slash"),
        pytest.param("address_balance/tb1qw508d6qe", id="a label with a suffix appended"),
        pytest.param("/address_balance", id="a leading slash"),
        pytest.param("address_balance/", id="a trailing slash"),
        pytest.param("Address_Balance", id="upper case"),
        pytest.param("address balance", id="a space"),
        pytest.param("address.balance", id="a dot"),
        pytest.param("address:balance", id="a colon"),
        pytest.param("address%2Fbalance", id="a percent escape"),
        pytest.param("address_balance\n", id="a newline, which would forge a log line"),
        pytest.param("", id="empty"),
        pytest.param("_address_balance", id="leading underscore, so not [a-z] first"),
        pytest.param("1address", id="leading digit"),
        pytest.param("a" * 33, id="one over the length cap"),
    ],
)
def test_a_label_that_is_not_a_plain_identifier_renders_as_unlabelled(label: str) -> None:
    """Every shape that is not the pattern falls back to saying nothing.

    The newline row is worth its place on its own: a label containing one would let a
    caller inject a second line into a JSON log stream, which is how a log gets a record
    nobody wrote.
    """
    request = httpx.Request(
        "GET",
        f"{TEST_ORIGIN}/api/address/{BIP173_TESTNET_P2WPKH}/utxo",
        extensions={ENDPOINT_EXTENSION: label},
    )

    target = request_target(request)

    assert target == f"{TEST_ORIGIN}/{UNLABELLED}"
    assert BIP173_TESTNET_P2WPKH not in target
    assert BIP173_TESTNET_P2WPKH[:20] not in target


@pytest.mark.parametrize(
    "label",
    [
        pytest.param(ADDRESS_BALANCE, id="the balance read"),
        pytest.param(ADDRESS_BALANCES, id="the batch balance read"),
        pytest.param(BLOCK_TIP_HEIGHT, id="Bitcoin's health check"),
        pytest.param(NODE_HEALTH, id="Kaspa's health check"),
    ],
)
def test_a_well_formed_label_is_still_carried(label: str) -> None:
    """The control. A gate that rejected everything would pass every test above.

    Without this the allowlist could ship as `frozenset()`, which would satisfy every
    absence assertion in this file -- every request would render `<unlabelled>` -- and
    destroy the one thing the label is for. Driven over every member of the allowlist,
    so a label that is listed but unreachable fails here.
    """
    request = httpx.Request(
        "GET", f"{TEST_ORIGIN}/anything", extensions={ENDPOINT_EXTENSION: label}
    )

    assert request_target(request) == f"{TEST_ORIGIN}/{label}"


# --------------------------------------------------------------------------------------
# Criterion 8 of #7: membership in a named allowlist, not a pattern match
# --------------------------------------------------------------------------------------
#
# #6 closed the *accidental* case -- a path, a suffix, anything with a slash or an
# upper-case letter -- and said so in `request_target`'s own docstring: a truncated bech32
# address is lowercase alphanumeric and under 32 characters, so it matched the pattern and
# reached a log. The pattern could not be narrowed to exclude it without excluding real
# labels, because the two are the same shape. Only membership can tell them apart.


@pytest.mark.parametrize(
    "label",
    [
        pytest.param(BIP173_TESTNET_P2WPKH[:20], id="a truncated address, which #6 let through"),
        pytest.param(BIP173_TESTNET_P2WPKH[:12], id="twelve characters of an address"),
        pytest.param("a", id="one character, which the pattern allows"),
        pytest.param("balances", id="well shaped and simply not a label we use"),
        pytest.param("utxo_set_v2", id="digits and underscores, still not on the list"),
        pytest.param("a" * 32, id="exactly the length cap, still not on the list"),
        # `address_balances` used to be this row's "one letter added" case. #8 made it a
        # real label -- the Kaspa batch read -- so the near miss moved one letter further
        # out. A near-miss case that quietly becomes a real label is a test that stops
        # testing anything, which is why the id says what the string is rather than only
        # that it is wrong.
        pytest.param("address_balancess", id="a real label with one letter added"),
        pytest.param("address_balanc", id="a real label with one letter removed"),
        pytest.param("node_healthy", id="the health label with one letter added"),
    ],
)
def test_a_label_that_is_not_on_the_allowlist_renders_unlabelled(label: str) -> None:
    """Criterion 8: **whatever its shape**, a label that is not listed says nothing.

    The first two rows are the criterion's reason for existing. A truncated bech32 address
    passes `ENDPOINT_LABEL` -- lowercase, alphanumeric, under 32 characters -- and twenty
    characters of one is unique on chain and enough to search an explorer with. #6
    documented that as a residual it could not close; membership closes it.

    The last two rows are the realistic mistake rather than the adversarial one: a
    provider author who mistypes the constant gets a quiet log line instead of a wrong
    one, and the test that notices is this one.
    """
    assert label not in ENDPOINT_LABELS
    request = httpx.Request(
        "GET",
        f"{TEST_ORIGIN}/api/address/{BIP173_TESTNET_P2WPKH}/utxo",
        extensions={ENDPOINT_EXTENSION: label},
    )

    target = request_target(request)

    assert target == f"{TEST_ORIGIN}/{UNLABELLED}"
    assert BIP173_TESTNET_P2WPKH not in target
    assert BIP173_TESTNET_P2WPKH[:12] not in target
    # Only for labels long enough to be a disclosure. A one-character label is a substring
    # of the host itself, so asserting its absence would fail against a target that is
    # perfectly correct -- and a test that fails for a reason it does not mean is a test
    # somebody weakens rather than reads.
    if len(label) >= 8:
        assert label not in target


def test_the_allowlist_names_the_labels_this_release_uses() -> None:
    """Pinned against literals, the same shape as `PUBLIC_API_PATHS`.

    Adding an endpoint protects it; saying more about one is an edit to a named constant,
    which is a visible line in a diff and a deliberate act. Derived from nothing: an
    assertion of the form `ENDPOINT_LABELS == frozenset(ENDPOINT_LABELS)` would be true of
    any set at all, including one somebody widened to make a log line prettier.

    The emptiness assertion is the other half. An empty allowlist renders every real
    request `<unlabelled>`, which satisfies every absence test in this module and quietly
    removes the only thing distinguishing a balance read from a health check in a
    production log.

    Two labels at #7, four at #8: the Kaspa provider adds `address_balances` -- its batch
    read, which is the plural of the single-address one on purpose, because they are the
    same question asked two ways -- and `node_health`, which is its health probe. Adding
    them here is the deliberate act rule 8's shape requires; a provider that shipped with a
    label not on this list would be correct, quiet and impossible to find in a log.
    """
    assert sorted(ENDPOINT_LABELS) == [
        "address_balance",
        "address_balances",
        "block_tip_height",
        "node_health",
    ]
    assert ADDRESS_BALANCE == "address_balance"
    assert ADDRESS_BALANCES == "address_balances"
    assert BLOCK_TIP_HEIGHT == "block_tip_height"
    assert NODE_HEALTH == "node_health"
    assert ENDPOINT_LABELS, "an empty allowlist makes every request <unlabelled>"
    assert isinstance(ENDPOINT_LABELS, frozenset), (
        "a mutable set would let any module widen what may be logged at import time, "
        "which is the `register_endpoint_label()` design the spec rejected"
    )


@pytest.mark.parametrize("label", sorted(ENDPOINT_LABELS))
def test_every_allowlisted_label_is_also_well_shaped(label: str) -> None:
    """The pattern did not go away; it moved from the request to the constants.

    A listed label still has to be lower snake case and at most 32 characters, because a
    label carrying a slash, a dot, a colon, a percent-escape or a newline is a label
    somebody built out of a request rather than wrote down -- and a newline in one would
    let it forge a second line in a JSON log stream.

    Checked over the allowlist itself rather than over a request, which is what makes this
    a gate on *adding* a label instead of a gate on using one.
    """
    assert ENDPOINT_LABEL_PATTERN.fullmatch(label), f"{label!r} is not a well-shaped label"
    assert label.isascii()
    assert label.islower()
    assert "\n" not in label
    assert "\r" not in label
    assert len(label) <= 32


def test_the_length_cap_is_hygiene_and_the_allowlist_is_the_control() -> None:
    """A full address is rejected by the cap; a truncated one is not. Say so.

    42 characters of bech32 exceeds the 32-character cap, which reads like a defence and
    is not one: a truncated address is still an address, and 20 characters of it is enough
    to search an explorer with. The cap bounds a log line, the pattern removes the
    accidental shapes, and **membership is what actually decides** -- which is why the
    truncated form now renders `<unlabelled>` in
    `test_a_label_that_is_not_on_the_allowlist_renders_unlabelled` rather than being
    asserted here as a residual that could not be closed.
    """
    truncated = BIP173_TESTNET_P2WPKH[:20]

    assert len(BIP173_TESTNET_P2WPKH) > 32
    assert len(truncated) <= 32
    # It still satisfies the shape, which is the whole reason the shape is not the gate.
    assert ENDPOINT_LABEL_PATTERN.fullmatch(truncated)
    assert truncated not in ENDPOINT_LABELS


def test_a_unicode_homoglyph_label_renders_as_unlabelled() -> None:
    """A label that looks like a legal one but is not ASCII.

    Refused twice over now, and both refusals are worth keeping. Membership refuses it
    because `"\u0430ddress_balance"` is not `"address_balance"` -- string equality is not fooled
    by a homoglyph -- and the pattern refuses it because `[a-z]` in a Python regex does not
    match a Cyrillic small a.

    The second is asserted directly so that widening the pattern to `\\w`, which reads like
    a tidy-up and is Unicode-aware by default in Python, fails here rather than silently
    admitting a whole alphabet of look-alikes into what may be *added* to the allowlist.
    """
    cyrillic_a = "\u0430"
    label = f"{cyrillic_a}ddress_balance"
    assert label != "address_balance"
    assert label not in ENDPOINT_LABELS
    assert not ENDPOINT_LABEL_PATTERN.fullmatch(label)

    request = httpx.Request(
        "GET", f"{TEST_ORIGIN}/anything", extensions={ENDPOINT_EXTENSION: label}
    )

    assert request_target(request) == f"{TEST_ORIGIN}/{UNLABELLED}"
