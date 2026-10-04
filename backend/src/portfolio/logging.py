"""Structured logging, with redaction of sensitive values built into the pipeline.

The redaction processors are not a nicety. This backend talks to exchanges on behalf of
its owner, so it holds API keys, API secrets and passphrases, and at least one provider
authenticates by signing the request *in the query string* -- which means a signature and
the key that produced it can ride along in a URL that any middleware, retry handler or
exception renderer might cheerfully log. Extended public keys (`xpub`/`ypub`/`zpub`) are
just as damaging in a different way: they are not spendable, but one leaked xpub exposes
every address and every balance the wallet will ever derive. An extended *private* key is
worse than either -- it spends -- and since spec 031 accepts extended keys in the field an
owner pastes into, one pasted by mistake is redacted by the same two rules.

## One pipeline for every record (#23)

Every record reaches stdout through one formatter, `structlog.stdlib.ProcessorFormatter` on
the root handler, whether structlog or the standard library wrote it:

* a structlog record runs `format_exc_info`, `redact_sensitive` and the `ValueRedactor` in
  structlog's own chain, then ends it with `ProcessorFormatter.wrap_for_formatter`, so it
  reaches the logging tree as an event dict that is **already redacted**, unrendered;
* a standard-library record -- `httpx`, `aiosqlite`, `uvicorn` -- enters through the
  formatter's `foreign_pre_chain`, which gives it the log level, the logger's name, the
  timestamp and the context variables, so a record written during a request carries its
  `request_id`;
* both then run the same processors in the formatter: `format_exc_info`, `redact_sensitive`,
  the `ValueRedactor`, and the renderer -- JSON on one line in production, the console
  renderer in development.

**Before #23 a standard-library record passed no processor at all.** It went to a
`"%(message)s"` handler and reached stdout as its preformatted string. That is how `httpx`
put a wallet address on stdout on every balance read at INFO, and how `aiosqlite` put every
bound parameter there at DEBUG (#106). Both were closed by raising the two vendors' loggers
to a floor, and this docstring called the general case open. It is now closed by mechanism:
a library nobody has looked at yet is redacted like everything else. The vendor floors
(`SILENCED_VENDOR_LOGGERS`, `VENDOR_LOG_FLOOR`) stay, as the second layer.

**No handler ever receives an unredacted structlog record** (spec 030, R7). A structlog
record travels as an event dict in `LogRecord.msg`, so the rules run in structlog's chain,
before `wrap_for_formatter`, and every handler sees `msg` redacted -- the root handler's,
pytest's `caplog`, a test's witness, one added later. The exception is rendered there too,
first, so that its traceback is a string by the time it is searched: no handler receives
`exc_info`.

**A standard-library record is the accepted residual** (spec 030, R10). It reaches every
handler as the library wrote it, and only the root handler's formatter redacts it, before it
is written to stdout. In production that is the only handler; a handler added beside it --
`caplog`, a witness -- sees `httpx`'s or `aiosqlite`'s record unredacted. A record factory or a
per-logger filter would close that for handlers nobody has added yet, at the cost of
processing every record twice.

The formatter therefore runs both rules a second time on a structlog record, and that is
harmless: each is idempotent. `redact_sensitive` replaces a value with `[REDACTED]`, which it
then replaces with itself. The `ValueRedactor` repeats its rules over each string until a pass
changes nothing (R12), so what it returns is already what one more pass would return. The one
exception is a loaded secret that is itself a piece of the marker, such as `REDACTED`: the
marker grows a bracket per pass until `MAX_REDACTION_PASSES` stops it, and nothing is revealed.

Uvicorn's loggers are routed to the same handler (`route_uvicorn_logging`), and its access
line is raised to WARNING: `request_completed`, written by `api/request_context.py`, replaces
it with the route template and never the raw path or query.

## Two redaction rules, by key and by value

**By key name** (`redact_sensitive`, unchanged): the value of any key whose name contains a
sensitive fragment is replaced, wherever it is nested. Keys are matched case-insensitively on
a substring, so `api_key`, `API_KEY` and `bitget_api_key_header` are all caught.

**By value** (`ValueRedactor`, spec 030's `redact_values`): every string in the record --
`event` and `exception` included, nested values included, and every key of every mapping
(R12), so a dict keyed by address is caught -- has four things replaced:

1. every loaded secret, as `secret_values` finds them on `Settings`, overlapping occurrences
   merged and replaced once;
2. extended keys, public and private, by pattern;
3. addresses of every form this application accepts, mainnet and testnet, by pattern;
4. the query string of any `scheme://` URL, found by a token scan, not a backtracking pattern.

The four repeat over a string until a pass changes nothing, because a replacement can create
the separator an earlier rule needed (R12). **Every rule is linear in the string** (R13): a
client without a session chooses the path `request_refused` logs, so a rule that could rescan
a run would let it hold the event loop. The URL rule used to: a 200 KB path cost 12 to 27 s.

A value that is not a string, a number, a boolean or `None` is rendered with `repr` first,
which is what both renderers would have printed for it, so an object bound as a value cannot
carry a secret past the rule inside its `repr`.

## What is still printed

**A value that is none of the above.** An exception message carrying an arbitrary row value
-- a trade id, an amount, an asset, a label -- is printed as it is, and so is a password hash
among a statement's parameters, which is one reason the `aiosqlite` floor stays. A secret is
caught in its own spelling, in its JSON-escaped spelling and in its `repr` spelling; any
other encoding of it (percent-encoded, base64) is not. A URL without a scheme -- a bare path
with a query -- keeps its query. The patterns verify no checksum, so they err toward
redacting: a false positive costs a word of a log line, and a false negative the owner's
holdings.

This is defence in depth, not the primary control: credentials belong in `SecretStr`
fields so they never reach a log statement in the first place.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sys
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Final, TextIO, override

import structlog
from pydantic import SecretStr

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
# whole derivation tree, and an extended private key spends it, so a field named after
# either is redacted wholesale. The private six arrived with spec 031; matched without case,
# they cover the four uppercase multisig spellings as well.
EXTENDED_KEY_PREFIXES: Final[tuple[str, ...]] = (
    "xpub",
    "ypub",
    "zpub",
    "xprv",
    "yprv",
    "zprv",
    "tprv",
    "uprv",
    "vprv",
)

MAX_REDACTION_PASSES: Final = 4
"""The most passes `ValueRedactor.redact_text` makes over one string (R12).

