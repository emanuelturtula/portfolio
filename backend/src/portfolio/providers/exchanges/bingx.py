"""BingX spot fills, read through the spot v1 API with a signed, read-only key.

The second venue behind the exchange seam. One signed endpoint,
`GET /openApi/spot/v1/trade/myTrades`, pages the account's spot executions **forwards in
time**. Every symbol comes back in one query, and each fill names its own symbol, spelled
`BASE-QUOTE`, so there is no second endpoint to ask.

## Three sources, and which one each fact rests on

The documentation contradicts itself on the two facts this design depends on most, so every
fact below names its source:

* **V3**: https://bingx-api.github.io/docs-v3/, the current documentation, last deployed
  2026-09-19. Its content ships in the `BingX-API/docs-v3` repository's bundle, which is
  where it was read.
* **V1**: https://bingx-api.github.io/docs/, the older site, deployed from the
  `BingX-API/docs` repository's `gh-pages` branch on 2026-01-22.
* **Probe**: three read-only scripts the owner ran on 2026-09-26/27 against the real API
  with their own read-only key. They printed codes, counts, JSON types and time relations,
  never an amount, an id, a key or a signature. The account held a few dozen fills in one
  symbol, the oldest under two weeks old. Where the probe and the documentation differ, the
  probe wins.

`docs/providers.md` carries the full table. What this module relies on, with its source:

* `GET /openApi/spot/v1/trade/myTrades` on `https://open-api.bingx.com`, signed, key
  permission "Read". V3, V1; the probe was answered as documented.
* `symbol` is not required, and without it every symbol is answered. V3 and the probe; V1
  marks it required.
* `startTime` and `endTime` are epoch milliseconds (V3) and **both inclusive** (the probe).
* Fills are sorted by `time`, ascending. V3, V1; the probe saw ascending time and id.
* **A capped page holds the oldest fills in range**, with both bounds, with one and with
  none, and without a symbol. Not documented; the third probe, 2026-09-27. Paging
  forward from the newest fill is complete because of it.
* Fills sharing one millisecond occur. The probe only.
* The envelope is `{code, msg, data: {fills: [...]}}`, code `0` on success. V3, V1, probe.
* **Every error the probe saw arrived on HTTP 200**, with a non-zero integer `code`; an
  empty window is code `0` with `data.fills: []`. The probe only.
* The signature is HMAC-SHA256 as 64 lower-case hex characters, over the parameters sorted
  by key in ASCII order with `timestamp` included, appended as `signature`; the key travels
  in `X-BX-APIKEY`. V3; the probe's request signed this way was accepted.
* A request outside the venue's window, 5000 ms by default, is refused with `100421`. V3;
  the probe's 60-second-old timestamp was.
* 5 requests a second per UID for this endpoint. V3, V1.
* `commission` is a `float64` JSON number and `quoteQty`, `price` and `qty` are strings.
  V3's types, and its own sample carries `"17.997667582000002"`; the probe saw a bare
  number for `commission`.

## Not established, and designed around

* **Whether trade ids are unique across symbols.** An id of about 26 bits cannot number
  every spot trade on a large venue, so they are very likely a sequence per symbol. Ids are
  namespaced `"{symbol}:{id}"` and the cursor is a time, which is correct under either
  scheme. See `parse_fill` and `parse_fills_page`.
* **Retention.** "Only the past 7 days" (V3, V1) is disproved by the probe, which read fills
  more than a week old. A year is the bound declared, and it is a bound, not a measurement.
  See `BINGX_CAPABILITIES`.
* **The largest page.** V3 says "Default 500, maximum 1000" and, on the same page,
  "limit = 500". This provider asks for 500 and calls exactly 500 full.
* **What the venue answers for a window older than it keeps.** Nothing maps to
  `ExchangeRetentionWindowError`.

## Every failure is one of the seven classes

`fetch_fill_page` raises `ValueError` for a caller's mistake, before any request, and one of
the seven `providers.exchanges.errors` classes for everything the venue or the network did.
Every vendor-supplied value passes a bound before the interpreter sees it: an id is a JSON
integer compared with `2**63 - 1`, never converted from text; a symbol is at most 61
characters, with an ASCII quote and no whitespace or control character in its base; an
amount is at most a hundred digits written out; a fill object is at most 32
levels deep. **No message carries a value**, and no log call exists in this module: the
transport logs `https://open-api.bingx.com/exchange_fills`, never a path, a query or a
header. That matters more here than for Bitget, because **the signature travels in the
query string**.
"""

from __future__ import annotations

