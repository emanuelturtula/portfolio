"""Criteria 3 and 8: the exchange error taxonomy, driven through the error map alone.

**No HTTP library is imported here, and that is criterion 3's "no HTTP mocking" made
mechanical.** The classification is a pure function of `(status, venue_code, error_map)`,
so every class is reached by calling it -- no transport, no fake server, no response object.
`test_this_module_imports_no_http_library` reads this file's own imports and fails if that
ever stops being true.

Every classification assertion checks the **exact** class with `is`, never `isinstance`:
`ExchangeInsufficientScopeError` is an `ExchangeAuthError` and
`ExchangeRetentionWindowError` is an `ExchangeInvalidRequestError`, so an `isinstance`
assertion on the parent passes for a map that produced the child, and vice versa for
nothing. The taxonomy is the subject; the parent is not an acceptable answer.

The venue codes below are synthetic. No real venue's code table is confirmed in #12 (the
spec's Risks section), and a test that looked like it pinned Bitget's or BingX's codes would
be read as evidence that somebody had checked them.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import pytest
import structlog

from portfolio.providers.errors import (
    ProviderError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from portfolio.providers.exchanges.errors import (
    STATUS_FALLBACKS,
    ExchangeAuthError,
    ExchangeError,
    ExchangeInsufficientScopeError,
    ExchangeInvalidRequestError,
    ExchangeRateLimitedError,
    ExchangeRetentionWindowError,
    ExchangeSchemaError,
    ExchangeUnavailableError,
    build_error_map,
    classify_error,
    exchange_error,
    venue_code_of,
)
from tests.providers.test_protocol import imported_roots
from tests.security.conftest import assert_carried_something

if TYPE_CHECKING:
    from collections.abc import Mapping

    from portfolio.providers.exchanges.errors import ErrorMap
    from tests.providers.exchanges.conftest import LoggingInstaller

type ErrorClass = type[ExchangeError]
type MapKey = tuple[int | None, str | None]

#: The seven classes the spec names, and nothing else. Pinned by hand.
TAXONOMY: Final[tuple[ErrorClass, ...]] = (
    ExchangeAuthError,
    ExchangeInsufficientScopeError,
    ExchangeRateLimitedError,
    ExchangeUnavailableError,
    ExchangeInvalidRequestError,
    ExchangeRetentionWindowError,
    ExchangeSchemaError,
)

#: The six whose constructor takes no free text. The schema error is the one exception.
REFUSALS: Final = tuple(cls for cls in TAXONOMY if cls is not ExchangeSchemaError)

#: A venue map with one entry per class, under each of the three key shapes a venue uses:
#: an in-band code on any status (BingX-style), a code under one status, a status alone.
VENUE_MAP: Final[ErrorMap] = build_error_map(
    {
        (None, "100001"): ExchangeAuthError,
        (403, "30002"): ExchangeInsufficientScopeError,
        (None, "100410"): ExchangeRateLimitedError,
        (200, "80012"): ExchangeUnavailableError,
        (200, "100400"): ExchangeInvalidRequestError,
        (400, "40017"): ExchangeRetentionWindowError,
        (200, "100500"): ExchangeSchemaError,
    }
)

EMPTY_MAP: Final[ErrorMap] = build_error_map({})

#: What a 401 body can carry, and the reason criterion 8 exists: a venue's `msg` routinely
#: echoes the request, which on a signed venue includes the key and the signature. The
#: provider parses `code` out of it; the sentinel stands in for everything else.
BODY_SENTINEL: Final = "sentinel-response-body-echo"

REPO_TESTS: Final = Path(__file__).resolve()


def every_attribute(error: BaseException) -> str:
    """Everything an exception holds, rendered: message, repr, args, and every attribute.

    `vars` covers the instance dictionary; `__slots__` anywhere in the MRO covers a slotted
    subclass; the public names from `dir` cover a property. A leak into any of them reaches
    a log the moment something renders the exception with `rich`, `repr` or `vars`.
    """
    parts = [str(error), repr(error), repr(error.args), repr(vars(error))]
    for cls in type(error).__mro__:
        for name in getattr(cls, "__slots__", ()):
            parts.append(repr(getattr(error, name, None)))
    for name in dir(error):
        if not name.startswith("_") and name not in {"args", "with_traceback", "add_note"}:
            parts.append(repr(getattr(error, name)))
    return "\n".join(parts)


def build(cls: ErrorClass, **fields: object) -> ExchangeError:
    """One instance of any taxonomy class; the schema error needs its `detail`."""
    if cls is ExchangeSchemaError:
        return ExchangeSchemaError("field price: not a decimal string", **fields)  # type: ignore[arg-type]
    return cls(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Criterion 3: every class, through the map, exactly
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        pytest.param(200, "100001", ExchangeAuthError, id="auth"),
        pytest.param(403, "30002", ExchangeInsufficientScopeError, id="insufficient-scope"),
        pytest.param(200, "100410", ExchangeRateLimitedError, id="rate-limited"),
        pytest.param(200, "80012", ExchangeUnavailableError, id="unavailable"),
        pytest.param(200, "100400", ExchangeInvalidRequestError, id="invalid-request"),
        pytest.param(400, "40017", ExchangeRetentionWindowError, id="retention-window"),
        pytest.param(200, "100500", ExchangeSchemaError, id="schema"),
    ],
)
def test_every_taxonomy_class_is_reachable_through_an_error_map(
    status: int, code: str, expected: ErrorClass
) -> None:
    """Classified and constructed, each landing on exactly the class the map names."""
    assert classify_error(status, code, VENUE_MAP) is expected

    error = exchange_error(status, code, error_map=VENUE_MAP)

    assert type(error) is expected
    assert error.status == status
    assert error.venue_code == code


def test_the_parametrisation_above_covers_all_seven_classes() -> None:
    """The control: a class dropped from the table above would otherwise go untested."""
    assert set(VENUE_MAP.values()) == set(TAXONOMY)
    assert len(TAXONOMY) == 7


# --------------------------------------------------------------------------------------
# Criterion 3: lookup precedence
# --------------------------------------------------------------------------------------

PRECEDENCE_STATUS: Final = 429
PRECEDENCE_CODE: Final = "50001"

#: Four candidate answers for one `(429, "50001")`, each a different class so the winner is
#: unambiguous. The fallback for 429 is rate-limited; the three map entries name classes a
#: 429 would never produce on its own.
EXACT: Final[tuple[MapKey, ErrorClass]] = (
    (PRECEDENCE_STATUS, PRECEDENCE_CODE),
    ExchangeInsufficientScopeError,
)
CODE_ANY_STATUS: Final[tuple[MapKey, ErrorClass]] = (
    (None, PRECEDENCE_CODE),
    ExchangeRetentionWindowError,
)
STATUS_ANY_CODE: Final[tuple[MapKey, ErrorClass]] = (
    (PRECEDENCE_STATUS, None),
    ExchangeInvalidRequestError,
)


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        pytest.param((EXACT, CODE_ANY_STATUS, STATUS_ANY_CODE), EXACT[1], id="all three"),
        pytest.param((CODE_ANY_STATUS, STATUS_ANY_CODE), CODE_ANY_STATUS[1], id="no exact"),
        pytest.param((STATUS_ANY_CODE,), STATUS_ANY_CODE[1], id="status only"),
        pytest.param((), ExchangeRateLimitedError, id="fallback only"),
        # The other orders of removal, so every adjacent pair is compared both ways.
        pytest.param((EXACT, STATUS_ANY_CODE), EXACT[1], id="exact beats status"),
        pytest.param((EXACT, CODE_ANY_STATUS), EXACT[1], id="exact beats code"),
        pytest.param((EXACT,), EXACT[1], id="exact beats fallback"),
        pytest.param((CODE_ANY_STATUS,), CODE_ANY_STATUS[1], id="code beats fallback"),
    ],
)
def test_lookup_precedence_is_exact_then_code_then_status_then_fallback(
    entries: tuple[tuple[MapKey, ErrorClass], ...], expected: ErrorClass
) -> None:
    """A map where all four could match, with entries removed one at a time.

    Swapping any two adjacent steps in `classify_error` changes the answer of at least one
    row here, which is what makes this a test of the order rather than of the presence of
    each step.
    """
    error_map = build_error_map(dict(entries))

    assert classify_error(PRECEDENCE_STATUS, PRECEDENCE_CODE, error_map) is expected
    assert type(exchange_error(PRECEDENCE_STATUS, PRECEDENCE_CODE, error_map=error_map)) is (
        expected
    )


def test_an_entry_for_another_status_or_code_does_not_match() -> None:
    """Keys are matched, not approximated: a neighbouring status or code is not a hit."""
    error_map = build_error_map(
        {
            (400, "50001"): ExchangeInsufficientScopeError,
            (None, "50002"): ExchangeRetentionWindowError,
            (428, None): ExchangeUnavailableError,
        }
    )

    assert classify_error(429, "50001", error_map) is ExchangeRateLimitedError


def test_an_unrecognisable_code_falls_back_to_the_status() -> None:
    """A code `venue_code_of` drops is treated as no code, so the status still classifies it.

    The spec's Risks section: an alphanumeric code from a venue is dropped, and the failure
    is classified less specifically rather than not at all.
    """
    error_map = build_error_map({(401, None): ExchangeInsufficientScopeError})

    assert classify_error(401, "not-a-code", error_map) is ExchangeInsufficientScopeError
    assert classify_error(401, "not-a-code", EMPTY_MAP) is ExchangeAuthError


def test_a_raw_integer_code_matches_its_string_entry() -> None:
    """`classify_error` normalises a raw code, so a venue sending `100001` as a JSON number
    is classified by the entry written as `"100001"`."""
    assert classify_error(200, 100001, VENUE_MAP) is ExchangeAuthError
    assert exchange_error(200, 100001, error_map=VENUE_MAP).venue_code == "100001"


# --------------------------------------------------------------------------------------
# Criterion 3: the fallbacks every venue shares
# --------------------------------------------------------------------------------------


def test_the_status_fallbacks_are_the_pinned_table() -> None:
    """The four statuses the spec names, pinned by hand. 403 is auth, not scope, not outage."""
    assert dict(STATUS_FALLBACKS) == {
        401: ExchangeAuthError,
        403: ExchangeAuthError,
        408: ExchangeUnavailableError,
        429: ExchangeRateLimitedError,
    }


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        pytest.param(401, None, ExchangeAuthError, id="401"),
        pytest.param(403, None, ExchangeAuthError, id="403"),
        pytest.param(408, None, ExchangeUnavailableError, id="408"),
        pytest.param(429, None, ExchangeRateLimitedError, id="429"),
        pytest.param(400, None, ExchangeInvalidRequestError, id="400"),
        pytest.param(404, None, ExchangeInvalidRequestError, id="404"),
        pytest.param(499, None, ExchangeInvalidRequestError, id="499"),
        pytest.param(500, None, ExchangeUnavailableError, id="500"),
        pytest.param(503, None, ExchangeUnavailableError, id="503"),
        pytest.param(599, None, ExchangeUnavailableError, id="599"),
        pytest.param(200, "12345", ExchangeSchemaError, id="200-with-unmapped-code"),
        pytest.param(200, None, ExchangeSchemaError, id="200-without-a-code"),
        pytest.param(302, None, ExchangeSchemaError, id="302"),
        pytest.param(None, "12345", ExchangeSchemaError, id="no-status-unmapped-code"),
        # A known status still wins over an unmapped code: the code adds nothing.
        pytest.param(401, "12345", ExchangeAuthError, id="401-with-unmapped-code"),
        pytest.param(503, "12345", ExchangeUnavailableError, id="503-with-unmapped-code"),
    ],
)
def test_status_fallbacks(status: int | None, code: str | None, expected: ErrorClass) -> None:
    """Steps 4-6, with an empty map, so nothing but the shared rules can answer."""
    assert classify_error(status, code, EMPTY_MAP) is expected
    assert type(exchange_error(status, code, error_map=EMPTY_MAP)) is expected


# --------------------------------------------------------------------------------------
# Criterion 3: rate limits carry Retry-After
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "error_map"),
    [
        pytest.param(429, None, EMPTY_MAP, id="429-fallback"),
        pytest.param(200, "100410", VENUE_MAP, id="in-band-code"),
    ],
)
def test_a_rate_limit_carries_retry_after_in_milliseconds(
    status: int, code: str | None, error_map: ErrorMap
) -> None:
    """The value handed in is the value carried, and absence is `None` rather than zero."""
    with_header = exchange_error(status, code, error_map=error_map, retry_after_ms=1500)
    immediately = exchange_error(status, code, error_map=error_map, retry_after_ms=0)
    without_header = exchange_error(status, code, error_map=error_map)

    assert type(with_header) is ExchangeRateLimitedError
    assert type(immediately) is ExchangeRateLimitedError
    assert type(without_header) is ExchangeRateLimitedError
    assert with_header.retry_after_ms == 1500
    assert immediately.retry_after_ms == 0
    assert without_header.retry_after_ms is None


def test_the_rate_limit_constructor_carries_retry_after_directly() -> None:
    assert ExchangeRateLimitedError(status=429, retry_after_ms=250).retry_after_ms == 250
    assert ExchangeRateLimitedError(status=429).retry_after_ms is None


@pytest.mark.parametrize("value", [-1, True, "1500"], ids=["negative", "bool", "str"])
def test_a_malformed_retry_after_is_refused(value: object) -> None:
    """A wait that cannot be a number of milliseconds is a bug in the caller, said loudly."""
    with pytest.raises(ValueError, match="retry_after_ms"):
        ExchangeRateLimitedError(status=429, retry_after_ms=value)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Criterion 3: retry semantics come from the existing hierarchy
# --------------------------------------------------------------------------------------

#: Hand-written: which classes a caller should retry. "Retry later" is spelled
#: `ProviderUnavailableError` since #6, and the exchange classes answer the same question
#: through the same hierarchy rather than a second one.
RETRYABLE: Final[frozenset[ErrorClass]] = frozenset(
    {ExchangeUnavailableError, ExchangeRateLimitedError}
)


@pytest.mark.parametrize("cls", TAXONOMY, ids=lambda cls: cls.__name__)
def test_each_class_is_retryable_exactly_when_it_is_unavailable(cls: ErrorClass) -> None:
    error = build(cls, status=500)

    assert isinstance(error, ExchangeError)
    assert isinstance(error, ProviderError)
    assert isinstance(error, ProviderUnavailableError) is (cls in RETRYABLE)
    assert isinstance(error, ProviderResponseError) is (cls not in RETRYABLE)
    assert isinstance(error, ProviderRateLimitedError) is (cls is ExchangeRateLimitedError)


def test_the_two_subclass_relationships_the_spec_chose() -> None:
    """Scope is an auth failure; a retention refusal is an invalid request. And not otherwise."""
    assert issubclass(ExchangeInsufficientScopeError, ExchangeAuthError)
    assert issubclass(ExchangeRetentionWindowError, ExchangeInvalidRequestError)
    assert not issubclass(ExchangeAuthError, ExchangeInsufficientScopeError)
    assert not issubclass(ExchangeInvalidRequestError, ExchangeRetentionWindowError)
    assert issubclass(ExchangeError, ProviderError)
    # `except ExchangeError` catches all seven, and nothing that is not an exchange error.
    assert all(issubclass(cls, ExchangeError) for cls in TAXONOMY)
    assert not issubclass(ProviderUnavailableError, ExchangeError)


# --------------------------------------------------------------------------------------
# Criterion 3: a malformed map is an import-time error
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param((99, None), ExchangeAuthError, id="status below 100"),
        pytest.param((600, None), ExchangeAuthError, id="status above 599"),
        pytest.param((True, None), ExchangeAuthError, id="bool status"),
        pytest.param(("401", None), ExchangeAuthError, id="string status"),
        pytest.param((None, "abc"), ExchangeAuthError, id="alphabetic code"),
        pytest.param((None, "12345678901"), ExchangeAuthError, id="eleven-digit code"),
        pytest.param((None, ""), ExchangeAuthError, id="empty code"),
        pytest.param((None, None), ExchangeAuthError, id="matches everything"),
        pytest.param((401, None), ProviderUnavailableError, id="not an exchange class"),
        pytest.param((401, None), ValueError, id="not an error class of ours at all"),
        pytest.param((401, None), ExchangeAuthError(status=401), id="an instance"),
    ],
)
def test_build_error_map_refuses_a_malformed_entry_at_construction(
    key: object, value: object
) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - each case is its own parametrised row
        build_error_map({key: value})  # type: ignore[dict-item]


def test_a_well_formed_map_builds_and_is_read_only() -> None:
    """The positive companion: the validator is not refusing everything."""
    entries: Mapping[MapKey, ErrorClass] = {
        (401, "40001"): ExchangeAuthError,
        (None, "-1"): ExchangeUnavailableError,
        (418, None): ExchangeInvalidRequestError,
    }

    error_map = build_error_map(entries)

    assert dict(error_map) == dict(entries)
    assert isinstance(error_map, MappingProxyType)
    with pytest.raises(TypeError):
        error_map[(500, None)] = ExchangeUnavailableError  # type: ignore[index]


# --------------------------------------------------------------------------------------
# Criterion 3 and 8: a venue code is carried only if it cannot be anything else
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(40001, "40001", id="int"),
        pytest.param(0, "0", id="int zero"),
        pytest.param(-1, "-1", id="negative int"),
        pytest.param(1234567890, "1234567890", id="ten-digit int"),
        pytest.param("40001", "40001", id="digit string"),
        pytest.param("-1", "-1", id="negative digit string"),
        pytest.param("0000000001", "0000000001", id="ten characters, zero-padded"),
        pytest.param("-1234567890", "-1234567890", id="ten digits and a sign"),
    ],
)
def test_a_venue_code_is_carried_when_numeric(raw: object, expected: str) -> None:
    assert venue_code_of(raw) == expected
    assert ExchangeAuthError(status=401, venue_code=raw).venue_code == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(True, id="bool true"),
        pytest.param(False, id="bool false"),
        pytest.param(None, id="none"),
        pytest.param("abc", id="alphabetic"),
        pytest.param("40001a", id="trailing letter"),
        pytest.param("12345678901", id="eleven digits"),
        pytest.param(12345678901, id="eleven-digit int"),
        pytest.param(-12345678901, id="eleven-digit negative int"),
        # Past CPython's int-to-str digit limit, so rendering it would raise `ValueError`
        # from inside the function meant to classify a failure.
        pytest.param(10**5000, id="an int past the str conversion limit"),
        pytest.param("", id="empty"),
        pytest.param(" 1", id="leading space"),
        pytest.param("1\n", id="trailing newline, which $ would accept and \\Z does not"),
        pytest.param("+1", id="plus sign"),
        pytest.param("1.0", id="decimal point"),
        pytest.param(1.0, id="float"),
        pytest.param("١٢", id="non-ASCII digits"),
        pytest.param(BODY_SENTINEL, id="a message"),
        pytest.param({"code": 1}, id="a mapping"),
    ],
)
def test_a_venue_code_is_dropped_when_it_could_be_anything_else(raw: object) -> None:
    """Dropped rather than truncated or cleaned: the constructor enforces the same rule."""
    assert venue_code_of(raw) is None
    assert ExchangeAuthError(status=401, venue_code=raw).venue_code is None


def test_a_venue_code_is_carried_only_when_numeric() -> None:
    """The spec's named case: kept and dropped side by side, so neither half passes alone."""
    kept = [venue_code_of(raw) for raw in (40001, "40001", "-1")]
    dropped = [venue_code_of(raw) for raw in (True, "abc", "12345678901")]

    assert kept == ["40001", "40001", "-1"]
    assert dropped == [None, None, None]