It repeats the rules until a pass changes nothing. A replacement can expose the start of an
address to a rule that has already passed that point -- `re.sub` reads its anchors in the
string as it was before the pass -- and one chain does: a bech32 or Base58 address glued
behind a Kaspa one, taken on the second pass. It goes no further. A glued run of Kaspa
addresses is one match (`KASPA_ADDRESS_RUN`, R16), and so is a glued run of bech32 addresses
whose prefix starts with `b` (R18), which used to lose one address per pass and print the
fifth; a Base58 address glued to what follows it is never one.

So **at most two passes change a string and the third finds nothing**, measured (R16, R18)
over every arrangement of up to three Kaspa, `tb1`, `bcrt1` and Base58 addresses, extended
keys, secrets and URLs, glued or separated, and 20,000 random ones of four to seven. Four is a
guard, not a budget. The one exception is a loaded secret that is a piece of the marker, which
the bound is what stops (`ValueRedactor.redact_text`).
"""

MIN_SUBSTRING_SECRET_LENGTH: Final = 8
"""The shortest secret replaced wherever it occurs inside a string.

A shorter one is replaced only where it is the whole string: a three-character secret matched
as a substring would redact ordinary words, and a log that redacts half its words is one
nobody reads.
"""

EXTENDED_PUBLIC_KEY_PREFIXES: Final[tuple[str, ...]] = (
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
"""The extended public key prefixes the value rule redacts.

The six single-signature prefixes `domain/extended_keys.py` parses, and the four multisig
forms it refuses. Matched case-sensitively, because the case is part of the prefix: `Ypub`
and `ypub` are different version bytes.
"""

EXTENDED_PRIVATE_KEY_PREFIXES: Final[tuple[str, ...]] = (
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
"""The extended private key prefixes the value rule redacts as well (spec 031).

