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


def configure_logging(settings: Settings) -> None:
    """Configure structlog: JSON on one line in production, readable console in dev."""
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)

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
