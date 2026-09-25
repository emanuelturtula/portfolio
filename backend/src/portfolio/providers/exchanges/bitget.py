"""Bitget spot fills, read through the Classic (v2) API with a signed, read-only key.

The first venue behind the exchange seam. One signed endpoint, `GET /api/v2/spot/trade/fills`,
pages the account's spot executions backwards by trade id; one public endpoint,
`GET /api/v2/spot/public/symbols`, says which coin is the base and which the quote of a
symbol. Everything else here is the discipline of reading both without trusting either.

## Confirmed against Bitget's documentation on 2026-09-25

Sources, each read on that date -- `docs/providers.md` carries the full table:

* Get Fills:
  `https://www.bitget.com/docs/catalog/classic-spot-trade/classic-spot-trade#get-fills`,
  static copy `https://www.bitget.com/legacy-docs/classic/spot/trade/Get-Fills`
* REST introduction: `https://www.bitget.com/docs/classic/rest-api`
* REST error codes: `https://www.bitget.com/docs/classic/error-code/restapi`
* the Classic introduction, Get Symbol Info, the UTA upgrade guide and the FAQ

What they say, and what this module relies on:

* `GET /api/v2/spot/trade/fills` on `https://api.bitget.com`. Parameters `symbol`, `orderId`,
  `startTime`, `endTime`, `limit`, `idLessThan`, all optional. `limit` defaults to 100 and
  is at most 100. `idLessThan` takes a **`tradeId`** and pages to older data. The range
  filters first and the cursor pages inside it. The span may not exceed 90 days, and only
  the last 90 days are kept.
* 10 requests a second per UID; 6000 a minute per IP overall.
* The envelope is `{"code": "00000", "msg": ..., "requestTime": ..., "data": [...]}`, and
  `data` is an array of fill objects whose fields are all strings.
* Headers `ACCESS-KEY`, `ACCESS-SIGN`, `ACCESS-TIMESTAMP` (epoch milliseconds),
  `ACCESS-PASSPHRASE`, `Content-Type: application/json`, `locale`. The pre-hash is
  `timestamp + METHOD + requestPath + "?" + queryString + body`, the body empty for a GET,
  signed with HMAC-SHA256 and sent in Base64. The timestamp must be within 30 seconds of the
  server's clock.
* `GET /api/v2/spot/public/symbols?symbol=X` is public and answers `baseCoin`, `quoteCoin`
  and `status`.
* Every error code in `BITGET_ERROR_MAP`, with the meaning its row gives. The documentation
  ties no code to an HTTP status.
* Classic is the account system this API serves. A Unified Trading Account reads fills from
  `GET /api/v3/trade/fills` instead, and the owner's account was confirmed Classic on
  2026-09-25; `docs/providers.md` records the finding, and UTA support is a follow-up.

**None of this has met the real venue.** Measuring a signed endpoint needs a key, and rule 3
keeps every key out of this repository. The owner's first sync is the first measurement, and
every guess below is written to fail loudly, as a typed error naming a field.

## Not documented, and treated as not known

* **Whether `startTime` and `endTime` are inclusive.** The request is widened by a
  millisecond and the two edge milliseconds are dropped after parsing, which is right under
  all four readings. See `build_fills_query` and `parse_fills_page`.
* **The order of fills within a page.** The next cursor is the smallest trade id on the page,
  which is right under any order. See `parse_fills_page`.
* **That a `tradeId` is numeric, or one sequence per account.** A numeric id of at most 64
  bits is required, and anything else fails the page loudly: the example shows digits, and
  paging every symbol with one `idLessThan` only works if the ids are one sequence.
* **What `size` and `amount` are measured in.** Base and quote, as the example's arithmetic
  (`13000 x 0.0007 = 9.1`) shows.
* **The sign of `feeDetail.totalFee`.** The REST example is negative for a fee paid; the
  WebSocket channel's is positive. Negative is read as paid and **positive is refused**.
* **What the fee fields mean when the fee is paid in BGB.** Refused.
* **`cTime`'s unit.** Described as "Unix second timestamp", and the example is thirteen-digit
  milliseconds. Read as milliseconds; a seconds value lands in 1970 and fails the page.
* **Any golden signature vector.** The documentation's samples use an empty secret and print
  nothing; the tests compute theirs outside this code.

## Every failure is one of the seven classes

`fetch_fill_page` raises `ValueError` for a caller's mistake -- before any request -- and one
of the seven `providers.exchanges.errors` classes for everything the venue or the network
did. Every vendor-supplied value passes a bound this module or `base` chooses before the
interpreter sees it: a trade id is at most nineteen digits before `int()`, a symbol is
matched against an ASCII pattern before a URL is built from it, an amount is at most a
hundred digits written out before `Decimal` arithmetic, a fill object is at most 32 levels
deep before it is rendered, and every text field is checked to encode as UTF-8. **No message
carries a value**, and no log call exists in this module: the transport logs
`https://api.bitget.com/exchange_fills`, never a path, a query or a header.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

import httpx

from portfolio.config import is_header_safe
from portfolio.domain.exchanges import ExchangeKey, FillSide
from portfolio.providers.base import decode_json
from portfolio.providers.errors import ProviderResponseError
from portfolio.providers.exchanges.base import (
    CursorKind,
    ExchangeCapabilities,
    NormalizedFill,
    RateLimit,
    assemble_fill_page,
    datetime_from_epoch_ms,
    derive_quote_quantity,
    encode_raw_payload,
    epoch_ms,
    require_fill_amount,
)
from portfolio.providers.exchanges.credentials import Credentials
from portfolio.providers.exchanges.errors import (
    ExchangeAuthError,
    ExchangeInsufficientScopeError,
    ExchangeInvalidRequestError,
    ExchangeRateLimitedError,
    ExchangeRetentionWindowError,
    ExchangeSchemaError,
    ExchangeUnavailableError,
    build_error_map,
    exchange_error,
)
from portfolio.providers.exchanges.signing import hmac_sha256_base64
from portfolio.providers.http import (
    ENDPOINT_EXTENSION,
    EXCHANGE_FILLS,
    EXCHANGE_SYMBOL,
    parse_retry_after,
    utc_now,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from decimal import Decimal

    from pydantic import SecretStr

    from portfolio.config import Settings
    from portfolio.providers.exchanges.base import FillPage, FillWindow

__all__ = [
    "ACCESS_KEY_HEADER",
    "ACCESS_PASSPHRASE_HEADER",
    "ACCESS_SIGN_HEADER",
    "ACCESS_TIMESTAMP_HEADER",
    "BITGET_API_URL",
    "BITGET_CAPABILITIES",
    "BITGET_ERROR_MAP",
    "FILLS_PATH",
    "LOCALE",
    "LOCALE_HEADER",
    "MAX_TRADE_ID",
    "PAGE_LIMIT",
    "SUCCESS_CODE",
    "SYMBOLS_PATH",
    "BitgetProvider",
    "SymbolAssets",
    "bitget_credentials",
    "build_fills_query",
    "build_prehash",
    "fill_symbols",
    "parse_fill",
    "parse_fills_page",
    "parse_symbol_info",
    "require_trade_id",
    "unwrap_envelope",
]

BITGET_API_URL: Final = "https://api.bitget.com"
"""The REST root, as the REST introduction documents it. A constant, not a setting.

