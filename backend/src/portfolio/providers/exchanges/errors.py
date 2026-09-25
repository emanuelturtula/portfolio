"""Why an exchange call failed: seven classes, a map from a venue's answer to one of them.

## The taxonomy sits inside the existing hierarchy, not beside it

Each class is also the `ProviderError` subclass whose meaning it shares, so "retry or not"
is answered by the hierarchy #6 built rather than by a second one:

```
ProviderError
├── ProviderUnavailableError                     (retry later)
│   ├── ExchangeUnavailableError        + ExchangeError
│   └── ProviderRateLimitedError
│       └── ExchangeRateLimitedError    + ExchangeError   .retry_after_ms
└── ProviderResponseError                        (do not retry; needs a person)
    ├── ExchangeAuthError               + ExchangeError
    │   └── ExchangeInsufficientScopeError
    ├── ExchangeInvalidRequestError     + ExchangeError
    │   └── ExchangeRetentionWindowError
    └── ExchangeSchemaError             + ExchangeError
```

`except ExchangeError` catches everything an exchange provider may raise, and `except
ProviderUnavailableError` -- the existing spelling of "transient" -- still sees an exchange
outage. A flat exchange hierarchy would have left that clause blind to one and given "retry
or not" two answers in one package.

## No constructor here takes a message, and that is the whole of criterion 8

A venue's error envelope carries a `msg` field, and `msg` is exactly the field that echoes
request parameters back -- a symbol, a time window, on a bad day a key. **It is never
carried anywhere.** Every class except `ExchangeSchemaError` builds its message from a
fixed per-class `summary`, the HTTP status and a sanitised venue code, and its constructor
has no parameter a message could be passed through. So "an auth error never includes the
response body" is a property of the type, which a call site cannot forget, rather than a
convention every raise has to remember.

`ExchangeSchemaError` is the one class with a free-text `detail`, because a parser has to
say which field was wrong. A detail names a field and a rule -- "quantity must be greater
than zero" -- and **never a value**: a fill quantity is the owner's holdings, and a value a
parser could not read is a value somebody should not have to read in a log either.

## A venue code is carried only if it cannot be anything else

`venue_code_of` keeps an `int` or a string of one to ten ASCII digits, optionally negative,
and drops everything else. A ten-digit number cannot be an API key, a signature or an
address, which is the reason for the shape; both target venues are *believed* to use
numeric codes, and that is unconfirmed until #13 and #14 read their documentation. If one
of them turns out to use an alphanumeric code, the code is dropped, classification falls
back to the status -- still safe, less specific -- and the pattern widens in that issue,
with the evidence.

## The map is data, and the lookup order is fixed

A venue declares what differs from the defaults with `build_error_map`, which refuses a
malformed entry at import rather than misclassifying in production. `classify_error` then
resolves in six steps, first match wins; steps 4 to 6 are the same for every venue, so a
venue's map only lists what it does differently. See `classify_error` for the order and the
reason for each step.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, Final

from portfolio.providers.errors import (
    ProviderError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

__all__ = [
    "STATUS_FALLBACKS",
    "ErrorKey",
    "ErrorMap",
    "ExchangeAuthError",
    "ExchangeError",
    "ExchangeInsufficientScopeError",
    "ExchangeInvalidRequestError",
    "ExchangeRateLimitedError",
    "ExchangeRetentionWindowError",
    "ExchangeSchemaError",
    "ExchangeUnavailableError",
    "build_error_map",
    "classify_error",
    "exchange_error",
    "venue_code_of",
]

_VENUE_CODE: Final = re.compile(r"\A-?[0-9]{1,10}\Z")
"""One to ten ASCII digits, optionally negative, and nothing else.

`[0-9]` rather than `\\d`, which matches every Unicode digit, and `\\A...\\Z` rather than
`^...$`, because `$` also matches before a trailing newline. Both are the difference between
the pattern this is and one that looks like it.
"""

_VENUE_CODE_LIMIT: Final = 10**10
"""The first magnitude an integer code may not reach: ten digits is the widest kept.