SLIP-0132's ten private counterparts of the ten above, mainnet and test alike. Registration
refuses every one of them by its prefix, before decoding a character, so none should ever
reach a log. This is for the one that does anyway -- a request body echoed by a library, a
traceback with the form in scope -- because the cost of that one is the owner's funds.
"""

_BASE58: Final = "1-9A-HJ-NP-Za-km-z"
"""The Base58 alphabet as a character-class body: no `0`, `O`, `I` or `l`."""

_BECH32: Final = "ac-hj-np-z02-9"
"""The bech32 alphabet as a character-class body: no `1`, `b`, `i` or `o`."""

EXTENDED_KEY_PATTERN: Final = re.compile(
    "(?:"
    + "|".join((*EXTENDED_PUBLIC_KEY_PREFIXES, *EXTENDED_PRIVATE_KEY_PREFIXES))
    + f")[{_BASE58}]{{100,}}"
)
"""A public or private prefix followed by 100 or more Base58 characters.

A real key, of either kind, is 111 characters long. Renamed from `EXTENDED_PUBLIC_KEY_PATTERN`
when spec 031 widened it, so that the name does not promise less than the pattern redacts.
"""

ADDRESS_STARTS: Final = r"(?<![0-9A-Za-z])"
"""Where an address may start: no letter or digit right before it (R14).

Not `\\b`, which counts `_` as part of a word: `wallet_<address>` and
`snapshot_<address>.json` -- a hurried f-string's favourite shapes -- were never redacted.
"""

ADDRESS_ENDS: Final = r"(?![0-9A-Za-z])"
"""Where a Base58 address must end: no letter or digit right after it (R14)."""

_KASPA_PREFIX: Final = "(?:kaspa|kaspatest|kaspasim|kaspadev):"
"""The four Kaspa network prefixes and the colon, as a group."""

_KASPA_PAYLOAD: Final = f"[{_BECH32}]{{61,63}}"
"""A Kaspa payload: 61 characters for a Schnorr key or a script hash, 63 for an ECDSA key."""

_OTHER_ADDRESS_START: Final = "(?:bc|tb|bcrt)1|(?-i:[13mn2])"
"""How a bech32 or a Base58 address begins, for where a glued Kaspa payload stops.

The Base58 start is case-sensitive inside a rule that ignores case (R19): `M` and `N` start no
Base58 address, and read as `m` and `n` they cut a payload one character short in front of
the `M` of a glued `2M...`.
"""

KASPA_ADDRESS_RUN: Final = (
    rf"{ADDRESS_STARTS}(?:{_KASPA_PREFIX}(?:"
    rf"{_KASPA_PAYLOAD}?(?={_KASPA_PREFIX})"
    rf"|{_KASPA_PAYLOAD}{ADDRESS_ENDS}"
    rf"|{_KASPA_PAYLOAD}(?={_OTHER_ADDRESS_START})"
    rf"|{_KASPA_PAYLOAD}"
    rf"))+"
)
"""One Kaspa address, or a run of them glued together, as one match (R16).

The payload's alphabet holds the first letters of `kaspa`, `tb1` and a Base58 address, so a
payload glued to what follows cannot be cut by its alphabet alone: a 61-character one -- the
common length -- took the next address's first characters with it, and that address was
printed. So each address in the run takes the first of these that fits:

1. a length that another Kaspa prefix follows, which continues the run;
2. the longest that no letter or digit follows: the address ends at a separator;
3. the longest that the start of a bech32 or a Base58 address follows, left for its own rule
   to take on the next pass. **Longest** (R19): the shortest stopped a 63-character payload
   whose 62nd character is `2` -- the `KASPA_TESTNET_V1_ZERO` vector -- at 61, and left `2d`
   glued in front of the next address, which was then printed whole on every pass;
4. the longest, glued to something else.

**A heuristic, and the last one** (R19); further glued-address cases are documented, not
fixed. Behind a 63-character payload, case 3 stops exactly where the next address starts.
Behind a 61-character one it tries the two longer lengths first, so when the next address's
second or third character is itself an address start -- the `n` of a signet `mfn...` -- the
payload takes the one or two characters before it, and the rest of that address, which
begins with the start, is taken on the next pass. Measured: 5,000 random addresses of each
form -- `m`, `n` and `2` Base58, `tb1`, `bcrt1` -- behind random payloads of each length all
end fully redacted, but for 18 of 200,000 random Base58 addresses behind a 61-character
payload. Those hold, at their second or third character, what reads as a bech32 start in
either case (`tB1`, `bC1`); what remains after it is no whole address of any form, so all or
part of it is printed.