There is one correct host and no self-hosting story, for the reason
`prices.kraken.KRAKEN_API_URL` gives.
"""

FILLS_PATH: Final = "/api/v2/spot/trade/fills"
"""Get Fills, Classic spot. Confirmed on 2026-09-25. Not callable with a UTA key."""

SYMBOLS_PATH: Final = "/api/v2/spot/public/symbols"
"""Get Symbol Info, Classic spot, public. Confirmed on 2026-09-25."""

ACCESS_KEY_HEADER: Final = "ACCESS-KEY"
ACCESS_SIGN_HEADER: Final = "ACCESS-SIGN"
ACCESS_TIMESTAMP_HEADER: Final = "ACCESS-TIMESTAMP"
# The name of a header, not a passphrase: S105 matches the word in the constant's name.
ACCESS_PASSPHRASE_HEADER: Final = "ACCESS-PASSPHRASE"  # noqa: S105
LOCALE_HEADER: Final = "locale"
LOCALE: Final = "en-US"
"""One of the two values the REST introduction lists, and the one this repository is written in."""

CONTENT_TYPE_HEADER: Final = "Content-Type"
JSON_CONTENT_TYPE: Final = "application/json"

SUCCESS_CODE: Final = "00000"
"""The envelope's `code` on success. A string with leading zeros, compared as one."""

PAGE_LIMIT: Final = 100
"""What `limit` is set to: the documented maximum, and the page size the capabilities declare."""

MAX_TRADE_ID: Final = 9_223_372_036_854_775_807
"""The largest trade id accepted: `2**63 - 1`, the largest signed 64-bit integer.

**A bound this application chooses.** The documentation does not say how large a trade id
may be. The example is eleven digits, and 64 bits is what an exchange's database hands out.
A value past it is not a trade id this code has reason to believe in, and refusing it keeps
`int()` far from the interpreter's digit limit.
"""

HTTP_OK: Final = 200

_ONE_MILLISECOND: Final = timedelta(milliseconds=1)

_TRADE_ID: Final = re.compile(r"\A[1-9][0-9]{0,18}\Z")
"""A positive integer, no leading zero, at most nineteen ASCII digits.

No leading zero so that string identity and numeric identity agree: `"012"` and `"12"` would
otherwise be two ids for one number, and the unique constraint compares strings. `[0-9]`
rather than `\\d`, which matches every Unicode digit, and `\\A...\\Z` rather than `^...$`,
because `$` also matches before a trailing newline.
"""

_SYMBOL: Final = re.compile(r"\A[A-Z0-9]{1,40}\Z")
"""A venue symbol, as it may be placed in a URL: upper-case ASCII letters and digits only.

Checked **before** a symbol is used to build the symbol-info request, so nothing a venue
sends can put a `/`, a `?`, a `&` or a character that needs encoding into a URL this
provider builds. Every symbol in the documentation's examples matches. Forty is a bound
chosen here, well past any symbol those examples show. **Assumed, not documented**: that
every symbol is upper-case letters and digits. One that is not fails its page loudly.
"""

_SIDES: Final[Mapping[str, FillSide]] = {"buy": FillSide.BUY, "sell": FillSide.SELL}

BITGET_CAPABILITIES: Final = ExchangeCapabilities(
    exchange_key=ExchangeKey.BITGET,
    retention=timedelta(days=90),
    max_query_window=timedelta(days=30),
    page_size=PAGE_LIMIT,
    cursor_kind=CursorKind.TRADE_ID_BEFORE,
    rate_limit=RateLimit(max_requests=10, per_ms=1000),
    requires_symbol=False,
)
"""What Bitget can do, as documented, with one number set below the documentation on purpose.

* `retention` is the documented 90 days. Error `40704` says "the last three months", and
  three calendar months can be 89 days; if the venue refuses the oldest window it arrives as
  `ExchangeRetentionWindowError` and #15 clamps further.
* **`max_query_window` is 30 days, below the documented 90.** The request is widened by a
  millisecond, so a 90-day window would be sent as 90 days and a millisecond, and a venue
  that states its limit in days may measure it by the calendar. Thirty is also UTA's limit,
  so a follow-up for UTA does not change the number #15 plans around.
* `rate_limit` is the documented 10 a second. It is declarative: the shared transport's
  per-host floor of one request a second is stricter.
* `requires_symbol` is `False`: `symbol` is optional, so one query covers every symbol.
"""