Applied to an `int` before it is rendered, so an enormous integer is refused by comparison
rather than by building its decimal string first -- which, past CPython's digit limit, would
raise a `ValueError` out of a function whose contract is that it never raises.
"""

_MIN_HTTP_STATUS: Final = 100
_MAX_HTTP_STATUS: Final = 599

_UNCLASSIFIED_DETAIL: Final = "no classification exists for this status and venue code"
"""The detail an `ExchangeSchemaError` built by `exchange_error` carries.

Fixed text, because the only inputs available are the status and the code, and both are
already in the message.
"""


def venue_code_of(raw: object) -> str | None:
    """The venue's error code, if it has the one shape a code is allowed to have.

    Returns the code as a string for an `int` of at most ten digits or a string of one to
    ten ASCII digits (either optionally negative), and `None` for everything else: a
    `bool` -- an `int` subclass that is never a code -- a longer number, a string with any
    other character in it, `None`, or a value of any other type.

    **The bound applies to an `int` as well as to a string.** A JSON number of twenty
    digits is not an error code in either target venue; it is far more likely to be an
    account id, and an account id is not something an exception should carry.

    A string is returned as its match -- a plain `str` -- and never normalised: Bitget is
    believed to spell its codes with leading zeros, and `"00000"` and `"0"` must stay
    different keys in an error map.

    Never raises. It is called from inside exception constructors, where a second
    exception would replace the one being built.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        if -_VENUE_CODE_LIMIT < raw < _VENUE_CODE_LIMIT:
            # `int(raw)` first, so an `int` subclass cannot render itself some other way.
            return str(int(raw))
        return None
    if isinstance(raw, str):
        match = _VENUE_CODE.match(raw)
        return match.group() if match is not None else None
    return None


class ExchangeError(ProviderError):
    """The marker base: everything an exchange provider may raise is one of these.

    Carries `status` (inherited from `ProviderError`) and `venue_code`, and nothing that
    came out of a response body except that code -- which has passed through
    `venue_code_of` here, in the constructor, so a code that reached this class by any
    route is held to the same rule as one that came through `exchange_error`.

    **The constructor has no parameter a message could be passed through.** The message is
    the class's `summary` plus `(HTTP <status>, venue code <code>)`, and a subclass changes
    what it says by overriding `summary`, which is a class attribute and not an argument.
    `ExchangeSchemaError` is the one exception, for the reason the module docstring gives.
    """

    summary: ClassVar[str] = "The exchange call failed"

    def __init__(self, *, status: int | None = None, venue_code: object = None) -> None:
        """Build the message from the class summary, the status and the sanitised code.

        Raises:
            TypeError: `status` is not an `int` -- a string here would be rendered into
                the message, which is the free text this constructor exists to refuse.
        """
        checked_status = _checked_status(status)
        self.venue_code: str | None = venue_code_of(venue_code)
        super().__init__(
            _describe(self._headline(), checked_status, self.venue_code),
            status=checked_status,
        )

    def _headline(self) -> str:
        """What the message says before the status and the code. The summary, here."""
        return type(self).summary


class ExchangeUnavailableError(ProviderUnavailableError, ExchangeError):
    """The exchange could not be reached, or answered with a 5xx or a 408. Retry later.

    Also what a transport failure becomes: the provider catches `httpx.TransportError`
    around its call and raises this, with no status, `from` the original.
    """

    summary: ClassVar[str] = "The exchange could not be reached or did not answer"


