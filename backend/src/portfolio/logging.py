"""Structured logging, with redaction of sensitive values built into the pipeline.

The redaction processor is not a nicety. This backend talks to exchanges on behalf of
its owner, so it holds API keys, API secrets and passphrases, and at least one provider
authenticates by signing the request *in the query string* -- which means a signature and
the key that produced it can ride along in a URL that any middleware, retry handler or
exception renderer might cheerfully log. Extended public keys (`xpub`/`ypub`/`zpub`) are
just as damaging in a different way: they are not spendable, but one leaked xpub exposes
every address and every balance the wallet will ever derive.

Redaction happens by *key name*, immediately before rendering, so it applies to whatever
a call site bound, whatever earlier processors added, and whatever is nested inside a
dict or a list. Keys are matched case-insensitively on a substring, so `api_key`,
`API_KEY` and `bitget_api_key_header` are all caught by the same rule.

This is defence in depth, not the primary control: credentials belong in `SecretStr`
fields so they never reach a log statement in the first place.

## The second control, and why the first one cannot do its job alone

`redact_sensitive` is a **structlog processor**, so it only ever sees records that went
through structlog's chain. A third-party library logging through the standard library does
not: its record goes to the root handler `configure_logging` installs, renders through
`"%(message)s"`, and reaches stdout without passing a single processor. Matching on key
names would not have helped even if it had -- the record carries one preformatted string.

That is not hypothetical. `httpx` logs every request it makes at **INFO**, with the full
URL, from `logger.info("HTTP Request: %s %s ...")` in `httpx/_client.py`. At production
defaults that put an owner's wallet address on stdout on every balance read, and would
have put an exchange's query-string signature there too -- the two disclosures rule 3
exists to prevent, arriving through a door the redaction processor does not watch.
`providers/http.py` scrubs what *it* logs and is powerless here, because
`AsyncClient.send` sits above the transport.

`URL_LOGGING_LIBRARIES` closes it, and the honest description of it is written here rather
than discovered later: **it is a named list, not a mechanism.** Any library that logs a URL
through the standard library bypasses `redact_sensitive` entirely, and silencing two
loggers closes today's leak without closing that hole. The general case is a separate
issue.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from structlog.typing import EventDict, Processor, WrappedLogger

    from portfolio.config import Settings

REDACTED: Final = "[REDACTED]"

# Matched as a case-insensitive substring of the key.
SENSITIVE_KEY_FRAGMENTS: Final[tuple[str, ...]] = (
    "secret",
    "passphrase",
    "api_key",
    "apikey",
    "token",
    "authorization",
    "signature",
    "password",
    # A wallet address is the owner's holdings in one string: anyone who has it can read
    # every balance and every transaction that address has ever been part of, forever.
    # Because the match is on a substring, this one entry also covers `address_canonical`,
    # `address_display` and `wallet_address` -- and, deliberately, `email_address`.
    #
    # This is the backstop, not the control. Nothing in the wallet registry logs an
    # address in the first place; a test walks the code to keep it that way. The fragment
    # is here for the log statement somebody adds in a hurry two years from now.
    "address",
)

# Matched as a case-insensitive prefix of the key: an extended public key leaks the
# whole derivation tree, so a field named after one is redacted wholesale.
EXTENDED_KEY_PREFIXES: Final[tuple[str, ...]] = ("xpub", "ypub", "zpub")

URL_LOGGING_LIBRARIES: Final[tuple[str, ...]] = ("httpx", "httpcore")
"""Standard-library loggers that render a URL, silenced below `VENDOR_LOG_FLOOR`.

`httpx` is the measured leak. Its `AsyncClient.send` calls
`logger.info('HTTP Request: %s %s "%s %d %s"', request.method, request.url, ...)`, which at
production defaults writes the path -- and therefore the wallet address, since both target
chains put it there -- and the query string -- and therefore an exchange signature -- onto
stdout for every request. Verified in httpx 0.28.1: those are the only two `logger` calls
in the package, both at INFO, so a WARNING floor removes the leak entirely.

`httpcore` is precautionary and I could not exercise it, which is worth saying plainly
rather than implying a test that does not exist: it logs only through `Trace`, only at
DEBUG, and `httpx.MockTransport` bypasses httpcore altogether, so nothing in the suite
reaches that code. It is listed because a connection-level logger is one we never want on
stdout, and because `Trace.__init__` asks `isEnabledFor(DEBUG)` before formatting anything,
so the floor also spares the work.

Naming the parent is enough for `httpcore.connection`, `httpcore.http11` and their three
siblings: none of them sets its own level, so each inherits its effective level from this
one. That would stop being true if httpcore ever called `setLevel` on a child.
"""

VENDOR_LOG_FLOOR: Final = logging.WARNING
"""An absolute floor for those loggers, not a maximum against `settings.log_level`.

Deliberately not `max(level, WARNING)`. Turning the application's own logging up to DEBUG
to investigate a provider must not be the act that puts every wallet address on stdout --
which is exactly when someone would be tailing it, and exactly when a copy would end up
pasted into an issue.

WARNING rather than silencing the loggers outright, because `httpx` has no URL-bearing call
above INFO and a genuine warning from it is worth hearing. Our own transport already logs
an attempt, a status and a scrubbed target for every request, so nothing diagnostic is
lost by dropping its INFO line.
"""


def is_sensitive_key(key: str) -> bool:
    """Return whether a log field name must never have its value rendered."""
    # Header-style names arrive as `X-API-KEY`; normalising the separators means one
    # fragment list covers `api_key`, `api-key` and `api key` alike.
    lowered = key.casefold().replace("-", "_").replace(" ", "_")
    return lowered.startswith(EXTENDED_KEY_PREFIXES) or any(
        fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS
    )


def _redact_value(value: object) -> object:
    """Recurse into containers so a secret cannot hide one level down."""
    if isinstance(value, Mapping):
        return {key: _redact_item(key, item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_redact_value(item) for item in value]
    return value


def _redact_item(key: object, value: object) -> object:
    if isinstance(key, str) and is_sensitive_key(key):
        return REDACTED
    return _redact_value(value)


def redact_sensitive(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """structlog processor that replaces the value of every sensitive key."""
    return {key: _redact_item(key, value) for key, value in event_dict.items()}


def silence_vendor_url_logging() -> None:
    """Raise every logger in `URL_LOGGING_LIBRARIES` to `VENDOR_LOG_FLOOR`.

    Called from `configure_logging`, which is the one place that decides what may reach
    stdout, so the rule applies to the whole process rather than to whichever factory
    remembered to ask for it. A named function rather than two lines inline so that the
    reasoning has somewhere to live and an entry point can call it on its own.

    Sets the level on the logger rather than adding a filter to the handler: a filter on
    the root handler would be removed by the next `logging.basicConfig(force=True)`, and
    fixing the leak with something a later call silently undoes is worse than not fixing
    it, because the gate would stay green.
    """
    for library in URL_LOGGING_LIBRARIES:
        logging.getLogger(library).setLevel(VENDOR_LOG_FLOOR)


def configure_logging(settings: Settings) -> None:
    """Configure structlog: JSON on one line in production, readable console in dev."""
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    # After `basicConfig`, which installs the root handler these records would otherwise
    # propagate to. `force=True` resets the root logger's handlers and does not touch a
    # named logger's level, so the order is not load-bearing -- but reading it in the
    # order the records travel is.
    silence_vendor_url_logging()

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.environment == "prod"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Last before the renderer: everything any earlier processor added is covered.
        redact_sensitive,
        renderer,
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