BITGET_ERROR_MAP: Final = build_error_map(
    {
        # The owner has to fix the key. A signature error is a wrong secret once the
        # pre-hash is proven by the golden vectors.
        (None, "40006"): ExchangeAuthError,  # Invalid ACCESS_KEY
        (None, "40037"): ExchangeAuthError,  # Apikey does not exist
        (None, "40041"): ExchangeAuthError,  # User's ApiKey does not exist
        (None, "40012"): ExchangeAuthError,  # apikey/password is incorrect
        (None, "40036"): ExchangeAuthError,  # passphrase is error
        (None, "40009"): ExchangeAuthError,  # sign signature error
        (None, "40038"): ExchangeAuthError,  # the current ip is not in the key's whitelist
        (None, "40018"): ExchangeAuthError,  # Invalid IP
        # The key was accepted and lacks read permission.
        (None, "40014"): ExchangeInsufficientScopeError,  # Incorrect permissions
        (None, "40025"): ExchangeInsufficientScopeError,  # the user does not have this permission
        (None, "40040"): ExchangeInsufficientScopeError,  # api key permission setting error
        # **Not auth.** The transport replays a signed request, and a skewed host clock is
        # not a bad key. A fresh request later is signed anew.
        (None, "40008"): ExchangeUnavailableError,  # Request timestamp expired
        (None, "40005"): ExchangeUnavailableError,  # Invalid ACCESS_TIMESTAMP
        # The in-band spelling of the throttle.
        (None, "429"): ExchangeRateLimitedError,  # Too many requests
        # #15 clamps further.
        (None, "40704"): ExchangeRetentionWindowError,  # only the last three months
        # A request this code built wrongly. Mapped by code so that it is not a schema
        # error when it arrives on a 200.
        (None, "00001"): ExchangeInvalidRequestError,  # startTime and endTime interval
        (None, "40705"): ExchangeInvalidRequestError,  # start and end cannot exceed 90 days
        (None, "40707"): ExchangeInvalidRequestError,  # start time is greater than end time
        (None, "40017"): ExchangeInvalidRequestError,  # parameter verification failed
        (None, "40019"): ExchangeInvalidRequestError,  # parameter cannot be empty
        (None, "40020"): ExchangeInvalidRequestError,  # parameter error
        (None, "40034"): ExchangeInvalidRequestError,  # parameter does not exist
        (None, "40102"): ExchangeInvalidRequestError,  # Symbol does not exist
        # The FAQ says these occur during deploys, and to retry.
        (None, "45001"): ExchangeUnavailableError,  # Unknown error
        (None, "40725"): ExchangeUnavailableError,  # service return an error
        (None, "40808"): ExchangeUnavailableError,  # parameter verification exception
        (None, "40015"): ExchangeUnavailableError,  # system is abnormal, try again later
    }
)
"""What differs from the fallbacks. Every key is `(None, code)`: the docs tie no code to a status.

**The header-missing codes are deliberately absent**: `40001` (ACCESS_KEY empty), `40002`
(ACCESS_SIGN empty), `40003` (signature empty) and `40011` (ACCESS_PASSPHRASE empty). This
provider always sends every header, so one of them means a bug in this code, and the status
fallback -- a 400 is an invalid request, a 200 with an unmapped code a schema error -- says
"needs a person" without marking the account `auth_failed` for a key that is fine.

The comments quote each code's documented meaning, read on 2026-09-25.
"""


@dataclass(frozen=True, slots=True)
class SymbolAssets:
    """Which coin a Bitget symbol is priced in and which it buys, as the venue says.

    `"BTCUSDT"` does not say where the base ends. Splitting it with a list of known quote
    coins is a guess that fails on the first new one -- silently, if it fails by matching
    the wrong one -- so the symbol-info endpoint is asked instead.
    """

    base_asset: str
    quote_asset: str


def bitget_credentials(settings: Settings) -> Credentials | None:
    """The Bitget credentials the settings hold, or `None` when none of the three is set.

    `Settings` has already refused a partial set and a blank value at startup, so "none set"
    and "all three set" are the only two cases that reach here. The check is `is None` on
    every field, as the price registry's is, so an unconfigured venue is told apart from a
    configured one by presence and never by truthiness.

    Raises:
        ValueError: a partial set, for a `Settings` built without its validator.
    """
    fields = (
        settings.bitget_api_key,
        settings.bitget_api_secret,
        settings.bitget_api_passphrase,
    )
    if all(value is None for value in fields):
        return None
    key, secret, passphrase = fields
    if key is None or secret is None or passphrase is None:
        message = (
            "The Bitget credentials are incomplete: PORTFOLIO_BITGET_API_KEY, "
            "PORTFOLIO_BITGET_API_SECRET and PORTFOLIO_BITGET_API_PASSPHRASE are all or none."
        )
        raise ValueError(message)
    return Credentials(api_key=key, api_secret=secret, passphrase=passphrase)


def build_fills_query(window: FillWindow, *, cursor: str | None) -> str:
    """The fills query string, exactly as it is signed and exactly as it is sent.

    **Keys in ascending order** -- `endTime`, `idLessThan` (only with a cursor), `limit`,
    `startTime` -- which satisfies both readings of the documentation: "the parameters after
    the `?`" and the samples' "sorted in ascending alphabetical order". What is sent *is*
    sorted, so the two cannot disagree. Every value is ASCII digits, so nothing needs
    encoding and nothing an HTTP library does can change the bytes.

    **The window is widened by a millisecond at each end.** Whether `startTime` and
    `endTime` are inclusive is not documented. The window is `[since, until)`, whose last
    millisecond is `until - 1`, and the request sends `startTime = epoch_ms(since) - 1` and
    `endTime = epoch_ms(until)`: one millisecond past each end. Under any of the four
    readings that covers `[since, until)` -- an exclusive `startTime` still admits `since`,
    an exclusive `endTime` still admits `until - 1` -- and `parse_fills_page` drops the two
    edge milliseconds an inclusive venue adds. A window starting at the epoch sends
    `startTime=0`, never `-1`.

    Raises:
        ValueError: `cursor` is not a canonical trade id, or the window ends at or before the
            epoch -- both the caller's mistake.
    """
    until_ms = epoch_ms(window.until)
    if until_ms <= 0:
        message = "The window must end after the epoch."
        raise ValueError(message)
    start_ms = max(epoch_ms(window.since) - 1, 0)
    parts = [f"endTime={until_ms}"]
    if cursor is not None:
        parts.append(f"idLessThan={_require_caller_cursor(cursor)}")
    parts.append(f"limit={PAGE_LIMIT}")
    parts.append(f"startTime={start_ms}")
    return "&".join(parts)