import decimal
import re
import unicodedata
from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Decimal
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
    ExchangeSchemaError,
    ExchangeUnavailableError,
    build_error_map,
    exchange_error,
)
from portfolio.providers.exchanges.signing import hmac_sha256_hex
from portfolio.providers.http import (
    ENDPOINT_EXTENSION,
    EXCHANGE_FILLS,
    parse_retry_after,
    utc_now,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime

    from pydantic import SecretStr

    from portfolio.config import Settings
    from portfolio.providers.exchanges.base import FillPage, FillWindow

__all__ = [
    "API_KEY_HEADER",
    "BINGX_API_URL",
    "BINGX_CAPABILITIES",
    "BINGX_ERROR_MAP",
    "FILLS_PATH",
    "MAX_TRADE_ID",
    "PAGE_LIMIT",
    "SIGNATURE_PARAM",
    "SIGNIFICANT_DIGITS",
    "SUCCESS_CODE",
    "BingXProvider",
    "bingx_credentials",
    "build_fills_query",
    "from_binary_float",
    "parse_fill",
    "parse_fills_page",
    "unwrap_envelope",
]

BINGX_API_URL: Final = "https://open-api.bingx.com"
"""The REST root (V3). A constant, not a setting, for the reason `BITGET_API_URL` gives.

V3 also names a fallback domain, `open-api.bingx.io`, "only available when the primary
domain is unavailable" and capped at 60 requests a minute. It is not used: an outage is
`ExchangeUnavailableError`, and the next run asks again.
"""

FILLS_PATH: Final = "/openApi/spot/v1/trade/myTrades"
"""Query transaction details, spot (V3, V1). Answered as documented in the probe."""

API_KEY_HEADER: Final = "X-BX-APIKEY"
"""The one credential header (V3). The secret never leaves this process; the key goes here."""

SIGNATURE_PARAM: Final = "signature"
"""The query parameter the signature travels in, appended last (V3)."""

SUCCESS_CODE: Final = 0
"""The envelope's `code` on success: the JSON integer `0`. Not `false`, not `"0"`."""

PAGE_LIMIT: Final = 500
"""What `limit` is set to, and the page size the capabilities declare.

V3 documents "Default 500, maximum 1000" in the parameter table and "limit = 500" in the
notes on the same page. The probe could not tell which the venue enforces: the account held
a few dozen fills. **Asking for 500 and calling exactly 500 full** is right under both
readings. At 1000, a venue that enforces 500 would return 500, the page would look short,
and the rest of the window would be lost without a word.
"""

MAX_TRADE_ID: Final = 9_223_372_036_854_775_807
"""The largest id accepted, trade or order: `2**63 - 1`. V3 types both as `int64`."""

SIGNIFICANT_DIGITS: Final = 15
"""How many significant digits of a `float64`-encoded field are information: `DBL_DIG`.

Every IEEE-754 double carries 15 significant decimal digits faithfully; any digit after
those is an artefact of the binary representation. See `from_binary_float`.
"""

HTTP_OK: Final = 200

_BINARY_FLOAT_CONTEXT: Final = decimal.Context(
    prec=SIGNIFICANT_DIGITS,
    rounding=ROUND_HALF_EVEN,
    Emax=decimal.MAX_EMAX,
    Emin=decimal.MIN_EMIN,
    traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
    flags=[],
)
"""The one context `from_binary_float` rounds in, passed explicitly.

Explicit for the reason `domain.money._MONEY_CONTEXT` is: an ambient context is state a
caller can change, and a rounding rule that depends on it is not a rule. The exponent range
is the widest `decimal` allows, so no finite input can overflow it.
"""

_CURSOR: Final = re.compile(r"\A(?:0|[1-9][0-9]{0,14})\Z")
"""A time cursor as this provider issues one: epoch milliseconds, canonical digits.

No leading zero, so a cursor has one spelling. At most fifteen digits, so `int()` never sees
a long string, and fifteen digits of milliseconds is past the year 30000.
"""

_QUOTE: Final = re.compile(r"\A[A-Z0-9]{1,20}\Z")
"""A BingX quote asset: what follows the **last** hyphen of a spot symbol.

Upper-case ASCII letters and digits. Every quote in the live public symbol list matches
(read on 2026-09-27: `BTC`, `DOG`, `ETH`, `L3`, `USDC`, `USDT`, `WHALES`, `ZKL`), and so
does every quote `myTrades`' own validation message names: `USDT`, `USD1`, `USDT2`,
`USDC`, `ETH` and `BTC`. Twenty is a bound chosen here, far past any of them.
"""

_MAX_QUOTE_LENGTH: Final = 20
"""The longest quote `_QUOTE` admits, for bounding a symbol's length before it is split."""

MAX_BASE_LENGTH: Final = 40
"""The longest base asset accepted: everything before a symbol's last hyphen.

**The base is wide on purpose.** BingX renames a pair when its token migrates, and the old
name takes forms like `STRK-OLD-USDT`, `H_OLD-USDT` and `PUMP_OLD-USDT`. The live list also
holds `$U-USDT`, `D.O.G.E.-USDT`, `ATOM(ARC20)-USDT` and `MØTH-USDT`: 38 of 2273 pairs are
not two runs of letters and digits (the public `GET /openApi/spot/v1/common/symbols`, read
on 2026-09-27). A pair the owner holds can become one of these at any time, and a pattern
that refused them would fail every page carrying it, on every run. So a base may hold any
character except whitespace and the Unicode control, format, surrogate, private-use and
unassigned categories (`C*`). Forty characters is a bound chosen here, more than twice the
longest base in that list (17).
"""

BINGX_CAPABILITIES: Final = ExchangeCapabilities(
    exchange_key=ExchangeKey.BINGX,
    retention=timedelta(days=365),
    max_query_window=timedelta(days=30),
    page_size=PAGE_LIMIT,
    cursor_kind=CursorKind.TIME,
    rate_limit=RateLimit(max_requests=5, per_ms=1000),
    requires_symbol=False,
)
"""What BingX can do, as far as the documentation and the probe establish it.

* **`retention` is 365 days: a declared bound, not a measured one.** V3 and V1 both say
  "Can only check data within the past 7 days range", and the probe disproved it: a
  time-bounded query returned fills more than a week old, and spans of 14, 30, 90 and
  365 days
  ending now all answered code 0 with every fill. The only longer statement BingX makes is
  its support centre's, that trade records are "available for up to one year", about the
  web export. Declaring more would claim history the venue may not return; declaring the
  documented 7 would drop the owner's own fills. With a year, `history_truncated` tells the
  owner that nothing before a year ago is promised, which is true.
* **`max_query_window` is 30 days.** Spans up to 365 days were accepted, so this is
  headroom, not a limit: a first backfill is 13 windows.
* `page_size` is `PAGE_LIMIT`, for the reason given there.
* **`cursor_kind` is `TIME`**: the next page starts at the newest fill's millisecond. See
  `parse_fills_page` for why not `fromId`.
* `rate_limit` is the documented 5 a second per UID. It is declarative: the shared
  transport's per-host floor of one request a second is stricter.
* **`requires_symbol` is `False`**: the probe's query without a symbol answered code 0 with
  every fill, each naming its own symbol. V1 marks `symbol` required; V3 does not.
"""

BINGX_ERROR_MAP: Final = build_error_map(
    {
        # The owner has to fix the key. A signature mismatch is a wrong secret once the golden
        # vectors prove the recipe.
        (None, "100001"): ExchangeAuthError,  # signature verification failed (V3, V1; probe)
        (None, "100412"): ExchangeAuthError,  # Null signature (V3, V1)
        (None, "100413"): ExchangeAuthError,  # Incorrect apiKey / Null apiKey (V3, V1; probe)
        (None, "100419"): ExchangeAuthError,  # IP does not match IP whitelist (V3, V1)
        (None, "100414"): ExchangeAuthError,  # The account is abnormal (V1, spot)
        (None, "100441"): ExchangeAuthError,  # Account is abnormal or KYC required (V3, spot)
        (None, "100401"): ExchangeAuthError,  # AUTHENTICATION_FAIL (V1's legacy status list)
        # The key was accepted and lacks the permission.
        (None, "100004"): ExchangeInsufficientScopeError,  # Permission denied (V3, V1)
        # **Not auth.** The transport replays a signed request, and a skewed host clock is not
        # a bad key. A fresh request on the next run is signed anew.
        (None, "100421"): ExchangeUnavailableError,  # timestamp mismatch (V3, V1; probe)
        # The throttle, in both spellings, and the ban that follows it.
        (None, "100410"): ExchangeRateLimitedError,  # rate limitation (V3, V1)
        (None, "109429"): ExchangeRateLimitedError,  # APIRateLimit, 100410's successor (V1)
        (418, None): ExchangeRateLimitedError,  # IP banned after continuing past a 429 (V3)
        # Retry on the next run.
        (None, "100500"): ExchangeUnavailableError,  # System busy (V3, V1)
        (None, "100503"): ExchangeUnavailableError,  # Server busy (V1)
        (None, "109500"): ExchangeUnavailableError,  # system busy (V3 changelog, 2026-09-05)
        # A request this code built wrongly, mapped so that it is not a schema error on a 200.
        (None, "100400"): ExchangeInvalidRequestError,  # parameter error (V3, V1; probe)
        (None, "100204"): ExchangeInvalidRequestError,  # data not found / span too wide (V3)
        (None, "100404"): ExchangeInvalidRequestError,  # path does not exist (V3)
        (None, "100490"): ExchangeInvalidRequestError,  # spot trading pair is offline (V3, V1)
    }
)
"""What differs from the fallbacks. Every key is `(None, code)` except the 418.

**Every error the probe saw arrived on HTTP 200**, with the code in the body, so the code
decides under any status and the status decides only when the body carries none. An unmapped
code on a 200 is a schema error: loud, and never retried.

Notes on individual rows, from reading both documentation sites on 2026-09-27:

* **`100421` has two meanings in V3.** The Common table says "Null timestamp or timestamp
  mismatch with server time"; the Spot table says "Request rejected", about order placement
  limits. V1 says both, plus "The current system is busy". The probe settles it for this
  endpoint: a 60-second-old timestamp is answered `100421`. Unavailable fits every reading,
  and auth fits none. #15 marks an account `auth_failed` for auth, and the key is not what
  failed. This is the same decision as Bitget's `40008`.
* **`109429`** is in V1's changelog of 2025-10-11: "Old error code 100410 has been updated
  to new error code 109429, meaning: APIRateLimit", from 2025-10-16. That list is among
  futures codes, and V3 lists `109429` under Futures only. Both spellings are mapped,
  because a throttle read as anything else is retried wrongly or not at all.
* **`100414` and `100441`** both say the account is abnormal, V1's in its spot list and
  V3's in its Spot table with "or advanced identity verification is required". Whether one
  replaced the other is not stated, so both are mapped.
* **`100401`** is V1's legacy `AUTHENTICATION_FAIL`. It can only mean the owner must act,
  and unmapped on a 200 it would never mark the account `auth_failed`.
* **`100403` is deliberately absent.** V1 calls it `AUTHORIZATION_FAIL`; V3 uses it for "not
  the main account". Two meanings is no meaning, so it takes the fallback: a schema error on
  a 200.
* **`109500`** is V3's "system is busy" code for a sibling endpoint: its changelog of
  2026-09-05 says `/openApi/swap/v2/user/positions` now answers a temporarily unavailable
  backend with `109500` "instead of code=0 with data=[]". It is not documented for this
  endpoint, and is mapped defensively, like `109429`.
* **`100204` is never an empty page.** The probe shows an empty window is code 0 with
  `fills: []`, so `100204` ("data not found", "query time span is too wide") is a request
  this code should not have built.
* **Nothing maps to `ExchangeRetentionWindowError`.** What BingX answers for a window older
  than it keeps is unknown. If it is an error code, it is unmapped and loud, and the first
  real case gets a mapping. If it is an empty success, it is indistinguishable from no
  trades, which is why the declared retention sits well outside the owner's history.
"""


def bingx_credentials(settings: Settings) -> Credentials | None:
    """The BingX credentials the settings hold, or `None` when neither variable is set.

    `Settings` has already refused a partial set, a blank value and an unsendable key at
    startup, so "none set" and "both set" are the only cases that reach here. The check is
    `is None`, never truthiness, as `bitget_credentials`'s is.

    Raises:
        ValueError: a partial set, for a `Settings` built without its validator.
    """
    key = settings.bingx_api_key
    secret = settings.bingx_api_secret
    if key is None and secret is None:
        return None
    if key is None or secret is None:
        message = (
            "The BingX credentials are incomplete: PORTFOLIO_BINGX_API_KEY and "
            "PORTFOLIO_BINGX_API_SECRET are both or neither."
        )
        raise ValueError(message)
    return Credentials(api_key=key, api_secret=secret)


def build_fills_query(window: FillWindow, *, cursor: str | None, timestamp_ms: int) -> str:
    """The fills query string, exactly as it is signed and exactly as it is sent.

    `endTime=<until - 1 ms>&limit=500&startTime=<start>&timestamp=<timestamp_ms>`, and
    nothing else: no `symbol` (every symbol is answered without one), no `fromId`, no
    `recvWindow` (the venue's default of 5000 ms applies). The signature is appended by the
    caller, after it, as `&signature=<hex>`.

    **The keys are in ASCII order, `timestamp` included**, which is V3's recipe. V1 says
    "without sorting". Both are satisfied, because the string sent is the string signed. Every
    value is ASCII digits, so nothing needs encoding, and nothing an HTTP library does can
    change the bytes.

    **Both bounds are inclusive**, which the probe established: `[T, T]` returns the fill
    at `T`, and `[T + 1, ...]` and `[..., T - 1]` do not. So `[since, until)` is sent as
    `startTime = since` and `endTime = until - 1 ms`, exactly. `startTime` is the cursor
    instead when there is one. A window starting before the epoch sends `startTime=0`.

    Raises:
        ValueError: `cursor` is not a time this provider could have issued for `window`, the
            window ends at or before the epoch, or `timestamp_ms` is not a non-negative
            `int`. Each is the caller's mistake.
    """
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int) or timestamp_ms < 0:
        message = "timestamp_ms must be a non-negative int number of epoch milliseconds."
        raise ValueError(message)
    start_ms = _start_ms(window, cursor)
    end_ms = _end_ms(window)
    return f"endTime={end_ms}&limit={PAGE_LIMIT}&startTime={start_ms}&timestamp={timestamp_ms}"


