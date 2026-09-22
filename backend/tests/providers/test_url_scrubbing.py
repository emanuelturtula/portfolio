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
    ENDPOINT_EXTENSION,
    UNLABELLED,
    request_target,
    strip_query,
)
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
# The label is a pattern, not a promise
# --------------------------------------------------------------------------------------
#
# `request_target` could not keep its docstring's promise while the label was anything a
# caller put in `extensions`. A provider author wanting more detail writes
# `{"endpoint": request.url.path}` -- helpfulness, not malice -- and the address is in
# every retry and failure line, out of the one function that says it cannot be. The label
# now has to match `ENDPOINT_LABEL`; anything else renders as `UNLABELLED`.


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
        pytest.param("address_balance", id="the label #7 will use"),
        pytest.param("a", id="one character, the shortest legal label"),
        pytest.param("balances", id="no underscore"),
        pytest.param("utxo_set_v2", id="digits and underscores"),
        pytest.param("a" * 32, id="exactly the length cap"),
    ],
)
def test_a_well_formed_label_is_still_carried(label: str) -> None:
    """The control. A pattern that rejected everything would pass every test above.

    Without this the fix could ship as "always `<unlabelled>`", which would satisfy every
    absence assertion in this file and destroy the one thing the label is for.
    """
    request = httpx.Request(
        "GET", f"{TEST_ORIGIN}/anything", extensions={ENDPOINT_EXTENSION: label}
    )

    assert request_target(request) == f"{TEST_ORIGIN}/{label}"


def test_the_label_pattern_does_not_make_a_deliberate_address_impossible() -> None:
    """The residual, asserted rather than left for a reader to discover.

    A bech32 address is lowercase alphanumeric, so a label that *is* a short address
    matches the pattern. What the pattern removes is the accidental case -- a path, a
    suffix, anything with a slash or a dot or an upper-case letter. Stating the limit in
    a test is the difference between a control and a control people believe is stronger
    than it is.

    The honest completion is an allowlist of known labels, the shape `PUBLIC_API_PATHS`
    has. That belongs with #7, when there are labels to list.
    """
    truncated = BIP173_TESTNET_P2WPKH[:20]
    assert truncated.isascii()
    assert truncated.islower()

    request = httpx.Request(
        "GET", f"{TEST_ORIGIN}/anything", extensions={ENDPOINT_EXTENSION: truncated}
    )

    # Deliberately asserting the *unsafe* outcome, because it is the current contract.
    # If a later change makes this render as `<unlabelled>`, this test failing is the
    # notification that the residual closed -- not a regression.
    assert request_target(request) == f"{TEST_ORIGIN}/{truncated}"


def test_the_length_cap_is_hygiene_and_the_shape_is_the_control() -> None:
    """A full address is rejected by the cap; a truncated one is not. Say so.

    42 characters of bech32 exceeds the 32-character cap, which reads like a defence and
    is not one: a truncated address is still an address, and 20 characters of it is
    enough to search an explorer with. The cap bounds a log line. The pattern is what
    does the work.
    """
    assert len(BIP173_TESTNET_P2WPKH) > 32
    assert len(BIP173_TESTNET_P2WPKH[:20]) <= 32


def test_a_unicode_homoglyph_label_renders_as_unlabelled() -> None:
    """A label that looks like a legal one but is not ASCII.

    `[a-z]` in a Python regex does not match a Cyrillic small a, so this is already
    refused -- but only because the pattern is ASCII by construction rather than because
    anybody chose it. Asserted so that widening the pattern to `\\w`, which reads like a
    tidy-up and is Unicode-aware by default in Python, fails here instead of silently
    admitting a whole alphabet of look-alikes.
    """
    cyrillic_a = "\u0430"
    label = f"{cyrillic_a}ddress_balance"
    assert label != "address_balance"

    request = httpx.Request(
        "GET", f"{TEST_ORIGIN}/anything", extensions={ENDPOINT_EXTENSION: label}
    )

    assert request_target(request) == f"{TEST_ORIGIN}/{UNLABELLED}"