def build_prehash(timestamp_ms: int, request_path: str, query: str) -> str:
    """The string a Bitget GET is signed over: `timestamp + "GET" + path + "?" + query`.

    As the REST introduction documents it, the body empty for a GET, and nothing encoded
    before signing. The provider only ever sends GETs with a query, so the method and the
    `?` are fixed here rather than arguments that could be got wrong.
    """
    return f"{timestamp_ms}GET{request_path}?{query}"


def require_trade_id(value: object, *, field: str = "tradeId") -> str:
    """A trade id the venue sent, if it is canonical digits that fit 64 bits.

    `\\A[1-9][0-9]{0,18}\\Z` and at most `MAX_TRADE_ID`: a positive integer written without
    a leading zero, so string identity and numeric identity agree, and so short that `int()`
    is nowhere near the interpreter's digit limit. **Numeric ids are what the example shows,
    not what the documentation promises**, and the cursor rule needs them to be numbers, so
    any other shape fails the page loudly rather than being guessed at.

    Returns:
        The id exactly as sent.

    Raises:
        ExchangeSchemaError: any other value, a JSON number included. The detail names
            `field` and never the value.
    """
    if isinstance(value, str) and _is_trade_id(value):
        return value
    detail = (
        f"{field} must be a string of a positive integer with no leading zero, of at most "
        "19 digits and no larger than a signed 64-bit integer"
    )
    raise ExchangeSchemaError(detail)


def unwrap_envelope(
    status: int,
    body: str | bytes,
    *,
    retry_after_ms: int | None = None,
) -> object:
    """The `data` member of a successful Bitget answer, or the exception its failure is.

    A success is **HTTP 200 and a JSON object whose `code` is the string `"00000"`**, and
    nothing else is. Otherwise:

    | Answer | Raised |
    |---|---|
    | any status but 200 | `exchange_error(status, code)` |
    | 200, a body that is not JSON or not an object | `ExchangeSchemaError` |
    | 200, a `code` that is not `"00000"` | `exchange_error(200, code)` |
    | 200, `"00000"`, no `data` | `ExchangeSchemaError` |

    On a failing status the code is read from the body only if the body is a JSON object;
    otherwise the status decides alone, so a 502 carrying HTML is unavailable, not a schema
    error. On a 200 a mapped code is its class and an unmapped one is a schema error.

    **Nothing raised here has a cause or a context.** A parser error is about the body, and
    the body is what this provider must not repeat -- `json.JSONDecodeError` even keeps the
    whole document on its `doc` attribute. `from None` would still leave it as the suppressed
    `__context__`, which a debugger or an error tracker walks anyway, so the decode is
    finished before anything is raised. **`httpx.Response.raise_for_status` is never
    called**: its message carries the full URL.

    Raises:
        ExchangeError: one of the seven classes, as the table says.
    """
    if status != HTTP_OK:
        raise exchange_error(
            status,
            _code_of(body),
            error_map=BITGET_ERROR_MAP,
            retry_after_ms=retry_after_ms,
        )
    decoded, document = _decoded(body)
    if not decoded:
        detail = "the response body is not JSON"
        raise ExchangeSchemaError(detail, status=status)
    if not isinstance(document, dict):
        detail = "the response body is not a JSON object"
        raise ExchangeSchemaError(detail, status=status)
    code = document.get("code")
    if not (isinstance(code, str) and code == SUCCESS_CODE):
        raise exchange_error(
            status,
            code,
            error_map=BITGET_ERROR_MAP,
            retry_after_ms=retry_after_ms,
        )
    if "data" not in document:
        detail = "data is missing from a successful response"
        raise ExchangeSchemaError(detail, status=status)
    return document["data"]


def parse_symbol_info(data: object, *, symbol: str) -> SymbolAssets:
    """The base and quote coins of `symbol`, from the `data` of a symbol-info answer.

    **Exactly one entry, and it must be about the symbol asked.** An empty `data`, two
    entries, or one whose `symbol` is another's is refused rather than used: it is the
    `align_balances` rule, and an answer about another pair would put the wrong asset on
    every fill of this one. `baseCoin` and `quoteCoin` must be non-blank strings that encode
    as UTF-8. `status` is not read: a delisted pair (`offline`) still names its coins, and
    its old fills still need them.

    Raises:
        ExchangeSchemaError: any rule above. The detail names the field, never the value.
    """
    if not isinstance(data, list):
        detail = "data must be an array of symbols"
        raise ExchangeSchemaError(detail)
    if len(data) != 1:
        detail = f"the symbol answer carries {len(data)} entries where exactly one was asked for"
        raise ExchangeSchemaError(detail)
    entry = data[0]
    if not isinstance(entry, dict):
        detail = "the symbol answer's entry must be a JSON object"
        raise ExchangeSchemaError(detail)
    if entry.get("symbol") != symbol:
        detail = "symbol in the symbol answer is not the symbol that was asked about"
        raise ExchangeSchemaError(detail)
    return SymbolAssets(
        base_asset=_require_vendor_text(entry, "baseCoin", field="baseCoin"),
        quote_asset=_require_vendor_text(entry, "quoteCoin", field="quoteCoin"),
    )


def fill_symbols(data: object) -> tuple[str, ...]:
    """Every distinct symbol on a fills page, in first-seen order, each safe for a URL.

    The first thing read from a page, and the only thing read before the symbol cache is
    consulted: each `symbol` is matched against `\\A[A-Z0-9]{1,40}\\Z` **before** any URL is
    built from it. The page's shape is checked on the way -- an array, of objects, of at most
    `PAGE_LIMIT` -- so a page the parser would refuse costs no symbol request. A `data` of
    `null` is an empty page, with no symbols; see `parse_fills_page`.

    Raises:
        ExchangeSchemaError: the page is neither `null` nor an array of at most `PAGE_LIMIT`
            objects, or a fill's `symbol` is missing or not a safe symbol.
    """
    items = _fill_items(data)
    return tuple(dict.fromkeys(_require_symbol(item) for item in items))