def from_binary_float(value: object) -> Decimal:
    """A `float64`-encoded amount with its binary artefacts removed: 15 significant digits.

    BingX produces `commission`, and evidently `quoteQty`, from IEEE-754 doubles: V3 types
    `commission` as `float64`, and its own sample shows `quoteQty` as
    `"17.997667582000002"`. A double carries 15 significant decimal digits faithfully
    (`DBL_DIG`), and any digit after those is an artefact of the binary representation, not
    information the venue holds. So the value is rounded to 15 significant digits, half to
    even, in `Decimal` arithmetic, and `17.997667582000002` becomes `17.997667582`.

    | Input | Result |
    |---|---|
    | at most 15 significant digits | `value`, unchanged, digits and exponent alike |
    | more, but rounding does not change the number | `value`, unchanged |
    | more, and rounding changes it | the rounded number, trailing zeros after the point removed |

    The last row removes the zeros that rounding left behind, because they are as much an
    artefact as the digits they replaced: `-0.00005820000000000001` becomes `-0.0000582`,
    not `-0.0000582000000000000`. The sign is kept; negating a fee is the caller's business.

    **Applied to `commission` and `quoteQty` only**, never to `price` or `qty`: those are
    strings the venue formats exactly, and a 19-digit quantity must survive them intact. A
    value still finer than `FILL_SCALE` after rounding is refused by `NormalizedFill`, as
    before. This is not "round rather than refuse": it is a documented decoding of the
    venue's float encoding, applied to the two fields that carry one.

    Takes `object` so the type check is not statically dead, for the reason
    `providers.base._require_base_units` gives.

    Raises:
        TypeError: `value` is not a `Decimal`. A provider bug; parse with
            `require_fill_amount` first.
        ValueError: `value` is not finite.
    """
    if not isinstance(value, Decimal):
        message = f"from_binary_float requires a Decimal, got {type(value).__name__}"
        raise TypeError(message)
    if not value.is_finite():
        message = "from_binary_float requires a finite Decimal"
        raise ValueError(message)
    if len(value.as_tuple().digits) <= SIGNIFICANT_DIGITS:
        return value
    rounded = _BINARY_FLOAT_CONTEXT.plus(value)
    if rounded == value:
        return value
    return _without_trailing_zeros(rounded)