Linear: each address tries a bounded number of bounded lengths, the run never revisits an
address it has passed, and a prefix starts nowhere else.
"""

ADDRESS_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # Bech32 and bech32m, human-readable part `bc`, `tb` or `bcrt`, either case. Eleven data
    # characters is the shortest a witness address can be: a version, a two-byte program and
    # the six-character checksum. One address, or a glued run of them as one match (R18): `b`
    # is outside the alphabet, so a data part stops in front of a `bc1` or `bcrt1` glued to
    # it, and the run carries on from there rather than leaving it to the next pass.
    re.compile(rf"{ADDRESS_STARTS}(?:(?:bc|tb|bcrt)1[{_BECH32}]{{11,}})+", re.IGNORECASE),
    # Base58Check P2PKH and P2SH, mainnet and testnet: `1`, `3`, `m`, `n` or `2`, then 25 to
    # 34 Base58 characters, with no letter or digit on either side.
    re.compile(rf"{ADDRESS_STARTS}[13mn2][{_BASE58}]{{25,34}}{ADDRESS_ENDS}"),
    # Kaspa: the four network prefixes, a colon, then 61 to 63 characters of its alphabet --
    # one address, or a run of them glued together (`KASPA_ADDRESS_RUN`).
    re.compile(KASPA_ADDRESS_RUN, re.IGNORECASE),
)
"""Every address form this application accepts, mainnet and testnet alike.

No checksum is verified, so these err toward redacting: a false positive costs a word of a
log line. `kaspasim` is here although registration refuses it: an address is redacted
whether or not it could be registered. A space, an underscore, a hyphen, a slash or any other
punctuation separates an address from what is around it.

**Not recognised, measured** (accepted, R13, R14 and R17): an address glued to a letter or a
digit, such as `x<address>`; and parts of two addresses glued together:

* **after a bech32 address**, its rule takes every following character of the bech32
  alphabet. A second address that starts with one -- the `t` of `tb1`, the `k` of a Kaspa
  prefix, a Base58 `m`, `n`, `2` or `3` -- loses its first characters to that match, up to
  the first character outside the alphabet, and what follows is printed -- for four in five
  random Base58 addresses, measured; the rest are taken whole by chance. One that starts
  outside it does not: the match stops in front of it. A `bcrt1` one -- `b` or `B` -- is then
  the next address of the same match, however long the glued run (R18), and a Base58 `1` one
  is redacted whole on the next pass;
* **after a Base58 address** both are printed, since the Base58 one cannot end before a
  letter or digit and the second cannot start after one.

After a Kaspa address of either length the second is redacted, measured for every form (R16,
R19): another Kaspa address in the same match, a bech32 or Base58 one on the next pass (R12).
The exception is about one Base58 address in ten thousand behind a 61-character payload, which
`KASPA_ADDRESS_RUN` describes.