def parse_fill(item: object, *, assets: SymbolAssets) -> NormalizedFill:
    """One Bitget fill object as a `NormalizedFill`, or a refusal naming the field.

    | `NormalizedFill` | From | Rule |
    |---|---|---|
    | `external_trade_id` | `tradeId` | `require_trade_id` |
    | `external_order_id` | `orderId` | a string, or absent or `null` |
    | `symbol` | `symbol` | `\\A[A-Z0-9]{1,40}\\Z` |
    | `base_asset`, `quote_asset` | `assets` | the symbol-info answer |
    | `side` | `side` | exactly `"buy"` or `"sell"` |
    | `quantity` | `size` | `require_fill_amount` |
    | `price` | `priceAvg` | `require_fill_amount` |
    | `quote_quantity` | `amount` | as reported; derived and flagged when absent, `null` or `""` |
    | `fee_amount` | `feeDetail.totalFee` | **negated**; a positive `totalFee` is refused |
    | `fee_asset` | `feeDetail.feeCoin` | required unless the fee is zero |
    | `executed_at` | `cTime` | epoch milliseconds |
    | `raw_payload` | the fill object | `encode_raw_payload`, never the envelope |

    **The fee sign.** The documented example is a buy of 0.0007 BTC with `totalFee`
    `"-0.0000007"` BTC -- 0.1% of the quantity, a fee paid, reported negative -- and
    `NormalizedFill` counts a fee paid as positive, so the value is negated. A positive
    `totalFee` is refused: the WebSocket channel reports the same field positive, and if
    REST ever did too, reading it as a rebate would record every fee as income, silently.
    The negation is `copy_negate()`, which is exact, rather than unary minus, which rounds
    to the calling thread's decimal context. A zero fee is stored as a positive zero with
    the exponent it was reported with, never as `-0`.

    **BGB deduction is refused.** `feeDetail.deduction` must be exactly `"no"`. What
    `totalFee` and `totalDeductionFee` hold when the fee is paid in BGB is not documented,
    and a guess would put the wrong amount in the wrong asset.

    Raises:
        ExchangeSchemaError: a field is missing, of the wrong type, or breaks its rule, or
            the fill breaks a `NormalizedFill` rule. The detail names the field -- the
            venue's spelling -- and never the value.
    """
    if not isinstance(item, dict):
        detail = "a fill must be a JSON object"
        raise ExchangeSchemaError(detail)
    trade_id = require_trade_id(_required(item, "tradeId", field="tradeId"))
    symbol = _require_symbol(item)
    side = _require_side(_required(item, "side", field="side"))
    quantity = require_fill_amount(_required(item, "size", field="size"), field="size")
    price = require_fill_amount(_required(item, "priceAvg", field="priceAvg"), field="priceAvg")
    reported = item.get("amount")
    if reported is None or reported == "":
        quote_quantity = derive_quote_quantity(quantity, price)
        derived = True
    else:
        quote_quantity = require_fill_amount(reported, field="amount")
        derived = False
    fee_amount, fee_asset = _parse_fee(_required(item, "feeDetail", field="feeDetail"))
    return NormalizedFill(
        external_trade_id=trade_id,
        external_order_id=_optional_text(item, "orderId"),
        symbol=symbol,
        base_asset=assets.base_asset,
        quote_asset=assets.quote_asset,
        side=side,
        quantity=quantity,
        price=price,
        quote_quantity=quote_quantity,
        quote_quantity_derived=derived,
        fee_amount=fee_amount,
        fee_asset=fee_asset,
        executed_at=_require_executed_at(_required(item, "cTime", field="cTime")),
        raw_payload=encode_raw_payload(item),
    )


def parse_fills_page(
    data: object,
    *,
    window: FillWindow,
    cursor: str | None,
    symbols: Mapping[str, SymbolAssets],
) -> FillPage:
    """A fills answer's `data` as the page the contract promises, or a refusal.

    Steps 5 to 7 of the spec's page, in order:

    0. **A `data` of `null` is an empty page** -- no fills, no next cursor. A tolerance chosen
       here, not a documented fact: the documented empty result is `[]`, but `null` under a
       success code can only mean "nothing", refusing it would fail every window without a
       trade in it, and it cannot hide a fill. Fills only; a `null` symbol-info answer is
       still refused. Any other `data` that is not an array is refused.
    1. **The raw count.** More than `PAGE_LIMIT` fills is refused *before* anything is
       parsed or dropped, so a 101-fill page cannot hide behind a dropped edge fill.
    2. **Every fill is parsed**, the two edge milliseconds included, so a malformed fill on
       the edge still fails the page.
    3. **No `tradeId` appears twice on the raw page**, the edge fills included. This is
       checked here, before the drop, and not only by `assemble_fill_page` after it: two
       fills sharing an id, one at `since - 1 ms` and one inside the window, would otherwise
       reach the page check as one fill and be accepted, where the same two inside the
       window are refused. The duplicate rule was written before the edge drop was placed
       in front of it -- spec 012's lesson of a rule reasoned about alone -- so every rule
       about the page as the venue sent it is applied to the raw page: the count, the ids,
       the cursor. `assemble_fill_page` still checks the kept fills as well.
    4. **With a cursor, every fill's `tradeId` must be below it**, or the venue ignored
       `idLessThan`. With the next rule, each cursor is strictly below the one before it, and
       a strictly decreasing sequence of positive integers is finite: **pagination
       terminates by construction**, a repeat and a cycle alike.
    5. **`next_cursor` is the smallest `tradeId` on the raw page** when it holds
       `PAGE_LIMIT` fills, and `None` otherwise. The smallest, not the last, because the
       order within a page is undocumented: whatever the order, "everything below the
       smallest" is exactly what has not been seen.
    6. **The two edge milliseconds are dropped**: a fill at exactly `since - 1 ms` or exactly
       `until` belongs to a neighbouring window under an inclusive reading, and that window
       fetches it. Anywhere else outside the window is **not** dropped, and
       `assemble_fill_page` refuses it.
    7. `assemble_fill_page` enforces the rest of the contract.

    Raises:
        ExchangeSchemaError: the page breaks any rule above or any `parse_fill` rule.
        ValueError: `cursor` is not a canonical trade id, a symbol on the page is missing
            from `symbols` (resolve every symbol `fill_symbols` returns first), or the
            window breaks `assemble_fill_page`'s caller rules -- each the caller's mistake.
    """
    items = _fill_items(data)
    fills: list[NormalizedFill] = []
    for item in items:
        assets = symbols.get(_require_symbol(item))
        if assets is None:
            message = (
                "A symbol on this page has no resolved assets; resolve every symbol "
                "fill_symbols returns before parsing the page."
            )
            raise ValueError(message)
        fills.append(parse_fill(item, assets=assets))
    trade_ids = [int(fill.external_trade_id) for fill in fills]
    distinct = len(set(trade_ids))
    if distinct != len(trade_ids):
        detail = (
            f"the page carries {len(trade_ids)} fills but only {distinct} distinct tradeId "
            "values, counted before the edge fills are dropped"
        )
        raise ExchangeSchemaError(detail)
    if cursor is not None:
        bound = int(_require_caller_cursor(cursor))
        at_or_above = sum(1 for trade_id in trade_ids if trade_id >= bound)
        if at_or_above:
            detail = (
                f"{at_or_above} fill(s) have a tradeId at or above the idLessThan cursor, so "
                "the venue did not page"
            )
            raise ExchangeSchemaError(detail)
    next_cursor = str(min(trade_ids)) if len(items) == PAGE_LIMIT else None
    edges = (window.since - _ONE_MILLISECOND, window.until)
    kept = [fill for fill in fills if fill.executed_at not in edges]
    return assemble_fill_page(
        window,
        kept,
        capabilities=BITGET_CAPABILITIES,
        cursor=cursor,
        next_cursor=next_cursor,
        symbol=None,
    )