def unwrap_envelope(
    status: int,
    body: str | bytes,
    *,
    retry_after_ms: int | None = None,
) -> object:
    """The `data.fills` array of a successful BingX answer, or the exception its failure is.

    A success is **HTTP 200 and a JSON object whose `code` is the integer `0`** (not
    `false`, not `"0"`), **whose `data` is an object holding a `fills` array**. Nothing else
    is. Otherwise:

    | Answer | Raised |
    |---|---|
    | any status but 200 | `exchange_error(status, code)` |
    | 200, a body that is not JSON or not an object | `ExchangeSchemaError` |
    | 200, a `code` that is not the integer `0` | `exchange_error(200, code)` |
    | 200, code `0`, `data` absent, `null` or not an object | `ExchangeSchemaError` |
    | 200, code `0`, `fills` absent or not an array | `ExchangeSchemaError` |

    On a failing status the code is read from the body only if the body is a JSON object;
    otherwise the status decides alone, so a 502 carrying HTML is unavailable, not a schema
    error. On a 200 a mapped code is its class and an unmapped one, `false` and `"0"`
    included, is a schema error.

    **A missing list is never read as "no fills".** It is spec 014's lesson: the place an
    undocumented shape is plausible is where the venue failed to answer, and an empty
    success there makes every window read as empty, the checkpoints advance past it, and the
    history is lost once it ages out. The documented empty answer is `fills: []`.

    **Nothing raised here has a cause or a context**, for the reason
    `bitget.unwrap_envelope` gives. `httpx.Response.raise_for_status` is never called: its
    message carries the full URL, and here the URL carries the signature.

    Raises:
        ExchangeError: one of the seven classes, as the table says.
    """
    if status != HTTP_OK:
        raise exchange_error(
            status,
            _code_of(body),
            error_map=BINGX_ERROR_MAP,
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
    # `type(...) is int` rather than `isinstance`: `False == 0`, and a `bool` is an `int`.
    if not (type(code) is int and code == SUCCESS_CODE):
        raise exchange_error(
            status,
            code,
            error_map=BINGX_ERROR_MAP,
            retry_after_ms=retry_after_ms,
        )
    data = document.get("data")
    if not isinstance(data, dict):
        detail = "data must be a JSON object on a successful response"
        raise ExchangeSchemaError(detail, status=status)
    if "fills" not in data:
        detail = "data.fills is missing from a successful response"
        raise ExchangeSchemaError(detail, status=status)
    fills = data["fills"]
    if not isinstance(fills, list):
        detail = "data.fills must be an array"
        raise ExchangeSchemaError(detail, status=status)
    return fills


def parse_fill(item: object) -> NormalizedFill:
    """One BingX fill object as a `NormalizedFill`, or a refusal naming the field.

    | `NormalizedFill` | From | Rule |
    |---|---|---|
    | `external_trade_id` | `symbol`, `id` | `"{symbol}:{id}"`, the id an integer (below) |
    | `external_order_id` | `orderId` | an integer (below), as digits; `null` or absent: `None` |
    | `symbol` | `symbol` | as the venue spells it, at most 61 characters |
    | `base_asset`, `quote_asset` | `symbol` | split on its **last** hyphen |
    | `side` | `isBuyer` | exactly `true` (buy) or `false` (sell) |
    | `quantity` | `qty` | `require_fill_amount`, as reported |
    | `price` | `price` | `require_fill_amount`, as reported |
    | `quote_quantity` | `quoteQty` | `require_fill_amount`, then `from_binary_float` |
    | `fee_amount` | `commission` | `from_binary_float`, then **negated**; positive refused |
    | `fee_asset` | `commissionAsset` | required unless the fee is zero |
    | `executed_at` | `time` | epoch milliseconds, a JSON integer |
    | `raw_payload` | the fill object | `encode_raw_payload`, never the envelope |

    An id, trade or order, is a JSON integer from 1 to `2**63 - 1`. A `quoteQty` that is
    absent, `null` or `""` is derived with `derive_quote_quantity` and flagged
    `quote_quantity_derived`. `isMaker` is kept in `raw_payload` only.

    **The trade id is namespaced by symbol.** `NormalizedFill` needs an id unique per account
    across symbols, and here that is not established: an id of about 26 bits (the probe's,
    and the docs' own sample, `36767057`) cannot number every trade on a venue this size, so
    ids are very likely per symbol. `KAS-USDT:7` and `BTC-USDT:7` are two fills. Namespacing
    costs nothing if ids turn out to be global, and if they are per symbol it is the
    difference between two fills and one silently dropped by the unique constraint.

    **The cost: the id embeds a name BingX can change.** BingX renames a pair when its token
    migrates (`STRK-USDT` to `STRK-OLD-USDT`, say). If a fill is read once under the old name
    and again under the new one, the second read has a different `external_trade_id`, so it
    is inserted as a second fill instead of meeting #15's collision check. That needs the
    rename to fall between two reads of the same window, which in practice means the sync's
    five-minute overlap. It is a recorded risk, not designed around: `docs/providers.md`.

    **The id must be a JSON integer**, which `decode_json` returns as an `int` without a
    float in between; a string, a number with a point, and a `bool` are refused. Its size
    is compared with `MAX_TRADE_ID`, never converted from text.

    **The fee sign.** V3's sample buys BTC with `commission` `-0.000046483255` BTC, a fee
    paid, reported negative. The probe saw the same: negative, in the base asset, on buys.
    `NormalizedFill` counts a fee paid as positive, so the value is negated, with
    `copy_negate()`, which is exact. **A positive `commission` is refused**, as Bitget's is,
    until a real rebate shows what one looks like. A zero fee is a positive zero.

    Raises:
        ExchangeSchemaError: a field is missing, of the wrong type, or breaks its rule, or
            the fill breaks a `NormalizedFill` rule. The detail names the field, in the
            venue's spelling, and never the value.
    """
    if not isinstance(item, dict):
        detail = "a fill must be a JSON object"
        raise ExchangeSchemaError(detail)
    symbol, base_asset, quote_asset = _require_symbol(item)
    trade_id = _require_id(_required(item, "id"), field="id")
    order_id = item.get("orderId")
    side = _require_side(_required(item, "isBuyer"))
    quantity = require_fill_amount(_required(item, "qty"), field="qty")
    price = require_fill_amount(_required(item, "price"), field="price")
    reported = item.get("quoteQty")
    if reported is None or reported == "":
        quote_quantity = derive_quote_quantity(quantity, price)
        derived = True
    else:
        quote_quantity = from_binary_float(require_fill_amount(reported, field="quoteQty"))
        derived = False
    fee_amount, fee_asset = _parse_fee(item)
    return NormalizedFill(
        external_trade_id=f"{symbol}:{trade_id}",
        external_order_id=None if order_id is None else _require_id(order_id, field="orderId"),
        symbol=symbol,
        base_asset=base_asset,
        quote_asset=quote_asset,
        side=side,
        quantity=quantity,
        price=price,
        quote_quantity=quote_quantity,
        quote_quantity_derived=derived,
        fee_amount=fee_amount,
        fee_asset=fee_asset,
        executed_at=_require_executed_at(_required(item, "time")),
        raw_payload=encode_raw_payload(item),
    )


def parse_fills_page(fills: object, *, window: FillWindow, cursor: str | None) -> FillPage:
    """A `data.fills` array as the page the contract promises, or a refusal.

    **The cursor is a time: the epoch millisecond of the newest fill on a full page**, sent
    as the next request's `startTime`. Not `fromId`, which pages by trade id: if ids are a
    sequence per symbol, as their size suggests, `id >= X` means something different in
    every symbol's sequence, and a page from a symbol with low ids would be skipped for good,
    silently. The venue orders every symbol's fills by time, so a time is correct whatever
    the id scheme.

    Let `start` be the request's `startTime` (the cursor, or `since`) and `m` the newest
    fill's millisecond:

    1. **More than `PAGE_LIMIT` fills is refused** before anything is parsed.
    2. **Every fill is parsed** (`parse_fill`).
    3. **Every fill must be at or after `start`**, or the venue ignored `startTime`.
    4. **Fewer than `PAGE_LIMIT` fills: `next_cursor` is `None`.** The venue returned
       everything in range.
    5. **Exactly `PAGE_LIMIT`, `m` after `start`: `next_cursor` is `str(m)`.** The next
       request starts **at** `m`, not `m + 1`, because more fills may share that
       millisecond. The fills at `m` are read twice, and #15's unique constraint makes the
       second read insert nothing, and fills sharing a millisecond do occur (the probe).
       `m` is the newest by value, not the last as served, so the order *within* a page
       does not matter.

       **Which fills a capped page holds does matter.** Paging forward from `m` misses
       nothing only because the venue fills a capped page with the **oldest** fills in
       range, so everything after `m` is still unread. That is not documented -- `fromId`'s
       "by default, the latest trade will be retrieved" hints the other way -- and was
       established by the owner's third probe on 2026-09-27, asking exactly as this
       provider does. A venue that kept the newest would lose the rest of a full window
       silently.
    6. **Exactly `PAGE_LIMIT`, all at `start`: `ExchangeSchemaError`.** More than a page of
       fills in one millisecond cannot be paged past with a time cursor, and a loud failure
       beats a silent loss.
    7. `assemble_fill_page` enforces the rest, fills outside `[since, until)` included.

    **Pagination terminates by construction.** Each cursor is strictly after the one before
    (rules 3, 5 and 6), and every cursor is at or before `until - 1 ms` (rule 7). A strictly
    increasing sequence of integers below a bound is finite, which covers a repeat and a
    cycle alike: the same page served twice fails rule 6, or rule 3, on its second request.

    Raises:
        ExchangeSchemaError: the page breaks any rule above or any `parse_fill` rule.
        ValueError: `cursor` is not a time this provider could have issued for `window`, or
            the window breaks `assemble_fill_page`'s caller rules. Both are the caller's
            mistake.
    """
    start_ms = _start_ms(window, cursor)
    items = _fill_items(fills)
    parsed = [parse_fill(item) for item in items]
    moments = [epoch_ms(fill.executed_at) for fill in parsed]
    early = sum(1 for moment in moments if moment < start_ms)
    if early:
        detail = (
            f"{early} fill(s) executed before the startTime this page was asked from, so the "
            "venue did not honour it"
        )
        raise ExchangeSchemaError(detail)
    next_cursor = None
    if len(items) == PAGE_LIMIT:
        newest = max(moments)
        if newest <= start_ms:
            detail = (
                f"a full page of {PAGE_LIMIT} fills all executed in the millisecond it was "
                "asked from, and a time cursor cannot page past them"
            )
            raise ExchangeSchemaError(detail)
        next_cursor = str(newest)
    return assemble_fill_page(
        window,
        parsed,
        capabilities=BINGX_CAPABILITIES,
        cursor=cursor,
        next_cursor=next_cursor,
        symbol=None,
    )


class BingXProvider:
    """BingX spot fills over the spot v1 API. Satisfies `ExchangeProvider` structurally.

    One instance per account, bound to the shared client. It holds the credentials as
    `SecretStr`s and reads the key out only while a request's header is built; the secret is
    only ever unwrapped inside `signing.py`. It holds no cache and no state between calls.

    **The transport replays a signed request, and that is accepted.** `RetryingTransport`
    retries a GET that got a 429, a 5xx or no answer by sending the same request again: the
    same timestamp and signature. BingX allows 5000 ms by default (V3), less than a backoff
    can take, so a replay may arrive expired. It is then refused with `100421`, which is
    `ExchangeUnavailableError`, and the next run signs a fresh request. Every error the probe
    saw arrived on a 200, which the transport does not retry, so the replay is the rare path.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        credentials: Credentials,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind to the shared client and keep the credentials, refusing a set BingX cannot use.

        `clock` is read once per signed request for `timestamp`, and for a `Retry-After`
        written as a date. It must return an aware `datetime`.

        **The API key must be text a header can carry** (`config.HEADER_SAFE_TEXT`), for the
        reason `BitgetProvider` gives: h11 quotes a refused header value whole in its error.
        The secret is not checked: it only ever enters an HMAC.

        Raises:
            ValueError: `credentials` carries a passphrase, which BingX keys do not have, so
                one being set means the caller is confused; or its API key is not
                header-safe. The message names the field, never the value.
        """
        if credentials.passphrase is not None:
            message = "BingX keys have no passphrase, and Credentials.passphrase was given."
            raise ValueError(message)
        if not is_header_safe(credentials.api_key.get_secret_value()):
            message = (
                "Credentials.api_key holds a character an HTTP header cannot carry: "
                "whitespace at either end, a control character, or a character outside "
                "printable ASCII."
            )
            raise ValueError(message)
        self._client = client
        self._api_key: SecretStr = credentials.api_key
        self._api_secret: SecretStr = credentials.api_secret
        self._clock = clock

    @property
    def capabilities(self) -> ExchangeCapabilities:
        """`BINGX_CAPABILITIES`. Constant for the life of the instance."""
        return BINGX_CAPABILITIES

    async def candidate_symbols(self) -> Sequence[str]:
        """Nothing: one query without a symbol answers every symbol (V3; probe)."""
        return ()

    async def fetch_fill_page(
        self,
        window: FillWindow,
        *,
        cursor: str | None,
        symbol: str | None,
    ) -> FillPage:
        """One page of the account's spot fills inside `window`, from `cursor` onwards.

        1. A caller's mistake is refused before any request: a `symbol` (BingX needs none),
           a window longer than `max_query_window`, a cursor that is not a time inside the
           window. `assemble_fill_page` checks the first two again; checking here means a
           caller's bug costs no signed request.
        2. The query is built (`build_fills_query`), signed and sent.
        3. The answer is classified (`unwrap_envelope`).
        4. The page is parsed, checked against the cursor and assembled
           (`parse_fills_page`).

        Raises:
            ValueError: the caller's mistake, as in step 1.
            ExchangeError: one of the seven classes, and only those.
        """
        _refuse_caller_mistakes(window, cursor=cursor, symbol=symbol)
        query = build_fills_query(window, cursor=cursor, timestamp_ms=epoch_ms(self._clock()))
        fills = await self._read_fills(query)
        return parse_fills_page(fills, window=window, cursor=cursor)

    async def _read_fills(self, query: str) -> object:
        """Sign `query` and send it, exactly as signed, with the signature last."""
        signature = hmac_sha256_hex(self._api_secret, query)
        url = f"{BINGX_API_URL}{FILLS_PATH}?{query}&{SIGNATURE_PARAM}={signature}"
        headers = {API_KEY_HEADER: self._api_key.get_secret_value()}
        return await self._get(url, headers)

    async def _get(self, url: str, headers: Mapping[str, str]) -> object:
        """Send one GET through the shared client and classify the answer.

        The URL is passed whole, never through `params=`, so the query string sent is byte
        for byte the one that was signed. The request is labelled `exchange_fills`, so the
        transport logs `https://open-api.bingx.com/exchange_fills` and never the path or the
        query, which here carries the signature.

        The three `except` arms are `BitgetProvider._get`'s, for its reasons: an
        `httpx.LocalProtocolError` quotes the refused header value, which here is the key,
        so it becomes `ExchangeInvalidRequestError` raised after the block has closed; an
        `httpx.DecodingError` is not a `TransportError`, so it is named, and becomes
        `ExchangeUnavailableError` with no cause; any other `httpx.TransportError` is
        `ExchangeUnavailableError` chained from it, and its message carries no URL.

        Raises:
            ExchangeInvalidRequestError: h11 refused the request before sending it.
            ExchangeUnavailableError: the request got no answer, or its body could not be
                decompressed.
            ExchangeError: whatever `unwrap_envelope` makes of the answer.
        """
        refused_locally = False
        undecodable = False
        try:
            response = await self._client.get(
                url, headers=headers, extensions={ENDPOINT_EXTENSION: EXCHANGE_FILLS}
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
        message = "BingX lists fills for every symbol at once; no symbol may be given."
        raise ValueError(message)
    if window.duration > BINGX_CAPABILITIES.max_query_window:
        message = "The window is longer than BingX's max_query_window."
        raise ValueError(message)
    _start_ms(window, cursor)


def _end_ms(window: FillWindow) -> int:
    """`endTime`: the last millisecond of `[since, until)`, which the venue treats inclusively.

    Raises:
        ValueError: the window ends at or before the epoch, so it has no millisecond to ask.
    """
    until_ms = epoch_ms(window.until)
    if until_ms <= 0:
        message = "The window must end after the epoch."
        raise ValueError(message)
    return until_ms - 1


def _start_ms(window: FillWindow, cursor: object) -> int:
    """`startTime`: the cursor when there is one, `since` otherwise, never before the epoch.

    A cursor from the caller must be canonical digits, `\\A(0|[1-9][0-9]{0,14})\\Z`, and lie
    inside `[since, until - 1 ms]`: every cursor this provider issues does. Anything else is a
    `ValueError`, naming no value, because the cursor came from the caller.
    """
    since_ms = max(epoch_ms(window.since), 0)
    end_ms = _end_ms(window)
    if cursor is None:
        return since_ms
    if isinstance(cursor, str) and _CURSOR.match(cursor) is not None:
        moment = int(cursor)
        if since_ms <= moment <= end_ms:
            return moment
    message = (
        "The cursor must be a time this provider issued for this window: epoch milliseconds "
        "in canonical digits, at or after the window's start and before its end."
    )
    raise ValueError(message)


def _without_trailing_zeros(value: Decimal) -> Decimal:
    """`value` in plain notation with no zero after the point that says nothing.

    Built from the tuple, so no context is consulted and nothing is rounded: `1.500` becomes
    `1.5`, `1.000` becomes `1`, `1.2E+3` becomes `1200`.
    """
    sign, digits, exponent = value.as_tuple()
    # An `int` on a finite Decimal; `from_binary_float` has refused the other kinds.
    places = int(exponent)
    coefficient = list(digits)
    if places > 0:
        coefficient.extend([0] * places)
        places = 0
    while places < 0 and len(coefficient) > 1 and coefficient[-1] == 0:
        coefficient.pop()
        places += 1
    return Decimal((sign, tuple(coefficient), places))


def _code_of(body: str | bytes) -> object:
    """The `code` of an error body, if the body is a JSON object; `None` otherwise."""
    _, document = _decoded(body)
    return document.get("code") if isinstance(document, dict) else None


def _decoded(body: str | bytes) -> tuple[bool, object]:
    """`(True, document)` if the body is JSON, `(False, None)` if it is not. Never raises.

    The caller raises *after* this returns, so its exception has no `__context__`, for the
    reason `bitget._decoded` gives.
    """
    try:
        return True, decode_json(body)
    except ProviderResponseError:
        return False, None


def _fill_items(fills: object) -> list[dict[str, object]]:
    """The fill objects of a page, after its shape and its raw count are checked."""
    if not isinstance(fills, list):
        detail = "data.fills must be an array of fills"
        raise ExchangeSchemaError(detail)
    if len(fills) > PAGE_LIMIT:
        detail = (
            f"the page carries {len(fills)} fills, more than the limit of {PAGE_LIMIT} it "
            "was asked for"
        )
        raise ExchangeSchemaError(detail)
    items: list[dict[str, object]] = []
    for item in fills:
        if not isinstance(item, dict):
            detail = "every element of data.fills must be a JSON object"
            raise ExchangeSchemaError(detail)
        items.append(item)
    return items


def _required(document: Mapping[str, object], key: str) -> object:
    """`document[key]`, or a schema error naming `key` when it is absent."""
    if key not in document:
        detail = f"{key} is missing"
        raise ExchangeSchemaError(detail)
    return document[key]


def _require_symbol(item: Mapping[str, object]) -> tuple[str, str, str]:
    """The fill's `symbol`, as the venue spells it, with its base asset and its quote asset.

    Split on the **last** hyphen: the quote after it must match `_QUOTE`, and the base
    before it is 1 to `MAX_BASE_LENGTH` characters with no whitespace, no `C*` character and
    nothing that does not encode as UTF-8. `STRK-OLD-USDT` is base `STRK-OLD`, quote `USDT`.
    The length is checked first, so a long string costs nothing to refuse.
    """
    value = _required(item, "symbol")
    if isinstance(value, str) and len(value) <= MAX_BASE_LENGTH + 1 + _MAX_QUOTE_LENGTH:
        base, hyphen, quote = value.rpartition("-")
        if hyphen and _QUOTE.match(quote) is not None and _is_base_asset(base):
            return value, base, quote
    detail = (
        "symbol must be BASE-QUOTE: a quote of 1 to 20 upper-case ASCII letters and digits "
        f"after the last hyphen, and a base of 1 to {MAX_BASE_LENGTH} characters before it, "
        "with no whitespace or control character"
    )
    raise ExchangeSchemaError(detail)


def _is_base_asset(base: str) -> bool:
    """Whether `base` is 1 to `MAX_BASE_LENGTH` characters a base asset may hold.

    Refused: whitespace, and every character in a Unicode `C*` category -- control, format,
    surrogate, private use, unassigned. Everything else is accepted, `$ . ( ) _ -` and
    non-ASCII letters included, because the venue's own list holds all of them.

    **Refusing `Cs` is what makes the base encode as UTF-8.** A `str` fails to encode only
    through a lone surrogate, which is category `Cs`, so no separate UTF-8 check is needed
    here, and `NormalizedFill` and the database driver get text they can encode.
    """
    if not 1 <= len(base) <= MAX_BASE_LENGTH:
        return False
    return not any(char.isspace() or unicodedata.category(char).startswith("C") for char in base)


def _require_id(value: object, *, field: str) -> str:
    """An id the venue sent as a JSON integer from 1 to `MAX_TRADE_ID`, as canonical digits.

    `type(...) is int`, so a `bool` is refused, and so is a `Decimal`, which is what a number
    written with a point or an exponent decodes to. The bound is compared, not parsed, so a
    long integer costs nothing to refuse.
    """
    if type(value) is int and 0 < value <= MAX_TRADE_ID:
        return str(value)
    detail = f"{field} must be a JSON integer from 1 to 2**63 - 1"
    raise ExchangeSchemaError(detail)


def _require_side(value: object) -> FillSide:
    """`isBuyer`: exactly `true` is a buy and exactly `false` a sell."""
    if value is True:
        return FillSide.BUY
    if value is False:
        return FillSide.SELL
    detail = "isBuyer must be true or false"
    raise ExchangeSchemaError(detail)


def _require_executed_at(value: object) -> datetime:
    """`time` as an instant: epoch milliseconds, as a JSON integer (V3: `int64`)."""
    if type(value) is int:
        try:
            return datetime_from_epoch_ms(value)
        except ExchangeSchemaError:
            pass
    # Raised after the handler has closed, so it carries no context: the refusal above says
    # nothing this one does not.
    detail = "time must be a JSON integer count of epoch milliseconds, at or after the epoch"
    raise ExchangeSchemaError(detail)


def _parse_fee(item: Mapping[str, object]) -> tuple[Decimal, str | None]:
    """`commission` and `commissionAsset` as `(fee_amount, fee_asset)`."""
    commission = require_fill_amount(_required(item, "commission"), field="commission")
    if commission > 0:
        detail = (
            "commission is positive; this venue reports a fee paid as a negative number, and a "
            "positive one is refused rather than recorded as a rebate"
        )
        raise ExchangeSchemaError(detail)
    rounded = from_binary_float(commission)
    fee_amount = rounded.copy_abs() if rounded.is_zero() else rounded.copy_negate()
    asset = item.get("commissionAsset")
    if asset is None or asset == "":
        if not fee_amount.is_zero():
            detail = "commissionAsset is required when the commission is not zero"
            raise ExchangeSchemaError(detail)
        return fee_amount, None
    if not isinstance(asset, str) or not asset.strip():
        detail = "commissionAsset must be a non-blank string"
        raise ExchangeSchemaError(detail)
    if not _encodes_as_utf8(asset):
        detail = "commissionAsset does not encode as UTF-8"
        raise ExchangeSchemaError(detail)
    return fee_amount, asset


def _encodes_as_utf8(value: str) -> bool:
    """Whether `value` encodes as UTF-8: in practice, whether it holds a lone surrogate.

    A predicate rather than a raise, so the caller's refusal has no `__context__`: a
    `UnicodeEncodeError` keeps the whole string in its `args`.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