class ExchangeRateLimitedError(ProviderRateLimitedError, ExchangeError):
    """The exchange refused the request because it was asked too often.

    `retry_after_ms` is the venue's `Retry-After`, in milliseconds, as the provider parsed
    it with `providers.http.parse_retry_after` -- or `None` when the venue said nothing,
    which is not the same as `0`, "immediately". It is an attribute and not part of the
    message, because a caller acts on it and nobody should have to parse it back out.
    """

    summary: ClassVar[str] = "The exchange refused the request because it was asked too often"

    def __init__(
        self,
        *,
        status: int | None = None,
        venue_code: object = None,
        retry_after_ms: int | None = None,
    ) -> None:
        """Carry the wait the venue asked for, if it asked for one.

        Raises:
            TypeError: `status` is not an `int` (a `bool` included).
            ValueError: `retry_after_ms` is not `None` or a non-negative `int` -- a `bool`,
                a string and a negative number alike.
        """
        self.retry_after_ms: int | None = _checked_retry_after(retry_after_ms)
        super().__init__(status=status, venue_code=venue_code)


class ExchangeAuthError(ProviderResponseError, ExchangeError):
    """The exchange refused the credentials. Terminal until the owner fixes the key.

    A 401 or a 403 by default. #15 marks the account `auth_failed` for this class and for
    its subclass alike, because both need a person to act on the key and retrying either
    only produces the same refusal.
    """

    summary: ClassVar[str] = "The exchange refused the API key"


class ExchangeInsufficientScopeError(ExchangeAuthError):
    """The key was accepted, and it lacks the permission this read needs.

    A subclass of the auth error because the remedy is the same -- the owner edits the key
    -- and it is its own class so that #16 can say *which* edit ("grant read permission"
    rather than "the key was refused"). No status maps here by default: a 403 means this
    only sometimes, and a CDN block or an IP allowlist also arrive as 403, so each venue
    maps its own scope code explicitly.
    """

    summary: ClassVar[str] = "The exchange accepted the API key but it lacks read permission"


class ExchangeInvalidRequestError(ProviderResponseError, ExchangeError):
    """The exchange refused a request this application built. Needs a person.

    Every 4xx the map and the fallbacks do not place elsewhere. The same request would be
    refused again, so it is not retried; either the provider builds requests wrongly or the
    venue changed what it accepts.
    """

    summary: ClassVar[str] = "The exchange refused the request as invalid"


class ExchangeRetentionWindowError(ExchangeInvalidRequestError):
    """The exchange refused a query window older than the history it keeps.

    Separate from the generic refusal because #15 can act on it -- clamp the window
    further and try again -- where a generic refusal needs a person.
    `clamp_to_retention` is what should make this rare; a venue whose retention is not
    what its capabilities declare is how it happens anyway.
    """

    summary: ClassVar[str] = "The exchange refused a window older than the history it keeps"


class ExchangeSchemaError(ProviderResponseError, ExchangeError):
    """The exchange answered with something this application cannot interpret.

    Two routes arrive here. A parser meets a document that does not have the documented
    shape, and says which field and which rule in `detail`. Or `classify_error` falls
    through every step -- a 200 carrying a venue code nobody mapped, a 3xx -- which is an
    answer we do not understand rather than a refusal we do.

    **`detail` names a field and a rule, never a value.** It is the one piece of free text
    in this taxonomy and it is only ever written by this application, from constants:
    "quantity must be greater than zero", not the quantity.
    """

    summary: ClassVar[str] = (
        "The exchange answered with something this application cannot interpret"
    )

    def __init__(
        self,
        detail: str,
        *,
        status: int | None = None,
        venue_code: object = None,
    ) -> None:
        """Say which field of the response was wrong, and by which rule."""
        self.detail = detail
        super().__init__(status=status, venue_code=venue_code)

    def _headline(self) -> str:
        """The summary followed by the detail, so a log line says which field failed."""
        return f"{type(self).summary}: {self.detail}"


type ErrorKey = tuple[int | None, str | None]
"""`(http_status, venue_code)`, either half `None` for "any" -- never both."""

type ErrorMap = Mapping[ErrorKey, type[ExchangeError]]
"""A venue's declared classifications. Build one with `build_error_map`, never by hand."""