**Linear, measured** (R13, R16, R18): none of the three can rescan a run. The anchors are
one-character lookarounds, every repetition is a single character class, the Base58 and Kaspa
lengths are bounded, and the Kaspa rule's lookaheads read at most ten characters. The two run
rules nest a repetition inside `+`, but nothing follows the `+`: when one more address does
not match, the run ends there and the match succeeds, so the engine never goes back into an
address it has passed. Measured on the shapes that fail -- a glued run ending in a letter or a
digit, a run broken every few hundred characters, `tb1` and a long data part ending in `b`,
glued Kaspa payloads of 60 and 64 characters -- a 200 KB string costs about 1.5 ms per pattern
and 400 KB twice that.
"""

URL_TOKEN_PATTERN: Final = re.compile(r"""(?<![^\s"'])[^\s"']*?://[^\s"']*""")
"""A token -- a run with no whitespace and no quote -- that holds `://` (R13).

It starts only where a token starts, so it is tried once per token, and its two repetitions
are single character classes that a separator ends: one pass over the string, never a rescan.
No word boundary, so `fetch_https://...?sign=...` is a token like any other (S2).
"""

REQUEST_ID_KEY: Final = "request_id"
"""The context variable `api/request_context.py` binds to every record a request writes.

Its value is redacted like every other (spec 030, R5): nothing is exempt. A hyphenated UUID
has no run of characters long enough for any pattern above to match.
"""

UVICORN_LOGGERS: Final[tuple[str, ...]] = ("uvicorn", "uvicorn.error", "uvicorn.access")
"""The loggers uvicorn configures with handlers of its own, before the application exists."""

UVICORN_ACCESS_LOGGER: Final = "uvicorn.access"
"""Raised to WARNING: `request_completed` replaces its line, without the raw path or query."""

UVICORN_ACCESS_FLOOR: Final = logging.WARNING
"""The level `route_uvicorn_logging` gives `UVICORN_ACCESS_LOGGER`."""

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

STATEMENT_LOGGING_LIBRARIES: Final[tuple[str, ...]] = ("aiosqlite",)
"""Standard-library loggers that render a statement's bound parameters, silenced likewise.

`aiosqlite` is the measured leak (#106). The worker thread behind every connection calls
`LOG.debug("executing %s", function)` for each call it makes on that connection, and for a
statement `function` is `functools.partial(cursor.execute, sql, parameters)` -- whose `repr`
is the SQL text followed by the tuple of values. At DEBUG that is every INSERT and UPDATE the
application writes, on stdout, values and all -- the owner's password hash among them.

Verified in aiosqlite 0.22.1: the package logs through this one logger and no child of it.
Three calls are at DEBUG -- the call, its completion, and the exception it raised -- and the
first two render the partial. The other three carry no row: an INFO when closing a connection
fails, which the floor also drops and whose exception is re-raised anyway, and an ERROR and a
WARNING from `iterdump`, which the application never calls.

`hide_parameters=True` in `db/engine.py` does not cover this and cannot. It decides what
*SQLAlchemy* renders into its own log lines and exception messages; this record is written
underneath SQLAlchemy, by the driver SQLAlchemy calls, through a logger of the driver's own.
"""

SILENCED_VENDOR_LOGGERS: Final[tuple[str, ...]] = (
    URL_LOGGING_LIBRARIES + STATEMENT_LOGGING_LIBRARIES
)
"""Every standard-library logger `silence_vendor_logging` raises to `VENDOR_LOG_FLOOR`.

Two lists joined rather than one, because why a logger is here is what somebody needs to find
before taking it off: a URL and a bound parameter leak for different reasons, from different
packages, and each list's docstring records the version its claim was verified against.
"""

VENDOR_LOG_FLOOR: Final = logging.WARNING
"""An absolute floor for those loggers, not a maximum against `settings.log_level`.

Deliberately not `max(level, WARNING)`. Turning the application's own logging up to DEBUG
to investigate a provider or a sync must not be the act that puts every wallet address on
stdout -- which is exactly when someone would be tailing it, and exactly when a copy would
end up pasted into an issue.

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


def secret_values(settings: Settings) -> frozenset[str]:
    """Every non-empty `SecretStr` value on `settings`, unwrapped.

    The fields are found by walking the model's fields, not from a list, so a `SecretStr`
    setting added later is covered without an edit here. The set goes to a `ValueRedactor`
    and is never logged, returned or written anywhere.
    """
    values: set[str] = set()
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if isinstance(value, SecretStr):
            secret = value.get_secret_value()
            if secret:
                values.add(secret)
    return frozenset(values)


def _spellings(secret: str) -> set[str]:
    """A secret as it is, and as a JSON string or a `repr` would escape it.

    An exception message or a rendered value can carry a secret escaped rather than raw -- a
    backslash doubled, a quote escaped, a non-ASCII character as a `\\u` escape -- and the
    substring rule would miss it in that spelling.
    """
    return {secret, json.dumps(secret)[1:-1], repr(secret)[1:-1]}


class ValueRedactor:
    """The value rule: a structlog processor that redacts inside every string of a record.

    `configure_logging` builds one with `secret_values(settings)` on every call, so a second
    call replaces the set. In each string, in this order:

    1. every secret of `MIN_SUBSTRING_SECRET_LENGTH` characters or more wherever it occurs,
       and a shorter one where it is the whole string. Every occurrence of every spelling is
       found as a span, and spans that overlap are merged and replaced once (R13), so neither
       one secret inside another nor two that overlap leaves a fragment;
    2. `EXTENDED_KEY_PATTERN`, public and private;
    3. `ADDRESS_PATTERNS`;
    4. `redact_url_queries`, keeping each URL's base and fragment and replacing its query.

    Each replacement is `REDACTED`, and the four repeat until a pass changes nothing, at most
    `MAX_REDACTION_PASSES` times. It walks mappings, lists, tuples and sets as
    `redact_sensitive` does, and applies the same rule to every key of every mapping, the event
    dict's own included (`redact_key`). It returns a new event dict and never mutates the one
    it was given.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        """Hold the secrets: the short ones for an exact match, the long ones' spellings."""
        given = frozenset(secrets)
        self._short_secrets = frozenset(
            secret for secret in given if 0 < len(secret) < MIN_SUBSTRING_SECRET_LENGTH
        )
        self._spellings = tuple(
            sorted(
                {
                    spelling
                    for secret in given
                    if len(secret) >= MIN_SUBSTRING_SECRET_LENGTH
                    for spelling in _spellings(secret)
                }
            )
        )

    def redact_text(self, text: str) -> str:
        """`text` with every secret, extended key, address and URL query replaced.

        The rules repeat until a pass changes nothing (R12): a replacement can create the
        separator an earlier rule needed -- a Kaspa address directly followed by a Base58 one
        leaves the second whole after one pass, because the Base58 rule runs first and finds a
        letter in front of it. `REDACTED` matches no pattern, so the repetition ends -- unless
        a loaded secret of `MIN_SUBSTRING_SECRET_LENGTH` characters or more is a piece of the
        marker, such as `REDACTED`. Every pass then finds that secret inside the marker it just
        wrote and wraps it in one more pair of brackets, and `MAX_REDACTION_PASSES` is what
        stops it (R17); nothing is revealed. A string with nothing to redact costs one pass,
        as before.
        """
        for _ in range(MAX_REDACTION_PASSES):
            redacted = self._redact_once(text)
            if redacted == text:
                break
            text = redacted
        return text

    def _redact_once(self, text: str) -> str:
        """One pass of the rules, in the order the class docstring gives."""
        if text in self._short_secrets:
            return REDACTED
        text = self._redact_secrets(text)
        text = EXTENDED_KEY_PATTERN.sub(REDACTED, text)
        for pattern in ADDRESS_PATTERNS:
            text = pattern.sub(REDACTED, text)
        return redact_url_queries(text)

    def _redact_secrets(self, text: str) -> str:
        """`text` with every occurrence of every long secret's spelling replaced (R13).

        Each occurrence is a span, overlapping occurrences of one spelling included; the spans
        are sorted, those that overlap are merged, and each merged span becomes one
        `REDACTED`. Spans that only touch stay apart, so a secret twice in a row is two
        markers. One replacement per secret would leave a fragment wherever two secrets
        overlap: `XXXXYYYYZZ` and `YYYYZZZZWW` in `XXXXYYYYZZZZWW` left `[REDACTED]ZZWW`.
        `str.find` is linear in the string, and a string holding no secret -- the common case,
        and every case an outsider can construct without knowing one -- costs one scan per
        spelling and nothing else.
        """
        spans: list[tuple[int, int]] = []
        for spelling in self._spellings:
            start = text.find(spelling)
            while start != -1:
                spans.append((start, start + len(spelling)))
                start = text.find(spelling, start + 1)
        if not spans:
            return text
        spans.sort()
        pieces: list[str] = []
        copied = 0
        merged_start, merged_end = spans[0]
        for start, end in spans[1:]:
            if start < merged_end:
                merged_end = max(merged_end, end)
                continue
            pieces += [text[copied:merged_start], REDACTED]
            copied = merged_end
            merged_start, merged_end = start, end
        pieces += [text[copied:merged_start], REDACTED, text[merged_end:]]
        return "".join(pieces)

    def redact(self, value: object) -> object:
        """`value` with the rule applied to every string in it, keys included, recursively."""
        if isinstance(value, str):
            return self.redact_text(value)
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, Mapping):
            return {self.redact_key(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [self.redact(item) for item in value]
        return self.redact_text(_safe_repr(value))

    def redact_key(self, key: object) -> object:
        """A mapping's key with the rule applied, as `redact` applies it to a value (R12).

        A dict keyed by address -- `balances={<address>: "0.5"}` -- is the likely shape of the
        hurried log line the address rule exists for. A key that is not a string, a number, a
        boolean or `None` is rendered with `repr`, as a value is, and kept hashable. Two keys
        that both become `REDACTED` collapse into one entry, the later value winning: losing an
        entry of a log line is the price of not printing the address.
        """
        if isinstance(key, str):
            return self.redact_text(key)
        if key is None or isinstance(key, bool | int | float):
            return key
        return self.redact_text(_safe_repr(key))

    def __call__(
        self,
        _logger: WrappedLogger,
        _method_name: str,
        event_dict: EventDict,
    ) -> EventDict:
        """Redact every key and every value of the record. Nothing is exempt.

        The record's own keys are strings -- keyword arguments and context variable names --
        so they go straight to `redact_text`; a nested mapping's go through `redact_key`.
        """
        return {self.redact_text(key): self.redact(value) for key, value in event_dict.items()}


_SCHEME_SEPARATOR: Final = "://"


def redact_url_queries(text: str) -> str:
    """`text` with the query of every URL in it replaced (R13, M1 and S2).

    The string is read as tokens, runs with no whitespace and no quote. In a token holding
    `://` and, after it and before any `#`, a `?`, everything from that `?` to the `#` or the
    token's end becomes `?[REDACTED]`; the scheme, the host, the path and the fragment stay.
    One exchange signs its requests in the query, and this is the mechanism the vendor floor
    on `httpx` was standing in for.

    **Linear.** It replaced a regex that rescanned a run from every word boundary in it: a
    client with no session chooses the path `request_refused` logs, and 200 KB of `a.` cost
    12 s on the event loop, `a://` 27 s. Now `URL_TOKEN_PATTERN` is tried once per token and
    `str.find` does the rest; a string without `://` is not scanned for tokens at all.
    Over-reading is accepted: a second URL glued into the first's query, with no separator,
    is redacted along with it.

    **Printed, measured** (R16). Four forms keep all or part of their query:

    * a quote inside the query ends the token, so what follows the quote is printed;
    * an unencoded `#` inside the query is read as the start of the fragment, so what follows
      it is printed;
    * a `?` inside the fragment is not a query, so what follows it is printed;
    * a URL whose slashes are JSON-escaped, `https:\\/\\/host\\/path?query`, holds no `://`,
      so its query is printed whole.
    """
    if _SCHEME_SEPARATOR not in text:
        return text
    return URL_TOKEN_PATTERN.sub(_redacted_token_query, text)


def _redacted_token_query(match: re.Match[str]) -> str:
    """One token with the query of each URL in it replaced. A function, so the marker is
    never read as a template.

    A URL's query is its first `?` after `://` and before the first `#`; a `?` inside the
    fragment is not one, and is kept. A fragment holding `://` is read as a URL in turn, so a
    second URL glued after a `#` loses its query too. Every `find` starts where the last one
    left off, and nothing is sliced until the end, so the token is read once.
    """
    token = match.group()
    pieces: list[str] = []
    copied = 0
    scheme = token.find(_SCHEME_SEPARATOR)
    while scheme != -1:
        after = scheme + len(_SCHEME_SEPARATOR)
        fragment = token.find("#", after)
        end = len(token) if fragment == -1 else fragment
        query = token.find("?", after, end)
        if query != -1:
            pieces += [token[copied:query], "?", REDACTED]
            copied = end
        if fragment == -1:
            break
        scheme = token.find(_SCHEME_SEPARATOR, fragment)
    pieces.append(token[copied:])
    return "".join(pieces)


def _safe_repr(value: object) -> str:
    """`repr(value)`, or a placeholder naming its type when that `repr` raises."""
    try:
        return repr(value)
    except Exception:  # a broken __repr__ must not lose the whole record
        return f"<{type(value).__name__} whose repr failed>"


HANDLER_ERROR_LINE: Final = (
    "--- Logging error: {error_type} in a record from logger {logger}; "
    "the record was not written ---\n"
)
"""What `SafeStreamHandler` writes to stderr when handling a record raises. Nothing else."""


class SafeStreamHandler(logging.StreamHandler[TextIO]):
    """The root handler: a `StreamHandler` whose error report never prints a record (R13, S3).

    When formatting or writing a record raises, `logging.Handler.handleError` prints the
    traceback, then the record's raw message and its arguments, to stderr: every rule in this
    module bypassed, on a stream nobody redacts. This one writes `HANDLER_ERROR_LINE` instead,
    with the exception's type and the logger's name. Not the exception's message, which can
    quote the record; not the traceback, whose frames hold it. `logging.raiseExceptions` is
    honoured as the standard library honours it.
    """

    @override
    def handleError(self, record: logging.LogRecord) -> None:
        """Report that `record` could not be written, without any of its content."""
        if not logging.raiseExceptions:
            return
        error_type = sys.exc_info()[0]
        line = HANDLER_ERROR_LINE.format(
            error_type=error_type.__name__ if error_type is not None else "an error",
            logger=record.name,
        )
        # Reporting must not raise in turn: stderr can be closed, or `None` under pythonw.
        with contextlib.suppress(Exception):
            sys.stderr.write(line)
            sys.stderr.flush()


def silence_vendor_logging() -> None:
    """Raise every logger in `SILENCED_VENDOR_LOGGERS` to `VENDOR_LOG_FLOOR`.

    Called from `configure_logging`, which is the one place that decides what may reach
    stdout, so the rule applies to the whole process rather than to whichever factory
    remembered to ask for it. A named function rather than two lines inline so that the
    reasoning has somewhere to live and an entry point can call it on its own.

    Sets the level on the logger rather than adding a filter to the handler: a filter on
    the root handler would be removed by the next `logging.basicConfig(force=True)`, and
    fixing the leak with something a later call silently undoes is worse than not fixing
    it, because the gate would stay green.
    """
    for library in SILENCED_VENDOR_LOGGERS:
        logging.getLogger(library).setLevel(VENDOR_LOG_FLOOR)


def route_uvicorn_logging() -> None:
    """Send uvicorn's records through the root handler, and drop its access line.

    Uvicorn configures its loggers before it imports the application, with handlers of its
    own and `propagate = False`, so its records would reach stdout without passing a single
    processor. Clearing the handlers and turning propagation on hands them to the root
    handler and its formatter. `create_app` runs after uvicorn's own configuration -- under
    the Dockerfile's `--factory` command and under `--reload` alike -- so this is the last
    word.

    `uvicorn.access` is raised to `UVICORN_ACCESS_FLOOR`: its line carries the raw path and
    query, and `request_completed` replaces it.
    """
    for name in UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger(UVICORN_ACCESS_LOGGER).setLevel(UVICORN_ACCESS_FLOOR)


def configure_logging(settings: Settings) -> None:
    """One pipeline for every record: JSON on one line in production, console in dev.

    See the module docstring for its shape. The secret set is built here, once per call, and
    handed to the one `ValueRedactor` both chains run.
    """
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.environment == "prod"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    value_redactor = ValueRedactor(secret_values(settings))
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.stdlib.add_logger_name,
            timestamper,
        ],
        processors=[
            # First: `_record` is a `LogRecord`, and nothing after this needs it.
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            # Before the redaction, so a traceback is a string by the time it is searched,
            # and before the renderer, so no renderer ever walks a frame's locals. A
            # structlog record arrives with its exception already rendered, so for it this
            # and the two rules after it are a second, idempotent pass.
            structlog.processors.format_exc_info,
            redact_sensitive,
            value_redactor,
            renderer,
        ],
    )
    handler = SafeStreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logging.basicConfig(handlers=[handler], level=level, force=True)
    # After `basicConfig`, which installs the root handler these records propagate to.
    # `force=True` resets the root logger's handlers and does not touch a named logger's
    # level, so the order is not load-bearing -- but reading it in the order the records
    # travel is.
    silence_vendor_logging()
    route_uvicorn_logging()

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        # Here as well as in the formatter (R7), so that the event dict that becomes
        # `LogRecord.msg` is redacted before any handler receives it, not only before the
        # root handler renders it. The exception first: `ValueRedactor` would turn an
        # `exc_info` exception into its `repr` and lose the traceback.
        structlog.processors.format_exc_info,
        redact_sensitive,
        value_redactor,
        # Last: the event dict goes to the logging tree unrendered; the root handler's
        # formatter renders it.
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