class BitgetProvider:
    """Bitget spot fills over the Classic v2 API. Satisfies `ExchangeProvider` structurally.

    One instance per account, bound to the shared client. It holds the credentials as
    `SecretStr`s and reads the key and the passphrase out of them only while a request's
    headers are built; the secret is only ever unwrapped inside `signing.py`. It holds a
    per-instance symbol cache, so each symbol costs one public request for the life of the
    instance.

    **The transport replays a signed request, and that is accepted.** `RetryingTransport`
    retries a GET that got a 429, a 5xx or no answer by sending the same request again: the
    same timestamp and signature. The venue allows 30 seconds of skew; without a
    `Retry-After` the backoff is well inside it. With one near the transport's 30-second cap
    the replay can arrive expired, is refused with `40008`, and that is
    `ExchangeUnavailableError`: the next run signs a fresh request.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        credentials: Credentials,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind to the shared client and keep the credentials, refusing a set Bitget cannot use.

        `clock` is read once per signed request for `ACCESS-TIMESTAMP`, and for a
        `Retry-After` written as a date. It must return an aware `datetime`.

        **The API key and the passphrase must be text a header can carry**
        (`config.HEADER_SAFE_TEXT`): printable ASCII, no whitespace at either end. Both travel
        as header values, and one h11 refuses comes back as an `httpx.LocalProtocolError`
        whose message is the whole value. `Settings` refuses such a value at startup; this is
        the same rule for `Credentials` built any other way. The secret is not checked: it
        only ever enters an HMAC.

        Raises:
            ValueError: `credentials` carries no passphrase, which every Bitget key has, or
                its API key or passphrase is not header-safe. The message names the field,
                never the value.
        """
        if credentials.passphrase is None:
            message = "Bitget requires Credentials.passphrase, and none was given."
            raise ValueError(message)
        for field, secret in (
            ("api_key", credentials.api_key),
            ("passphrase", credentials.passphrase),
        ):
            if not is_header_safe(secret.get_secret_value()):
                message = (
                    f"Credentials.{field} holds a character an HTTP header cannot carry: "
                    "whitespace at either end, a control character, or a character outside "
                    "printable ASCII."
                )
                raise ValueError(message)
        self._client = client
        self._api_key: SecretStr = credentials.api_key
        self._api_secret: SecretStr = credentials.api_secret
        self._passphrase: SecretStr = credentials.passphrase
        self._clock = clock
        self._symbols: dict[str, SymbolAssets] = {}

    @property
    def capabilities(self) -> ExchangeCapabilities:
        """`BITGET_CAPABILITIES`. Constant for the life of the instance."""
        return BITGET_CAPABILITIES

    async def candidate_symbols(self) -> Sequence[str]:
        """Nothing: the fills endpoint covers every symbol, so none needs discovering."""
        return ()

    async def fetch_fill_page(
        self,
        window: FillWindow,
        *,
        cursor: str | None,
        symbol: str | None,
    ) -> FillPage:
        """One page of the account's spot fills inside `window`, older than `cursor`.

        1. A caller's mistake is refused before any request: a `symbol` (Bitget needs none),
           a window longer than `max_query_window`, a cursor that is not a canonical trade
           id. `assemble_fill_page` checks the first two again; checking here means a
           caller's bug costs no signed request.
        2. The query is built (`build_fills_query`), signed and sent.
        3. The answer is classified (`unwrap_envelope`).
        4. Each new symbol on the page is resolved through the symbol cache.
        5. The page is parsed, checked against the cursor and assembled
           (`parse_fills_page`).

        Raises:
            ValueError: the caller's mistake, as in step 1.
            ExchangeError: one of the seven classes, and only those.
        """
        _refuse_caller_mistakes(window, cursor=cursor, symbol=symbol)
        query = build_fills_query(window, cursor=cursor)
        data = await self._read_fills(query)
        for name in fill_symbols(data):
            if name not in self._symbols:
                self._symbols[name] = await self._read_symbol(name)
        return parse_fills_page(data, window=window, cursor=cursor, symbols=self._symbols)

    async def _read_fills(self, query: str) -> object:
        """Sign `query` and send it, exactly as signed. The `data` of the answer."""
        timestamp_ms = epoch_ms(self._clock())
        signature = hmac_sha256_base64(
            self._api_secret, build_prehash(timestamp_ms, FILLS_PATH, query)
        )
        headers = {
            ACCESS_KEY_HEADER: self._api_key.get_secret_value(),
            ACCESS_SIGN_HEADER: signature,
            ACCESS_TIMESTAMP_HEADER: str(timestamp_ms),
            ACCESS_PASSPHRASE_HEADER: self._passphrase.get_secret_value(),
            CONTENT_TYPE_HEADER: JSON_CONTENT_TYPE,
            LOCALE_HEADER: LOCALE,
        }
        return await self._get(f"{BITGET_API_URL}{FILLS_PATH}?{query}", EXCHANGE_FILLS, headers)

    async def _read_symbol(self, symbol: str) -> SymbolAssets:
        """Ask the public symbol endpoint what `symbol` is made of. No credential is sent.

        `symbol` has already matched `_SYMBOL` in `fill_symbols`, so it is ASCII letters and
        digits and needs no encoding.
        """
        data = await self._get(
            f"{BITGET_API_URL}{SYMBOLS_PATH}?symbol={symbol}",
            EXCHANGE_SYMBOL,
            {LOCALE_HEADER: LOCALE},
        )
        return parse_symbol_info(data, symbol=symbol)

    async def _get(self, url: str, label: str, headers: Mapping[str, str]) -> object:
        """Send one GET through the shared client and classify the answer.

        The URL is passed whole -- never `params=` -- so the query string sent is byte for
        byte the one that was signed.

        **An `httpx.LocalProtocolError` is an invalid request, with no cause and no context.**
        It means this side built a request h11 would not send -- the venue never saw it, and
        asking again cannot change it -- so it is not "unavailable", which #15 would retry
        forever, and it needs a person. It is never linked to what it replaces, not even as
        the suppressed `__context__` that `from None` still leaves for a debugger or an error
        tracker to walk: h11's message is `Illegal header value b'...'` with the **whole**
        header value in it, and the header values here are the API key and the passphrase. So
        it is raised after the `except` block has closed. The constructor's header-safe check
        makes this unreachable except through a bug, which is when a leak is least expected.

        **An `httpx.DecodingError` is unavailable, with no cause and no context.** A response
        declaring `Content-Encoding: gzip` or `deflate` whose body does not decompress makes
        `client.get` raise it while reading the body -- above the transport, and it is a
        `RequestError` but **not** a `TransportError`, so the arm below never saw it and it
        escaped the seven classes (measured by the tech lead through `build_http_client`, on a
        200 and on a 500). The answer could not be read, and its status is not known, because
        the error comes before the response is returned; a corrupt compressed body from an
        intermediary is most plausibly transient, so the next run asks again. It is not
        linked either: the decompressor's message says nothing useful, and whether it ever
        quotes the bytes is not this module's to guarantee. Named as the one class it is,
        like the other arms -- not widened to `httpx.RequestError`, which would also swallow
        failures nobody has measured.

        Raises:
            ExchangeInvalidRequestError: h11 refused the request before sending it.
            ExchangeUnavailableError: the request got no answer, chained `from` the
                `httpx.TransportError`, whose message carries no query; or its body could
                not be decompressed, with no cause.
            ExchangeError: whatever `unwrap_envelope` makes of the answer.
        """
        refused_locally = False
        undecodable = False
        try:
            response = await self._client.get(
                url, headers=headers, extensions={ENDPOINT_EXTENSION: label}
            )
        except httpx.LocalProtocolError:
            refused_locally = True
        except httpx.DecodingError:
            undecodable = True
        except httpx.TransportError as error:
            raise ExchangeUnavailableError from error
        if refused_locally:
            raise ExchangeInvalidRequestError
        if undecodable:
            raise ExchangeUnavailableError
        retry_after_ms = None
        if response.status_code != HTTP_OK:
            retry_after_ms = parse_retry_after(response.headers.get("retry-after"), self._clock())
        return unwrap_envelope(
            response.status_code, response.content, retry_after_ms=retry_after_ms
        )


