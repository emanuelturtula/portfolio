"""Criterion 8: `docs/providers.md` documents what a new chain must implement.

Checked as substance rather than as prose. The wording is free to change; what cannot
change without failing is that every protocol member is named, that the traps a new
provider author will actually fall into are written down, and that the things this change
guessed at are labelled as guesses.

The protocol members are read off the protocol rather than listed here, deliberately --
the opposite choice from `tests/providers/test_protocol.py`, and for the opposite reason.
There, a derived list could shrink silently along with the interface. Here, deriving is the
point: adding a member to `ChainProvider` must fail this test until the document mentions
it, which is how a document stays a description of the thing rather than of its ancestor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from portfolio.providers import errors
from portfolio.providers.base import ChainProvider

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
PROVIDER_DOC: Final = REPO_ROOT / "docs" / "providers.md"


def document() -> str:
    assert PROVIDER_DOC.is_file(), f"{PROVIDER_DOC} does not exist"
    return PROVIDER_DOC.read_text(encoding="utf-8")


def protocol_members() -> frozenset[str]:
    """Whatever `ChainProvider` requires today, read off the protocol itself."""
    attrs: object = getattr(ChainProvider, "__protocol_attrs__", None)
    assert isinstance(attrs, set), "ChainProvider is not a Protocol, or typing changed"
    return frozenset(str(name) for name in attrs)


def test_the_provider_document_names_every_protocol_member() -> None:
    """A member nobody documented is a member the next implementer discovers from mypy.

    Derived from the protocol, so this fails the moment a member is added without the
    document following -- which is the only way a "what a new chain must implement"
    document stays true.
    """
    text = document()

    missing = sorted(name for name in protocol_members() if name not in text)

    assert missing == [], f"docs/providers.md does not mention: {missing}"


def test_the_protocol_member_set_is_not_empty() -> None:
    """The control. An empty set makes the test above pass against an empty document."""
    assert len(protocol_members()) >= 4


@pytest.mark.parametrize(
    "phrase",
    [
        # The helpers a provider cannot skip without reinventing the contract.
        "align_balances",
        "chunk_addresses",
        "ChainCapabilities",
        "max_addresses_per_call",
        "register_chain_provider",
        "build_http_client",
        # The two rules that are easy to break and hard to notice.
        "endpoint",
        "extensions",
        "Retry-After",
        # The layering decision a new provider will otherwise get wrong.
        "domain",
    ],
)
def test_the_document_covers_the_mechanisms_and_not_only_the_verdicts(phrase: str) -> None:
    """Each phrase is something a new provider author has to know by name to use.

    A document that said "implement the protocol and register it" would be true and
    useless. These are the names somebody has to search the codebase for otherwise.
    """
    assert phrase in document()


def test_the_document_says_the_path_is_never_logged() -> None:
    """The one rule a provider can break from outside the transport's reach.

    `RetryingTransport` controls what *it* logs. A provider that writes its own log line
    with `request.url` in it bypasses the whole control, and the only thing standing
    between that and a disclosure is that the author read this.
    """
    text = document()

    assert "path" in text
    assert "<unlabelled>" in text or "UNLABELLED" in text


def test_the_document_marks_the_unverified_vendor_numbers_as_unverified() -> None:
    """Neither target API's rate limit or batch ceiling has been measured against anything.

    A document that stated them flatly would turn three guesses into three facts, and #7
    and #8 would implement against them without checking. Saying which is which is what
    makes them correctable rather than inherited.
    """
    text = document()

    assert "Not confirmed" in text or "not confirmed" in text
    assert "rate limit" in text or "rate-limit" in text


def test_the_document_records_the_rate_limit_as_unpublished() -> None:
    """Criterion 6 of #7: the limit is unpublished, enforced by ban, and dated.

    Verified against mempool.space's own documentation on 2026-09-22: it states that
    exceeding the limits returns 429 and that repeatedly exceeding them may result in a
    ban, and it publishes **no numbers**. One request per second is therefore a guess made
    from the shape of a warning rather than from a measurement, and the first real
    evidence will be a 429 in a production log.

    Three separate things have to be in the document and each is a different failure if it
    is missing. That the limit is unpublished, or the next person reads `1000` as a
    measured figure and halves it. That the enforcement is a **ban**, or it reads as an
    ordinary throttle to wait out -- and being banned from a free public index outlives
    the sync that caused it. And the **date**, because a vendor fact with no date is a
    fact nobody can tell has expired.
    """
    text = document()

    assert "1000" in text or "1,000" in text or "one request per second" in text.lower()
    assert "ban" in text.lower(), "the enforcement mechanism is the reason for the floor"
    assert "2026-09-22" in text, "a vendor fact with no date cannot be known to be stale"
    lowered = text.lower()
    assert "unpublished" in lowered or "publishes no" in lowered or "not published" in lowered


def test_the_document_records_the_wrong_network_residual() -> None:
    """The failure nothing in this change can detect, written down where #8 will read it.

    `tb1` is testnet3, testnet4 and signet alike, and a base58 regtest address is
    indistinguishable from a testnet one. An operator who points the base URL at signet
    while holding testnet4 addresses gets confident, wrong answers, and no check built out
    of the address can see it. A residual that is not documented is a residual the next
    person rediscovers from a wrong balance.
    """
    text = document()
    lowered = text.lower()

    assert "signet" in lowered
    assert "regtest" in lowered
    assert "testnet4" in lowered or "testnet3" in lowered


def test_the_document_names_the_endpoint_label_allowlist() -> None:
    """Criterion 8's mechanism, in the document a new provider author copies from.

    A provider author who does not know `ENDPOINT_LABELS` exists writes a label, sees
    `<unlabelled>` in the log, and either removes the label or -- far worse -- concludes
    the logging is broken and writes their own log line with the URL in it. That is the
    one route around the transport's whole guarantee, and a document is what closes it.
    """
    text = document()

    assert "ENDPOINT_LABELS" in text
    assert "address_balance" in text
    assert "block_tip_height" in text


def test_the_document_records_the_work_this_change_deliberately_left_undone() -> None:
    """The lifespan wiring is #10's, and an undocumented omission reads as an oversight.

    Nothing builds the shared client at startup yet, because nothing calls a provider --
    holding a connection pool open for a caller that does not exist would be worse than
    the gap. That is a decision, and a decision nobody wrote down is indistinguishable
    from a thing somebody forgot.
    """
    text = document()

    assert "#10" in text
    assert "lifespan" in text


def test_the_document_contains_no_mainnet_address_or_real_hostname() -> None:
    """Rule 3 applies to documentation exactly as it applies to a fixture.

    An example in a document is the thing people copy, so a mainnet address here would be
    copied into a test, and an infrastructure hostname would be copied into a config.
    """
    text = document()

    forbidden_prefixes = ("bc1q", "bc1p", "kaspa:q", "xpub", "ypub", "zpub")
    found = [prefix for prefix in forbidden_prefixes if prefix in text]

    assert found == [], f"docs/providers.md contains mainnet material: {found}"


# --------------------------------------------------------------------------------------
# The error mapping, which is the part a new provider copies rather than reasons about
# --------------------------------------------------------------------------------------


def test_the_document_gives_a_mapping_for_every_error_the_package_defines() -> None:
    """Every public error class is named in the document, derived from the module.

    Derived rather than listed, for the same reason the protocol members are: a class
    added to `providers/errors.py` without a line in the document is a class the next
    provider author will map by guesswork. `ProviderUnavailableError` and
    `ProviderRateLimitedError` have no raiser in this change, which makes documenting
    them more important rather than less -- there is no code to read instead.
    """
    text = document()

    missing = sorted(name for name in errors.__all__ if name not in text)

    assert missing == [], f"docs/providers.md does not mention: {missing}"


@pytest.mark.parametrize(
    "phrase",
    [
        # The three-way split, by the status that decides it.
        "httpx.TransportError",
        "ProviderUnavailableError",
        "ProviderRateLimitedError",
        "ProviderResponseError",
        "429",
    ],
)
def test_the_document_shows_how_a_failure_becomes_one_of_the_typed_errors(phrase: str) -> None:
    """The mapping is the thing #7 and #8 will copy, so it has to be in the document.

    The transport deliberately does not translate: it returns the last response and lets
    an `httpx.TransportError` propagate, because what a failure *means* for a balance is
    the provider's judgement. That decision only works if the judgement is written down
    somewhere a provider author will find it.
    """
    assert phrase in document()


def test_the_document_says_why_a_4xx_is_not_unavailability() -> None:
    """The mapping most easily got wrong, and the one with a concrete consequence.

    A self-hosted Esplora behind an auth proxy starts returning 401. Mapped to
    "unavailable", the owner is told their chain is down -- forever, on every sync, with
    nothing anywhere mentioning a credential. It is the difference between an error
    somebody can act on and one they learn to ignore.
    """
    text = document()

    assert "401" in text
    assert "404" in text or "400" in text