STATUS_FALLBACKS: Final[Mapping[int, type[ExchangeError]]] = MappingProxyType(
    {
        401: ExchangeAuthError,
        # Auth rather than scope: a CDN block and an IP allowlist both arrive as a 403 and
        # "fix the key" covers them. A venue maps its own scope code explicitly.
        403: ExchangeAuthError,
        408: ExchangeUnavailableError,
        429: ExchangeRateLimitedError,
    }
)
"""Step 4 of `classify_error`: the statuses whose meaning is the same at every venue."""


def build_error_map(entries: Mapping[ErrorKey, type[ExchangeError]]) -> ErrorMap:
    """Validate a venue's classifications once, at import, and freeze them.

    Called at module level in a provider, so a malformed entry is a `ValueError` when the
    process starts rather than a misclassified failure in production -- where "the key was
    refused" read as "the exchange is down" retries a revoked key forever.

    Refused, each naming the rule and never the offending code:

    | Entry | Why |
    |---|---|
    | a status outside 100-599, or not an `int` | it is not an HTTP status |
    | a code `venue_code_of` would change | lookup normalises through it, so it never matches |
    | the key `(None, None)` | it would match every failure |
    | not a strict `ExchangeError` subclass | the taxonomy is all a provider raises |

    The marker base itself is refused with any other type: it names no condition, and a
    failure classified as "an exchange error" has been classified as nothing.

    Returns:
        A read-only mapping. Assigning into it raises `TypeError`, so a map cannot be
        edited after the checks above have run.

    Raises:
        ValueError: any entry breaks a rule above.
    """
    validated: dict[ErrorKey, type[ExchangeError]] = {}
    for key, error_class in entries.items():
        validated[_checked_key(key)] = _checked_class(error_class)
    return MappingProxyType(validated)


def classify_error(
    status: int | None,
    venue_code: object,
    error_map: ErrorMap,
) -> type[ExchangeError]:
    """Which class a venue's `(status, code)` answer belongs to. First match wins.

    | Step | Looks up | Why it is in this position |
    |---|---|---|
    | 1 | `(status, code)` exactly | the most specific thing a venue can say |
    | 2 | `(None, code)` | a code means one thing under any status (in-band, on a 200) |
    | 3 | `(status, None)` | the venue's own reading of a status, whatever the code |
    | 4 | `STATUS_FALLBACKS` | 401/403 auth, 408 unavailable, 429 rate-limited, everywhere |
    | 5 | the status class | any other 4xx is an invalid request; any 5xx is unavailable |
    | 6 | nothing matched | a schema error, a 200 with an unmapped code included |

    Step 2 exists for venues that report an error in the body of a `200` -- BingX is
    believed to be one -- where the status says nothing and the code says everything.

    Step 6 is deliberate: an in-band code nobody mapped is an answer we do not understand,
    not a refusal we do. It fails the run loudly and is not retried, which is the
    conservative reading of a venue saying something new.

    `venue_code` is taken raw and passed through `venue_code_of`, so a code that is not
    one -- a message, an empty string, a `bool` -- takes no part in the lookup and the
    status decides.
    """
    code = venue_code_of(venue_code)
    for key in _lookup_keys(status, code):
        mapped = error_map.get(key)
        if mapped is not None:
            return mapped
    if status is None:
        return ExchangeSchemaError
    fallback = STATUS_FALLBACKS.get(status)
    if fallback is not None:
        return fallback
    if 400 <= status < 500:
        return ExchangeInvalidRequestError
    if 500 <= status < 600:
        return ExchangeUnavailableError
    return ExchangeSchemaError


def exchange_error(
    status: int | None,
    venue_code: object,
    *,
    error_map: ErrorMap,
    retry_after_ms: int | None = None,
) -> ExchangeError:
    """Classify a venue's answer and build the exception. Raise the result.

    **Takes no body, no message and no URL, by signature.** The caller has already pulled
    the status off the response and the code out of the envelope; everything else in the
    envelope stays where it was.

    `retry_after_ms` is carried only by `ExchangeRateLimitedError` and dropped for every
    other class, so a provider can pass the parsed `Retry-After` unconditionally.
    """
    error_class = classify_error(status, venue_code, error_map)
    if issubclass(error_class, ExchangeRateLimitedError):
        return error_class(status=status, venue_code=venue_code, retry_after_ms=retry_after_ms)
    if issubclass(error_class, ExchangeSchemaError):
        return error_class(_UNCLASSIFIED_DETAIL, status=status, venue_code=venue_code)
    return error_class(status=status, venue_code=venue_code)