def _refuse_caller_mistakes(window: FillWindow, *, cursor: str | None, symbol: str | None) -> None:
    """Refuse, before any request, what `assemble_fill_page` would refuse after one."""
    if symbol is not None:
        message = "Bitget lists fills for every symbol at once; no symbol may be given."
        raise ValueError(message)
    if window.duration > BITGET_CAPABILITIES.max_query_window:
        message = "The window is longer than Bitget's max_query_window."
        raise ValueError(message)
    if cursor is not None:
        _require_caller_cursor(cursor)


def _is_trade_id(value: str) -> bool:
    """Whether `value` is canonical trade-id digits no larger than `MAX_TRADE_ID`.

    The pattern bounds the length to nineteen digits before `int()` sees it.
    """
    return _TRADE_ID.match(value) is not None and int(value) <= MAX_TRADE_ID


def _require_caller_cursor(cursor: object) -> str:
    """A cursor the caller passed, if it is a canonical trade id. `ValueError` otherwise.

    A `ValueError` and not a schema error: the cursor came from the caller, and a cursor
    this provider issued always passes. Never names the value.
    """
    if isinstance(cursor, str) and _is_trade_id(cursor):
        return cursor
    message = (
        "The cursor must be a trade id this provider issued: a positive integer with no "
        "leading zero that fits a signed 64-bit integer."
    )
    raise ValueError(message)


def _code_of(body: str | bytes) -> object:
    """The `code` of an error body, if the body is a JSON object; `None` otherwise.

    A body that does not parse -- an HTML error page from a proxy -- contributes no code, and
    the status decides alone.
    """
    _, document = _decoded(body)
    return document.get("code") if isinstance(document, dict) else None