# --------------------------------------------------------------------------------------
# Criterion 8: nothing from the body, by value and by signature
# --------------------------------------------------------------------------------------


def a_refused_401() -> ExchangeError:
    """What a provider builds from a 401 whose envelope is `{"code": ..., "msg": ...}`.

    Both fields hold the sentinel. The provider may hand `exchange_error` the code and
    nothing else, because nothing else fits through its signature -- which the test after
    next asserts. `msg` has nowhere to go.
    """
    envelope = {"code": BODY_SENTINEL, "msg": BODY_SENTINEL}
    return exchange_error(401, envelope["code"], error_map=VENUE_MAP)


def test_an_auth_error_carries_nothing_from_the_body() -> None:
    error = a_refused_401()
    rendered = every_attribute(error)

    assert type(error) is ExchangeAuthError
    assert error.venue_code is None
    assert BODY_SENTINEL not in rendered
    # The positive companion: the rendering is real and carries what it should.
    assert error.status == 401
    assert "401" in str(error)
    assert "401" in rendered


@pytest.mark.parametrize("cls", TAXONOMY, ids=lambda cls: cls.__name__)
def test_no_class_carries_a_body_value_handed_in_as_its_code(cls: ErrorClass) -> None:
    """Criterion 8 extended to all seven: a non-numeric code is dropped, not stored."""
    error = build(cls, status=400, venue_code=BODY_SENTINEL)
    rendered = every_attribute(error)

    assert BODY_SENTINEL not in rendered
    assert error.venue_code is None
    assert "400" in str(error)