def _lookup_keys(status: int | None, code: str | None) -> Iterator[ErrorKey]:
    """Steps 1 to 3 of `classify_error`, in order, skipping any key that has a `None` in
    the half the step is about -- `(None, None)` is never a key, and never looked up."""
    if status is not None and code is not None:
        yield (status, code)
    if code is not None:
        yield (None, code)
    if status is not None:
        yield (status, None)


def _describe(headline: str, status: int | None, venue_code: str | None) -> str:
    """`<headline> (HTTP <status>, venue code <code>).`, leaving out what is `None`."""
    context: list[str] = []
    if status is not None:
        context.append(f"HTTP {status}")
    if venue_code is not None:
        context.append(f"venue code {venue_code}")
    if not context:
        return f"{headline}."
    return f"{headline} ({', '.join(context)})."


def _checked_status(status: object) -> int | None:
    """Refuse a status that is not an `int`: it is rendered into the message.

    Takes `object` so the check is not statically dead: the annotation on the constructor
    is a promise `mypy` keeps for our code and nobody keeps for a value built at run time.
    """
    if status is None:
        return None
    if isinstance(status, bool) or not isinstance(status, int):
        message = f"An exchange error's status must be an int, got {type(status).__name__}"
        raise TypeError(message)
    return status


def _checked_retry_after(retry_after_ms: object) -> int | None:
    """Refuse a wait that is not a whole, non-negative number of milliseconds.

    One exception type for every malformed wait, a `bool` and a string included: each is
    the same mistake -- the caller skipped `parse_retry_after` or mangled its result -- and
    a caller should not have to catch two types to handle one bug.
    """
    if retry_after_ms is None:
        return None
    if (
        isinstance(retry_after_ms, bool)
        or not isinstance(retry_after_ms, int)
        or retry_after_ms < 0
    ):
        message = (
            "retry_after_ms must be None or a non-negative int number of milliseconds, "
            f"got a {type(retry_after_ms).__name__}"
        )
        raise ValueError(message)
    return retry_after_ms


def _checked_key(key: object) -> ErrorKey:
    """One error-map key, or a `ValueError` naming the rule it breaks."""
    if not isinstance(key, tuple) or len(key) != 2:
        message = "An error map key must be a (status, venue_code) pair."
        raise ValueError(message)
    status, code = key
    if status is not None and (
        isinstance(status, bool)
        or not isinstance(status, int)
        or not _MIN_HTTP_STATUS <= status <= _MAX_HTTP_STATUS
    ):
        message = (
            "An error map key's status must be None or an HTTP status between "
            f"{_MIN_HTTP_STATUS} and {_MAX_HTTP_STATUS}."
        )
        raise ValueError(message)
    if code is not None and (not isinstance(code, str) or venue_code_of(code) != code):
        message = (
            "An error map key's venue code must be None or a string of one to ten digits, "
            "optionally negative; any other code could never be matched."
        )
        raise ValueError(message)
    if status is None and code is None:
        message = "An error map key must name a status, a venue code, or both."
        raise ValueError(message)
    return (status, code)


def _checked_class(error_class: object) -> type[ExchangeError]:
    """One error-map value, or a `ValueError` if it is not in the taxonomy."""
    if (
        not isinstance(error_class, type)
        or not issubclass(error_class, ExchangeError)
        or error_class is ExchangeError
    ):
        message = (
            "An error map value must be one of the exchange error classes, "
            "not the ExchangeError base and not any other type."
        )
        raise ValueError(message)
    return error_class
