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

import re
from pathlib import Path
from typing import Final

import pytest

from portfolio.providers import errors
from portfolio.providers.base import ChainProvider
from portfolio.providers.prices.base import SUPPORTED_PAIRS, sources_for
from portfolio.providers.prices.registry import price_sources
from portfolio.services.prices import STALE_AFTER
from tests.providers.prices.harness import (
    SYNTHETIC_COINGECKO_KEY,
    PriceFake,
    price_client,
    price_settings,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
PROVIDER_DOC: Final = REPO_ROOT / "docs" / "providers.md"

#: The multiplication sign the document is written with, as an escape rather than as the
#: character. Ruff's RUF001 flags a literal U+00D7 in source as ambiguous with the letter
#: `x`, which is a fair complaint about a character nobody can tell apart in a diff -- and
#: the document is prose, where the typographic sign is the right one to read.
TIMES: Final = "\u00d7"

#: Every `a TIMES b = c` written in the document, so the arithmetic can be evaluated
#: rather than read. A budget nobody checked is a number somebody once believed.
MULTIPLICATION: Final = re.compile(rf"(\d[\d,]*)\s*[{TIMES}x]\s*(\d[\d,]*)\s*=\s*(\d[\d,]*)")

HOURS_PER_DAY: Final = 24


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


def test_the_document_records_the_kaspa_limits_and_the_example_trap() -> None:
    """Criterion 7 of #8: the undocumented limits, and the trap in the vendor's own document.

    Four separate facts, and each is a different failure if it is missing.

    **The batch ceiling is a guess.** The OpenAPI document declares `addresses` as an array
    of strings with no `maxItems` and the operation description names no ceiling, confirmed
    against the live document on 2026-09-22. A document that stated 64 flatly would turn a
    guess into a fact, and the next person would size a request against it. The first real
    evidence will be a refused batch in production.

    **`ratelimit-*` does not appear at all**, measured against both endpoints on
    2026-09-23: the API is behind Cloudflare, so the parser criterion 4 asks for is code
    nothing in production exercises. Unexercised code that looks tested is how a green
    suite lies, and the only thing that stops the next reader believing this path is
    covered is a sentence saying it is not.

    **The mainnet-examples trap.** The vendor's document uses real mainnet addresses as its
    example values, and an example is the thing people copy. Copied into a fixture it fails
    the secret scan; copied into a fixture that somehow passes, it is a rule 3 violation in
    a public repository.

    **And the dates**, because a vendor fact with no date is a fact nobody can tell has
    expired.
    """
    text = document()
    lowered = text.lower()

    assert "maxitems" in lowered or "no documented limit" in lowered or "undocumented" in lowered
    assert "64" in text, "the batch ceiling this release ships has to be findable"
    assert "ratelimit" in lowered, "criterion 4's headers, and the fact they never arrive"
    assert "cloudflare" in lowered, "the reason the headers never arrive"
    assert "mainnet" in lowered, "the example trap is about mainnet addresses specifically"
    assert "example" in lowered
    assert "2026-09-23" in text, "the measurement that says the headers are absent is dated"


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


# --------------------------------------------------------------------------------------
# Criterion 7 of #9: the monthly call budget is calculated and written down
# --------------------------------------------------------------------------------------
#
# "Calculated and written down" is two claims, and a document can satisfy the second
# without the first. So the arithmetic in it is evaluated rather than read, and the figure
# everything else rests on -- 24 refreshes a day -- is tied back to `STALE_AFTER`, which is
# the only thing that makes 24 the right number.


def test_the_document_states_the_measured_call_budget() -> None:
    """Criterion 7: the number, the request, the date, and the claim it rests on.

    Each of these is a different failure if it is missing. Without **720** there is no
    budget. Without **one request per refresh** the 720 is unexplained, and that figure is
    what the whole design rests on -- an unbatched primary would make it 2,880. Without the
    **date** it is a vendor fact nobody can tell has expired. And without the measured
    request a reader cannot reproduce it.
    """
    text = document()
    lowered = text.lower()

    assert "720" in text, "the monthly budget is the number criterion 7 asks for"
    assert "0/public/Ticker" in text, "the measured request has to be reproducible"
    assert "2026-09-23" in text, "a vendor fact with no date cannot be known to be stale"
    assert "budget" in lowered
    assert "one request per refresh" in lowered


def test_the_arithmetic_in_the_document_is_right() -> None:
    """Every `a x b = c` in the document, evaluated rather than read.

    A budget is only worth writing down if it is correct, and a written multiplication is
    the one kind of claim a test can check completely. The 30-day figure, the 31-day worst
    case and the yearly total are all stated; a typo in any of them would survive every
    substring assertion above.
    """
    text = document()

    statements = MULTIPLICATION.findall(text)

    assert statements, "the document states no arithmetic at all"
    wrong = [
        f"{left} x {right} = {product}"
        for left, right, product in statements
        if int(left.replace(",", "")) * int(right.replace(",", "")) != int(product.replace(",", ""))
    ]
    assert wrong == [], f"docs/providers.md states arithmetic that is wrong: {wrong}"


def test_the_document_states_the_budget_the_shipped_threshold_actually_produces() -> None:
    """The join: 24 refreshes a day is right **because** `STALE_AFTER` is one hour.

    This is the assertion that stops the document going quietly stale. Change `STALE_AFTER`
    to fifteen minutes and every figure in that table is wrong by a factor of four -- the
    arithmetic still checks out, the date is still there, and nothing else in the suite has
    an opinion. Deriving the cadence from the constant is what ties the two together.

    The monthly figure is derived here and then looked for in the document, rather than the
    document being trusted: `720` written next to a `24` that no longer follows from
    anything is exactly the shape of a number nobody rechecked.
    """
    text = document()

    refreshes_per_day = int(HOURS_PER_DAY * 3600 // STALE_AFTER.total_seconds())
    per_month = refreshes_per_day * 30

    assert refreshes_per_day == HOURS_PER_DAY, (
        f"STALE_AFTER is {STALE_AFTER}, so a refresh keyed to it runs {refreshes_per_day} "
        f"times a day and the document's figures no longer follow from it"
    )
    assert str(per_month) in text
    assert f"{refreshes_per_day} {TIMES} 30 = {per_month}" in text


def test_the_document_bounds_what_a_bad_day_costs() -> None:
    """The budget on the day the primary is down, which is the number nobody writes down.

    720 is the healthy case and it is the easy half. The fallbacks are not batched --
    Coinbase is one request per pair -- so a reader who had only the healthy figure would be
    surprised by the first outage. Bounding it is what makes the design's own claim ("it
    never costs more than one request per pair per source") checkable rather than soothing.
    """
    # Whitespace collapsed before the search. Markdown wraps at eighty columns, so a phrase
    # this test cares about is as likely to be split across two lines as not -- and an
    # assertion that fails on a line break is one somebody deletes rather than reads.
    flattened = " ".join(document().split()).lower()

    assert "if kraken fails" in flattened or "kraken down" in flattened
    assert "per pair per source" in flattened
    assert "3 requests" in flattened, "the bound itself, not only the claim that there is one"


@pytest.mark.parametrize(
    "pair",
    sorted(SUPPORTED_PAIRS),
    ids=lambda pair: f"{pair[0]}/{pair[1]}",
)
def test_the_document_names_the_source_order_the_code_actually_produces(
    pair: tuple[str, str],
) -> None:
    """The per-pair table, derived from the shipped code and looked up in the document.

    Derived rather than listed, the same choice the protocol members make and for the same
    reason: adding a source, or reordering `price_sources`, must fail this test until the
    table follows. A hand-written expectation here would agree with a document that had gone
    stale.

    Every source name in the shipped order has to appear in that pair's row, **in that
    order** -- so a table listing the right sources in the wrong sequence is caught, and the
    sequence is the whole of what the table is claiming.
    """
    asset_symbol, quote_currency = pair
    sources = price_sources(
        price_client(PriceFake()),
        settings=price_settings(coingecko_api_key=SYNTHETIC_COINGECKO_KEY),
    )
    expected = [source.name for source in sources_for(asset_symbol, quote_currency, sources)]

    row = next(
        (
            line
            for line in document().splitlines()
            if line.startswith(f"| {asset_symbol}/{quote_currency} ")
        ),
        None,
    )

    assert row is not None, f"docs/providers.md has no row for {asset_symbol}/{quote_currency}"
    positions = [row.lower().find(name) for name in expected]
    assert all(position >= 0 for position in positions), (
        f"{asset_symbol}/{quote_currency} is documented as {row!r}, "
        f"which does not name every source in {expected}"
    )
    assert positions == sorted(positions), (
        f"{asset_symbol}/{quote_currency} is documented in the wrong order; "
        f"the code tries {expected}"
    )


def test_the_document_records_what_is_assumed_rather_than_measured() -> None:
    """Three guesses this change ships with, each named where the next reader will meet it.

    The Kaspa endpoint's **currency** is an inference from a number's magnitude. CoinGecko's
    **response shape** comes from documentation, because measuring it needs a key this
    repository must not contain. And KAS/EUR has exactly one key-free source, so losing
    Kraken loses that pair. A document that stated all three flatly would turn three guesses
    into three facts, and #10 would build on them without checking.
    """
    lowered = document().lower()

    assert "assum" in lowered, "the Kaspa currency assumption has to be named as one"
    assert "not measured" in lowered or "not verified" in lowered
    assert "coingecko" in lowered
    assert "kas/eur" in lowered


def test_the_document_names_the_contract_that_keeps_prices_off_a_request_path() -> None:
    """Criterion 2's mechanism, in the document a future provider author reads first.

    Somebody who does not know the contract exists writes "just refresh it if it is stale"
    into the valuation service, watches CI fail on a layering check they have never seen,
    and either reads the contract or works around it. Which of those happens is decided by
    whether this paragraph is here.
    """
    text = document()

    assert "prices-are-never-fetched-in-a-request" in text
    assert "allow_indirect_imports" in text
    assert "services/price_refresh.py" in text


def test_the_document_explains_the_float_boundary_and_where_it_is() -> None:
    """Rule 2 at the one place in this change where it is not where anyone would look.

    A vendor sending a price as a JSON number is the whole subject of #9, and the fix is in
    a **shared decoder** rather than in the parser that meets it. Naming `decode_json` and
    `parse_float` is what stops the next provider author writing their own `json.loads` and
    quietly reintroducing the thing this issue existed to remove.
    """
    text = document()

    assert "decode_json" in text
    assert "parse_float" in text
    assert "NumericText" in text


def test_the_document_carries_no_credential_and_no_key_shaped_string() -> None:
    """Rule 3 over the document, including a plausible-looking fake.

    An example in a document is the thing people copy, so a key-shaped string here would be
    copied into a `.env` and then into a screenshot. The header **name** is expected to be
    present -- an operator needs it -- and no value beside it.
    """
    text = document()

    assert "x-cg-demo-api-key" in text, "the header name is documentation, not a secret"
    assert "CG-" not in text, "a CoinGecko key shape must not appear, even as an example"
    assert SYNTHETIC_COINGECKO_KEY not in text
    assert not re.search(r"PORTFOLIO_COINGECKO_API_KEY\s*=\s*\S", text), (
        "the document assigns a value to the key variable"
    )


def test_building_the_documented_sources_reaches_no_vendor() -> None:
    """The control on this module's own harness: construction costs no request.

    The table test above builds the shipped sources once per pair. If construction made a
    call, a documentation test would be reaching four real vendors on every CI run -- which
    is the sort of thing nobody notices until a rate limiter does.
    """
    fake = PriceFake()

    price_sources(price_client(fake), settings=price_settings())
    price_sources(
        price_client(fake),
        settings=price_settings(coingecko_api_key=SYNTHETIC_COINGECKO_KEY),
    )

    assert fake.requests == []


# --------------------------------------------------------------------------------------
# Criterion 1 of #13: the Bitget facts, confirmed against the live documentation
# --------------------------------------------------------------------------------------
#
# Searched inside the Bitget section rather than the whole document: "90" and "order" are
# in the document a dozen times over, and a substring that is true of the file is not a
# statement about the venue.

#: A link into Bitget's own documentation, current or the static legacy copy.
BITGET_DOCS_URL: Final = re.compile(r"https://www\.bitget\.com/(?:legacy-)?docs/\S+")


def heading_level(line: str) -> int:
    return len(line) - len(line.lstrip("#"))


def bitget_section() -> str:
    """From the first heading naming Bitget to the next heading at its level or above.

    Fenced code is skipped when looking for headings, so a `# comment` in a shell example
    is not mistaken for one.
    """
    headings: list[tuple[int, str]] = []
    fenced = False
    lines = document().splitlines()
    for index, line in enumerate(lines):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            headings.append((index, line))
    start = next((index for index, line in headings if "Bitget" in line), None)
    assert start is not None, "docs/providers.md has no heading naming Bitget"
    level = heading_level(lines[start])
    end = next(
        (index for index, line in headings if index > start and heading_level(line) <= level),
        len(lines),
    )
    return "\n".join(lines[start:end])


def flattened(text: str) -> str:
    """Whitespace collapsed: Markdown wraps at a column, and a phrase can break anywhere."""
    return " ".join(text.split())


@pytest.mark.parametrize(
    "phrase",
    [
        "/api/v2/spot/trade/fills",
        "idLessThan",
        "tradeId",
        "UTA",
        "/api/v3/trade/fills",
        "2026-09-25",
        "Classic",
    ],
)
def test_the_document_records_the_confirmed_bitget_facts(phrase: str) -> None:
    """The endpoint, the cursor and what it takes, the UTA finding, and the date read.

    Criterion 1 of #13 is that these were confirmed against the live documentation before
    any code, and recorded. A fact with no date is one nobody can tell has expired, and
    this venue's documentation moved once already: every old `api-doc` URL now redirects.
    """
    assert phrase in flattened(bitget_section())


def test_the_bitget_section_links_the_documentation_and_says_what_is_confirmed() -> None:
    section = flattened(bitget_section())

    assert BITGET_DOCS_URL.search(section), "no link into bitget.com/docs"
    assert re.search(r"\b90[ -]days?\b", section), "the retention is not stated in days"
    assert "confirmed" in section.lower(), "the owner's account type is recorded as confirmed"


@pytest.mark.parametrize(
    ("what", "pattern"),
    [
        ("that these are undocumented at all", r"not documented|undocumented"),
        ("whether startTime and endTime are inclusive", r"inclusive"),
        ("the order of fills within a page", r"within a page"),
        ("the fee's sign", r"fee'?s sign|fee sign|sign of the fee"),
        ("BGB fee deduction", r"\bbgb\b"),
    ],
)
def test_the_document_lists_what_bitget_does_not_document(what: str, pattern: str) -> None:
    """Each gap the provider was written around, named as a gap rather than as a fact.

    Stated flatly, any one of these would turn a guess into a fact #15 builds on: the window
    is widened because inclusivity is unknown, the cursor is the smallest id because the
    order is unknown, and a positive fee and BGB deduction are refused because their meaning
    is unknown.
    """
    assert re.search(pattern, flattened(bitget_section()).lower()), f"not named: {what}"


def test_the_document_assigns_no_value_to_a_bitget_variable() -> None:
    """Rule 3: the variable names are documentation; a value beside one is a leak or a lure."""
    text = document()

    assert "PORTFOLIO_BITGET_API_KEY" in text, "the positive companion: the names are there"
    assert not re.search(r"PORTFOLIO_BITGET_API_(?:KEY|SECRET|PASSPHRASE)\s*=\s*\S", text)