@pytest.mark.parametrize("cls", REFUSALS, ids=lambda cls: cls.__name__)
def test_the_status_and_a_numeric_code_are_in_the_message(cls: ErrorClass) -> None:
    """What the message does carry, so the absences above are not satisfied by silence."""
    error = cls(status=418, venue_code="40001")

    assert "418" in str(error)
    assert "40001" in str(error)
    assert str(error).strip()


ALLOWED_REFUSAL_PARAMETERS: Final = frozenset({"status", "venue_code", "retry_after_ms"})
EXCHANGE_ERROR_PARAMETERS: Final = frozenset(
    {"status", "venue_code", "error_map", "retry_after_ms"}
)
FREE_TEXT_KINDS: Final = frozenset(
    {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
)


@pytest.mark.parametrize("cls", REFUSALS, ids=lambda cls: cls.__name__)
def test_refusal_constructors_accept_no_message(cls: ErrorClass) -> None:
    """No parameter a message, a body or a URL could be passed through, by signature.

    The allowed names are pinned by hand; `*args` and `**kwargs` are refused by kind, since
    either would accept anything. The runtime half: a positional message is a `TypeError`.
    """
    parameters = inspect.signature(cls).parameters

    assert set(parameters) <= ALLOWED_REFUSAL_PARAMETERS, sorted(parameters)
    assert {"status", "venue_code"} <= set(parameters)
    assert not {parameter.kind for parameter in parameters.values()} & FREE_TEXT_KINDS
    with pytest.raises(TypeError):
        cls(BODY_SENTINEL)  # type: ignore[call-arg, arg-type]


def test_exchange_error_accepts_no_message_either() -> None:
    parameters = inspect.signature(exchange_error).parameters

    assert set(parameters) == EXCHANGE_ERROR_PARAMETERS
    assert not {parameter.kind for parameter in parameters.values()} & FREE_TEXT_KINDS


def test_the_signature_check_can_see_a_free_text_parameter() -> None:
    """The control: the schema error does take `detail`, and the inspection finds it."""
    parameters = inspect.signature(ExchangeSchemaError).parameters

    assert "detail" in parameters
    assert not set(parameters) <= ALLOWED_REFUSAL_PARAMETERS
    assert "field price" in str(ExchangeSchemaError("field price: not a decimal string"))


def test_a_logged_auth_error_carries_nothing_from_the_body(
    production_logging: LoggingInstaller,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`logger.exception` through the real JSON pipeline, read off stdout."""
    production_logging()

    try:
        raise a_refused_401()
    except ExchangeError:
        structlog.get_logger("tests.exchanges.errors").exception("exchange_request_refused")

    written = capsys.readouterr().out

    assert_carried_something(written, marker="exchange_request_refused")
    assert "ExchangeAuthError" in written, "the traceback never reached the line"
    assert "401" in written
    assert BODY_SENTINEL not in written


# --------------------------------------------------------------------------------------
# Criterion 3's "no HTTP mocking", mechanically
# --------------------------------------------------------------------------------------

HTTP_LIBRARIES: Final = frozenset({"httpx", "respx", "httpcore", "requests", "aiohttp"})


def test_this_module_imports_no_http_library() -> None:
    """The classification is exercised with no transport at all, and this file proves it."""
    roots = imported_roots(REPO_TESTS)

    assert "portfolio" in roots, "the scan read nothing, so its silence proves nothing"
    assert not roots & HTTP_LIBRARIES, sorted(roots & HTTP_LIBRARIES)


def test_the_http_import_scan_can_fail(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text("import respx\nfrom httpx import Response\n", encoding="utf-8")

    assert imported_roots(planted) & HTTP_LIBRARIES == {"respx", "httpx"}
    # And the AST walk sees an import nested in a function, not only at module level.
    nested = tmp_path / "nested.py"
    nested.write_text("def f():\n    import httpx\n", encoding="utf-8")
    assert "httpx" in imported_roots(nested)
    assert ast.parse(nested.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------
# The remaining refusals: a status that is not a number, a key that is not a pair
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("cls", TAXONOMY, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("status", ["401", True, 401.0], ids=["str", "bool", "float"])
def test_a_status_that_is_not_an_int_is_refused(cls: ErrorClass, status: object) -> None:
    """A string status would be rendered into the message: the free text criterion 8 refuses."""
    with pytest.raises(TypeError):
        build(cls, status=status)


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(401, id="a bare status"),
        pytest.param((401,), id="a one-tuple"),
        pytest.param((401, "40001", "extra"), id="a three-tuple"),
    ],
)
def test_build_error_map_refuses_a_key_that_is_not_a_pair(key: object) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - each case is its own parametrised row
        build_error_map({key: ExchangeAuthError})  # type: ignore[dict-item]