def _decoded(body: str | bytes) -> tuple[bool, object]:
    """`(True, document)` if the body is JSON, `(False, None)` if it is not. Never raises.

    The caller raises *after* this returns, so its exception has no `__context__`: a
    `ProviderResponseError` from `decode_json` is chained from the parser's own error, and
    `json.JSONDecodeError` keeps the whole body on its `doc` attribute.
    """
    try:
        return True, decode_json(body)
    except ProviderResponseError:
        return False, None


def _fill_items(data: object) -> list[dict[str, object]]:
    """The fill objects of a page, after its shape and its **raw** count are checked.

    **A `data` of `null` is an empty page -- a tolerance chosen here, not a documented fact.**
    The documented empty result is `[]`. But `null` under `code == "00000"` can only mean
    "nothing", and if Bitget spells an empty result that way, refusing it would fail every
    window without a trade in it -- for an owner who rarely trades on this venue, most of
    them. It cannot hide a fill: there is nothing in a `null` to drop. It applies to the
    fills answer only; a symbol-info answer whose `data` is `null` is still refused, because
    that question was about a symbol a fill named, and "no such symbol" is an anomaly there.
    """
    if data is None:
        return []
    if not isinstance(data, list):
        detail = "data must be an array of fills"
        raise ExchangeSchemaError(detail)
    if len(data) > PAGE_LIMIT:
        detail = (
            f"the page carries {len(data)} fills, more than the limit of {PAGE_LIMIT} it "
            "was asked for"
        )
        raise ExchangeSchemaError(detail)
    items: list[dict[str, object]] = []
    for item in data:
        if not isinstance(item, dict):
            detail = "every element of data must be a JSON object"
            raise ExchangeSchemaError(detail)
        items.append(item)
    return items


def _required(document: Mapping[str, object], key: str, *, field: str) -> object:
    """`document[key]`, or a schema error naming `field` when it is absent."""
    if key not in document:
        detail = f"{field} is missing"
        raise ExchangeSchemaError(detail)
    return document[key]


def _require_symbol(item: Mapping[str, object]) -> str:
    """The fill's `symbol`, if it is safe to put in a URL."""
    value = _required(item, "symbol", field="symbol")
    if not isinstance(value, str) or _SYMBOL.match(value) is None:
        detail = "symbol must be 1 to 40 upper-case ASCII letters and digits"
        raise ExchangeSchemaError(detail)
    return value


def _require_side(value: object) -> FillSide:
    """`"buy"` or `"sell"`, exactly. The type is checked before the lookup, which hashes."""
    side = _SIDES.get(value) if isinstance(value, str) else None
    if side is None:
        detail = 'side must be "buy" or "sell"'
        raise ExchangeSchemaError(detail)
    return side


def _require_executed_at(value: object) -> datetime:
    """`cTime` as an instant: epoch milliseconds, as the example shows."""
    try:
        return datetime_from_epoch_ms(value)
    except ExchangeSchemaError:
        pass
    # Raised after the handler has closed, so it carries no context: the refusal above says
    # nothing this one does not, and a context is one more link for something to render.
    detail = "cTime must be a non-negative count of epoch milliseconds, written in digits"
    raise ExchangeSchemaError(detail)


def _parse_fee(value: object) -> tuple[Decimal, str | None]:
    """`feeDetail` as `(fee_amount, fee_asset)`, with the sign and deduction rules applied."""
    if not isinstance(value, dict):
        detail = "feeDetail must be a JSON object"
        raise ExchangeSchemaError(detail)
    deduction = _required(value, "deduction", field="feeDetail.deduction")
    if not (isinstance(deduction, str) and deduction == "no"):
        detail = (
            'feeDetail.deduction must be "no": a fee deducted in BGB is not supported, '
            "because what the fee fields hold then is not documented"
        )
        raise ExchangeSchemaError(detail)
    total_fee = require_fill_amount(
        _required(value, "totalFee", field="feeDetail.totalFee"), field="feeDetail.totalFee"
    )
    if total_fee > 0:
        detail = (
            "feeDetail.totalFee is positive; this venue reports a fee paid as a negative "
            "number, and a positive one is refused rather than recorded as a rebate"
        )
        raise ExchangeSchemaError(detail)
    fee_amount = total_fee.copy_abs() if total_fee.is_zero() else total_fee.copy_negate()
    coin = value.get("feeCoin")
    if coin is None or coin == "":
        if not fee_amount.is_zero():
            detail = "feeDetail.feeCoin is required when the fee is not zero"
            raise ExchangeSchemaError(detail)
        return fee_amount, None
    return fee_amount, _require_vendor_text(value, "feeCoin", field="feeDetail.feeCoin")


def _optional_text(document: Mapping[str, object], key: str) -> str | None:
    """A text field that may be absent or `null`, and otherwise a string that encodes as UTF-8.

    `NormalizedFill` would refuse unencodable text too, but it names its own field rather than
    the venue's, and its refusal carries the `UnicodeEncodeError` as context -- whose `args`
    hold the whole string.
    """
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        detail = f"{key} must be a string when present"
        raise ExchangeSchemaError(detail)
    if not _encodes_as_utf8(value):
        detail = f"{key} does not encode as UTF-8"
        raise ExchangeSchemaError(detail)
    return value


def _require_vendor_text(document: Mapping[str, object], key: str, *, field: str) -> str:
    """A required text field: a non-blank string that encodes as UTF-8.

    The encoding is checked here, where the field's name is known, rather than left to
    `NormalizedFill`, which would name its own field instead of the venue's.
    """
    value = _required(document, key, field=field)
    if not isinstance(value, str) or not value.strip():
        detail = f"{field} must be a non-blank string"
        raise ExchangeSchemaError(detail)
    if not _encodes_as_utf8(value):
        detail = f"{field} does not encode as UTF-8"
        raise ExchangeSchemaError(detail)
    return value


def _encodes_as_utf8(value: str) -> bool:
    """Whether `value` encodes as UTF-8 -- in practice, whether it holds a lone surrogate.

    A predicate rather than a raise, so the caller's refusal has no `__context__`: a
    `UnicodeEncodeError` keeps the whole string in its `args`.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
