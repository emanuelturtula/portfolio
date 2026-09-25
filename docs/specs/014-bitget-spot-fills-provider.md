# 014 — Bitget spot fills provider

Issue: #13
Status: implementing

## Problem

The exchange seam from #12 has nothing behind it. The owner's Bitget spot trades cannot be
read, so #15 has no provider to drive and M4 has no fills to compute a cost basis from. This
issue adds the first venue: a read-only, signed import of spot fills from Bitget, built
against the vendor's live documentation.

## What the live documentation says (read 2026-09-25)

The issue asked for the endpoint, the cursor, the retention and the v2-versus-UTA question to
be confirmed before any code. They were, and the answers shape the design.

**Where the docs live.** Every old `https://www.bitget.com/api-doc/...` URL now redirects to
the UTA introduction. The Classic (v2) documentation is at `/docs/catalog/classic-*` and
`/docs/classic/*`, with a static copy under `/legacy-docs/classic/...` whose content matches.

| Fact | Documented as | Source |
|---|---|---|
| endpoint | `GET /api/v2/spot/trade/fills` on `https://api.bitget.com` | Get Fills |
| parameters | `symbol`, `orderId`, `startTime`, `endTime`, `limit`, `idLessThan`, **all optional** (`symbol` "changed from required to optional", changelog 2025-03-27) | Get Fills, classic changelog |
| `idLessThan` | "the value input should be the **tradeId** of the corresponding interface", pages to older data | Get Fills |
| filters compose | "verification order ... `id` > `startTime` + `endTime` > `idLessThan`": the range narrows first, the cursor pages inside it | Classic Introduction |
| `limit` | default 100, max 100 | Get Fills |
| span | "The interval between startTime and endTime must not exceed 90 days" | Get Fills |
| retention | "It only supports to get the data within 90days" | Get Fills |
| rate limit | 10 requests/s per UID; 1/s for a copy-trading "trader"; 6000/min per IP overall; a triggered limit "takes 5 minutes to recover" | Get Fills, REST intro, FAQ Q9 |
| envelope | `{"code": "00000", "msg": "success", "requestTime": <int>, "data": [...]}`; `data` is an array; no cursor object | Get Fills |
| fill fields | all strings: `userId`, `symbol`, `orderId`, `tradeId`, `orderType`, `side`, `priceAvg`, `size`, `amount`, `feeDetail{deduction, feeCoin, totalDeductionFee, totalFee}`, `tradeScope`, `cTime`, `uTime` | Get Fills |
| headers | `ACCESS-KEY`, `ACCESS-SIGN`, `ACCESS-TIMESTAMP` (ms), `ACCESS-PASSPHRASE`, plus `Content-Type: application/json` and `locale` | REST intro |
| pre-hash | `timestamp + METHOD + requestPath + "?" + queryString + body`, body empty for a GET; HMAC-SHA256, **Base64**; do not URL-encode before signing | REST intro, common error handling |
| timestamp window | "must be within 30 seconds of the API server time"; `40008` expired, `40005` invalid | REST intro, error codes |
| symbol info | `GET /api/v2/spot/public/symbols?symbol=X`, **unauthenticated**, 20/s per IP, `baseCoin`, `quoteCoin`, `status` in `offline`/`gray`/`online`/`halt` | Get Symbol Info |

**Not documented, and treated as such below:** whether `startTime`/`endTime` are inclusive;
the order of fills within a page; that `tradeId` is numeric or unique across symbols; what
`size` and `amount` are measured in (the example, `13000 x 0.0007 = 9.1`, says base and quote);
the fee's sign (the REST example is negative, the WebSocket fill channel's is positive); what
`totalFee` means when the fee is paid with BGB; and any golden signature vector (the docs'
samples use an empty secret and print no output). **`cTime` is described as "Unix second
timestamp" and the example is 13-digit milliseconds** -- contradictory.

### v2 against the Unified Trading Account

- Classic is "in maintenance mode and receives only essential updates". No retirement notice
  or date exists for v2. (v1 was shut down on 2025-11-28, announced on 2025-09-25.)
- "API Keys for UTA Unified Trading Accounts cannot access Classic Account API endpoints"
  appears in the *Bitget Broker UTA API Upgrade Notice* (2026-06-17), **addressed to broker
  partners and their clients**. For a retail account the docs do not state it outright. The
  UTA upgrade guide says an existing v2 key "automatically gains UTA access" and maps v2
  fills to `GET /api/v3/trade/fills`. That endpoint differs in every dimension that matters:
  `category=SPOT`, an opaque `cursor`, a 30-day window, 20 requests/s, and a `data.list` of
  differently named fields. (Corrected on 2026-09-25, after backend-dev could not find the
  sentence in the pages first cited.)
- Since **2026-09-15** Bitget has been migrating eligible Classic accounts to UTA
  automatically. **An account with an API key linked is not eligible.** A main account can
  switch back.
- **The owner's account was confirmed Classic on 2026-09-25**, from the app: separate Spot,
  Futures and Margin tabs and a banner offering the upgrade.

So v2 is correct for this owner, and this issue builds v2 only. UTA is a follow-up issue,
filed when the pull request opens, and `docs/operations.md` tells the operator not to accept
the upgrade.

## Scope

- `providers/exchanges/bitget.py`: `BitgetProvider`, implementing `ExchangeProvider` over the
  v2 fills endpoint, with its capabilities, its error map, its request signing, its fill
  parser and a per-instance symbol cache fed by the public symbol-info endpoint.
- `providers/exchanges/registry.py`: `exchange_providers(client, *, settings=None)`, the
  table of configured venues. The shape of `providers/prices/registry.py`: a venue with no
  credentials is **absent, not built**.
- Three settings: `PORTFOLIO_BITGET_API_KEY`, `PORTFOLIO_BITGET_API_SECRET`,
  `PORTFOLIO_BITGET_API_PASSPHRASE`, all `SecretStr | None`, all or none.
- Two endpoint labels in `ENDPOINT_LABELS`: `exchange_fills` and `exchange_symbol`.
- `docs/providers.md` records every fact above with its date and source; `docs/operations.md`
  gains a section on the Bitget key.

## Non-goals

| Not here | Where it belongs |
|---|---|
| UTA (`/api/v3/trade/fills`) | a follow-up issue, filed with the pull request |
| BGB fee deduction (`feeDetail.deduction` other than `"no"`) | refused loudly here; supported when a real fill shows what the fields mean |
| calling the provider from anything: sync, scheduler, checkpoints, `exchange_accounts` rows | #15 |
| detecting a trade id that collides across windows | #15 (see Handed on) |
| re-signing a request per transport attempt | not planned; see "The transport replays a signed request" |
| a credential health check or a server-time skew probe | not planned; #15 learns about a bad key or a skewed clock from a sync |
| anything under `api/`, `services/` or `frontend/` | #15, #16 |

## Design

### Capabilities

```python
ExchangeCapabilities(
    exchange_key=ExchangeKey.BITGET,
    retention=timedelta(days=90),
    max_query_window=timedelta(days=30),
    page_size=100,
    cursor_kind=CursorKind.TRADE_ID_BEFORE,
    rate_limit=RateLimit(max_requests=10, per_ms=1000),
    requires_symbol=False,
)
```

- **`max_query_window` is 30 days, below the documented 90, on purpose.** The request is
  widened by a millisecond (below), so a 90-day window would be sent as 90 days and a
  millisecond, and a vendor that states its limit in days may measure it by the calendar.
  Thirty days costs three windows for a full-retention first sync, and it is UTA's limit, so
  the follow-up does not change the number #15 plans around.
- **`retention` is the documented 90 days.** Error `40704` says "the last three months", and
  three calendar months can be 89 days. If the venue refuses the oldest window, `40704` is
  mapped to `ExchangeRetentionWindowError` and #15 clamps further. Recorded under Risks.
- `requires_symbol` is `False`: `symbol` is optional, so one query covers every symbol and
  `candidate_symbols()` returns an empty tuple.
- `rate_limit` is declarative. The shared transport's per-host floor is one request a second,
  which is stricter, so nothing here can exceed the documented limit.

### One page, step by step

`fetch_fill_page(window, *, cursor, symbol)`:

1. **Refuse a caller's mistake before any request** (`ValueError`): a `symbol` that is not
   `None`, a window longer than `max_query_window`, a `cursor` that is not a canonical trade
   id (below). `assemble_fill_page` checks the first two again; checking here means a caller
   bug costs no signed request.
2. **Build the query**, keys in ascending order, values all ASCII digits so nothing needs
   encoding: `endTime`, `idLessThan` (only with a cursor), `limit=100`, `startTime`.
   Ascending order satisfies both readings of the docs ("the parameters after the `?`" and
   the samples' "sorted in ascending alphabetical order"), because what is sent *is* sorted.
3. **Sign and send.** `timestamp = epoch_ms(clock())`, `prehash = f"{timestamp}GET{FILLS_PATH}?{query}"`,
   `ACCESS-SIGN = hmac_sha256_base64(credentials.api_secret, prehash)`. The URL is sent with
   exactly that query string, and the test's fake venue verifies the signature against the
   bytes it received.
4. **Classify the answer** (below). Anything but HTTP 200 with `code == "00000"` raises.
5. **Parse every fill** into a `NormalizedFill` (below), resolving each new symbol through
   the symbol cache.
6. **Check the page against the cursor** (below), and compute `next_cursor`.
7. **Drop the two boundary milliseconds** (below), then hand the rest to
   `assemble_fill_page`, which enforces the contract.

### Window bounds: widen by a millisecond, then filter

Whether `startTime` and `endTime` are inclusive is not documented. A guess is wrong in one
direction or the other, and either way #15 meets the same error at the same boundary every run.
This is the "two grids" lesson from spec 012:

| Guess | If the venue's bound is the other kind |
|---|---|
| send `[since, until]` as is | an exclusive `startTime` loses fills at exactly `since`, and nothing ever fetches them |
| send `until - 1` | an exclusive `endTime` loses fills at exactly `until - 1` |

So the request asks for **`startTime = epoch_ms(since) - 1`** and **`endTime = epoch_ms(until)`**,
which covers `[since, until)` under all four readings. Before assembling, the provider drops a
fill whose `executed_at` is exactly `since - 1 ms` or exactly `until`: under an inclusive
reading those two belong to the neighbouring windows, which fetch them. **A fill anywhere else
outside the window is not dropped**. It goes to `assemble_fill_page`, which refuses it as an
answer to a question nobody asked. The drop happens after parsing, so a malformed fill on the
edge still fails the page. The page-size check is applied to the **raw** count, before the
drop, so a 101-fill page cannot hide behind a dropped edge fill.

`since` at the epoch itself is sent as `0`, not `-1`. Retention makes this unreachable in
practice, but the arithmetic has an edge, so it gets a rule and a test.

### The cursor is the smallest trade id, and it must strictly decrease

Trap 1 of the issue: `idLessThan` takes a **`tradeId`**. Sending an `orderId` pages from the
wrong sequence, and a venue that answers the same page again loops forever.

- **A trade id is canonical digits**: `\A[1-9][0-9]{0,18}\Z`, a positive integer that fits a
  signed 64-bit integer, with no leading zero. So string identity and numeric identity agree,
  and nothing near CPython's 4300-digit limit is ever converted. A `tradeId` of any other
  shape is an `ExchangeSchemaError`, because numeric ids are what the example shows and are
  not documented. `external_trade_id` is the id as sent.
- **`next_cursor` is the smallest `tradeId` on the raw page** when the raw page holds `limit`
  fills, and `None` otherwise. It is the smallest rather than the last, because the order
  within a page is undocumented. If the venue sorts by trade id, descending, the two are the
  same. If it sorts by time and ids are not monotonic in time, the smallest is still the only
  value for which "everything less than this" is exactly what has not been seen.
- **With a cursor, every fill on the page must have `tradeId < cursor`**, or the page is an
  `ExchangeSchemaError`: the venue ignored `idLessThan`. With the rule above, each
  `next_cursor` is strictly below the cursor before it. A strictly decreasing sequence of
  positive integers is finite, so **pagination terminates by construction**. That covers a
  repeated cursor and a cycle (A -> B -> A) alike, which `require_cursor_advanced` alone
  cannot promise.

### Parsing a fill

| `NormalizedFill` | From | Rule |
|---|---|---|
| `external_trade_id` | `tradeId` | canonical digits, as above |
| `external_order_id` | `orderId` | a string or absent; UTF-8 checked by `NormalizedFill` |
| `symbol` | `symbol` | `\A[A-Z0-9]{1,40}\Z`, checked **before** it is used to build the symbol-info URL |
| `base_asset`, `quote_asset` | the symbol cache | `baseCoin`, `quoteCoin` |
| `side` | `side` | exactly `"buy"` or `"sell"` |
| `quantity` | `size` | `require_fill_amount` |
| `price` | `priceAvg` | `require_fill_amount` |
| `quote_quantity`, `quote_quantity_derived` | `amount` | reported, `False`; when `amount` is absent, `null` or `""`, `derive_quote_quantity(quantity, price)` and `True` |
| `fee_amount` | `feeDetail.totalFee` | **negated**: see below |
| `fee_asset` | `feeDetail.feeCoin` | required when the fee is not zero; `None` when it is zero and the coin is absent or `""` |
| `executed_at` | `cTime` | `datetime_from_epoch_ms`: milliseconds, as the example shows |
| `raw_payload` | the fill object | `encode_raw_payload(item)`: the element of `data`, never the envelope |

**The fee sign.** The documented REST example is a buy of 0.0007 BTC with
`totalFee: "-0.0000007"` in BTC, which is 0.1% of the quantity. That is a fee paid, reported
negative. `NormalizedFill` counts a fee paid as positive, so `fee_amount = -totalFee`. **A
`totalFee` above zero is refused** as an `ExchangeSchemaError`. The WebSocket channel reports
the same field positive, so if REST ever does the same, treating it as a rebate would record
every fee as income, silently. A refusal is loud, and a real spot rebate needs a rule written
from evidence.

**BGB deduction is refused.** A `feeDetail.deduction` other than `"no"` is an
`ExchangeSchemaError` naming the field. What `totalFee` and `totalDeductionFee` hold when the
fee is paid in BGB is not documented for this endpoint, and a guess would put the wrong
amount in the wrong asset.

**`cTime` in seconds would fail loudly.** A ten-digit value reads as 1970, lands outside the
window, and `assemble_fill_page` refuses the page. No extra rule is needed; a test pins it.

A missing field, or one of the wrong JSON type, is an `ExchangeSchemaError` naming the
field, never the value. `KeyError`, `TypeError` and `AttributeError` never escape the
provider. The seven classes are all it raises.

### The symbol cache

`NormalizedFill` needs `base_asset` and `quote_asset`, and `"BTCUSDT"` does not say where one
ends. Splitting it with a list of known quote coins is a guess that fails on the first new
one. The symbol-info endpoint answers exactly this question.

- `GET /api/v2/spot/public/symbols?symbol=<symbol>`, **unsigned** (it is public: no
  credential header is sent to it), labelled `exchange_symbol`.
- Asked **once per distinct symbol per provider instance**, lazily, and cached in a dict on the
  instance. A symbol already seen costs nothing.
- The answer must be HTTP 200, `code == "00000"`, and a `data` array holding **exactly one**
  entry whose `symbol` equals the one asked about, with non-blank `baseCoin` and `quoteCoin`.
  Anything else is an `ExchangeSchemaError`, and a non-200 is classified like the fills call.
  This is the `align_balances` correlation rule: an answer about another symbol is refused,
  not used.
- A symbol the endpoint does not know (a delisting, for instance) fails the page. Recorded
  under Risks.

### Classifying an answer

A response is a success only if it is HTTP 200 **and** its body is a JSON object whose `code`
is the string `"00000"`. Otherwise:

- **A transport failure** (`httpx.TransportError`) raises `ExchangeUnavailableError`, chained
  `from` the transport error, whose message carries no query.
- **Any other failure** raises
  `exchange_error(status, venue_code_of(body.get("code")), error_map=BITGET_ERROR_MAP,
  retry_after_ms=parse_retry_after(...))`, `from None`. A body that does not parse, or is not
  an object, contributes no code, and the status decides alone: a 502 carrying HTML is
  unavailable, not a schema error.
- **HTTP 200 with a body that does not parse** or does not have the documented shape raises
  `ExchangeSchemaError`, `from None`. The cause would be a parser error about the body, and
  the body is what the provider must not repeat.
- **Never `raise_for_status()`, and never chain from `httpx.HTTPStatusError`.** Its message
  carries the full URL.

`BITGET_ERROR_MAP` names only what differs from the fallbacks. Every key is `(None, code)`,
because the docs tie no code to a status:

| Codes | Class | Why |
|---|---|---|
| `40006` invalid key, `40037` / `40041` key does not exist, `40012` key or passphrase incorrect, `40036` passphrase wrong, `40009` signature error, `40038` / `40018` IP not allowed | `ExchangeAuthError` | the owner has to fix the key. A signature error is a wrong secret once the pre-hash is proven by the golden vectors |
| `40014`, `40025`, `40040` permission | `ExchangeInsufficientScopeError` | the key lacks read permission |
| `40008` timestamp expired, `40005` invalid timestamp | `ExchangeUnavailableError` | **not auth**: the transport can replay a signed request, and a skewed clock is not a bad key. A fresh request later is signed anew |
| `429` | `ExchangeRateLimitedError` | the in-band spelling of the throttle |
| `40704` "only the last three months" | `ExchangeRetentionWindowError` | #15 clamps further |
| `00001`, `40705` span too long, `40707` start after end, `40017`, `40019`, `40020`, `40034` parameter errors, `40102` symbol does not exist | `ExchangeInvalidRequestError` | a request we built wrongly. Mapped by code so it is not a schema error when it arrives on a 200 |
| `45001`, `40725`, `40808`, `40015` | `ExchangeUnavailableError` | the FAQ says these occur during deploys and to retry |

The header-missing codes (`40001`, `40002`, `40003`, `40011`) are deliberately **not** mapped.
The provider always sends every header, so one of them means a bug in this code, and the
status fallback (a 400 is an invalid request) says "needs a person" without marking the
account `auth_failed`.

### The transport replays a signed request, and that is accepted

`RetryingTransport` retries a `GET` that got a 429, a 5xx or no answer by resending the same
request: same timestamp, same signature. The venue allows 30 seconds of skew. Without a
`Retry-After`, the backoff is under 250 ms and then under 500 ms, well inside that window. With
a `Retry-After` near the 30-second cap, the replay can arrive expired. It is then refused with
`40008`, which is mapped to `ExchangeUnavailableError`, and the next run signs a fresh request.
Re-signing per attempt would mean a transport that knows about signing, or retries owned by the
provider. Neither is worth it for a 429 that, per the FAQ, takes five minutes to clear anyway.
A test pins the replay, so the decision is visible.

### Credentials and settings

- `Settings` gains `bitget_api_key`, `bitget_api_secret` and `bitget_api_passphrase`, all
  `SecretStr | None = None`.
- `_refuse_unsafe_configuration` refuses **a partial set**: some set and some not. The message
  names the missing variables and never a value. It also refuses **a blank value** in any of
  the three, naming the variable. Unlike the CoinGecko key, a blank here cannot reach the
  vendor as a 401, because `Credentials` refuses a blank at construction. Refusing at startup
  is the same fact, surfaced where the pipeline rolls back.
- `bitget_credentials(settings) -> Credentials | None` builds `Credentials` with the
  passphrase, or returns `None` when none of the three is set.
- `BitgetProvider(client, credentials, *, clock=utc_now)` refuses `Credentials` without a
  passphrase (`ValueError`, naming the field). The API key and the passphrase are read out of
  their `SecretStr` only while the headers are built. The secret is only ever unwrapped inside
  `signing.py`.
- `exchange_providers(client, *, settings=None) -> Mapping[ExchangeKey, ExchangeProvider]`
  holds Bitget only when configured, as a read-only mapping. The check is `is None`, as in the
  price registry.

### Logging

The provider has **no log call**. The transport logs `https://api.bitget.com/exchange_fills`
or `.../exchange_symbol`, never a path, a query or a header. The signature, the key and the
passphrase travel in headers, which nothing logs. The message of every exception the provider
raises is built by the taxonomy, which carries no body, no URL and no header.

### Modules

| Path | Change |
|---|---|
| `backend/src/portfolio/providers/exchanges/bitget.py` | new: constants (`BITGET_API_URL`, `FILLS_PATH`, `SYMBOLS_PATH`, header names), `BITGET_CAPABILITIES`, `BITGET_ERROR_MAP`, `bitget_credentials`, `BitgetProvider`, and the pure parsing functions it uses, exposed so they can be tested without HTTP |
| `backend/src/portfolio/providers/exchanges/registry.py` | new: `exchange_providers` |
| `backend/src/portfolio/providers/exchanges/__init__.py` | docstring: Bitget exists; BingX is #14 |
| `backend/src/portfolio/providers/http.py` | `EXCHANGE_FILLS`, `EXCHANGE_SYMBOL` and their places in `ENDPOINT_LABELS` |
| `backend/src/portfolio/config.py` | the three settings and the two refusals |
| `docs/providers.md` | a Bitget section: the facts table above with its date and URLs, the undocumented list, the UTA finding, the decisions below; the "Not confirmed" table and "Not done yet" updated |
| `docs/operations.md` | a new section: creating a **read-only** key, the three variables in the host-local `secrets.env`, keeping the account Classic, and what `40008` in a sync error means |

### Rejected alternatives

| Rejected | Why |
|---|---|
| UTA v3 alongside v2, chosen by a setting | the owner's account is Classic; v3 differs in every dimension, and a second, unused code path is where rot starts |
| `next_cursor` = the last fill on the page | the order within a page is undocumented; the smallest id is correct under either order |
| trusting `require_cursor_advanced` alone | it catches a repeat, not a cycle; strict decrease catches both |
| sending the window unwidened | inclusive bounds are undocumented; either guess loses or refuses fills at a boundary on every run |
| splitting the symbol by a list of quote coins | a guess that fails on the first new quote coin, silently if it fails by matching the wrong one |
| namespacing trade ids by symbol | the endpoint pages every symbol with one cursor over `tradeId`, which only works if trade ids are one sequence per account. A collision within a page is refused; one across windows is #15's (Handed on) |
| mapping a timestamp error to auth | #15 would mark a working key `auth_failed` |
| re-signing per transport attempt | see above |

## API contract

None. No endpoint changes; `openapi.json` is untouched.

## Data model

None. No migration.

## Acceptance criteria

From the issue, numbered, with this spec's reading where one is needed:

1. Endpoint path, parameters, cursor semantics, retention window and the v2-versus-UTA
   question are confirmed against live documentation and recorded in `docs/providers.md`.
   *Reading:* recorded with the date and the source URL, and with the undocumented points
   listed as such. The owner's account type is recorded as confirmed.
2. Signature verified by golden vectors.
   *Reading:* Bitget publishes none. The vectors are computed outside this code, with the
   pre-hash algorithm of Bitget's official Python SDK and `openssl`, and the command sits
   beside each literal. The fake venue also recomputes the signature of every request it
   receives with the standard library's `hmac`, not with `signing.py`.
3. Multi-page pagination terminates; a cursor that stops advancing raises rather than looping.
4. 401, 403, 429 and 5xx each map to the right taxonomy class.
5. Fills normalize to exact `Decimal` values.
6. The retention clamp surfaces an `effective_since` rather than throwing.
   *Reading:* `clamp_to_retention` with Bitget's capabilities, and the provider accepting a
   window opened at the clamped `effective_since`.
7. The signature never appears in any log record.
   *Reading:* nor the API key, the passphrase or the secret, through the production log
   pipeline, on a success, a refusal, a retried request and a transport failure, and in the
   rendering of every exception the provider raises.

Added by this spec:

8. The Bitget credentials are three settings, all or none, never blank, and a venue without
   credentials is absent from `exchange_providers`.
9. Every failure is one of the seven classes, whatever the venue sends.

## Test plan

Every expected value comes from outside the code under test. Every absence check has a
positive companion. Response bodies are **hand-written strings**, never `json.dumps` of a
Python value, as `tests/providers/prices/harness.py` explains.

A fake Bitget venue (`backend/tests/providers/exchanges/bitget_harness.py`) behind an
`httpx.MockTransport` holds a scripted list of fills. It implements `startTime`/`endTime`
(inclusive or exclusive, switchable), `idLessThan` over `tradeId`, `limit`, and the symbol
endpoint, and it **verifies every signed request** with `hmac` + `base64` directly. The
fills' `orderId`s are chosen so that paging by `orderId` would return a page already seen.

| # | Test | Must assert |
|---|---|---|
| 1 | `tests/providers/test_documentation.py::test_the_document_records_the_confirmed_bitget_facts` | `docs/providers.md` names `/api/v2/spot/trade/fills`, `idLessThan`, `tradeId`, `90`, `UTA`, `/api/v3/trade/fills`, the date, and at least one `bitget.com/docs` URL |
| 1 | `...::test_the_document_lists_what_bitget_does_not_document` | inclusive bounds, page order, fee sign and BGB deduction are named as undocumented |
| 2 | `tests/providers/exchanges/test_bitget.py::test_the_signature_matches_the_sdk_golden_vector` | `1684814440729GET/api/v2/spot/trade/fills?idLessThan=12345678910&limit=100&symbol=BTCUSDT` under the synthetic secret signs to `EhrSzSzM7SVAe3w2KKc1QWKmBHbLWu2l/jdM3Qa7mxY=`, with the `openssl` command beside it |
| 2 | `...::test_the_signature_of_the_request_the_provider_sends` | a fixed clock and window produce the pre-hash written by hand and the Base64 literal computed with `openssl` (`...?endTime=1695900000000&idLessThan=12345678910&limit=100&startTime=1695800000000` at `1684814440729` gives `incJ9meeHk+l8ZNsbae3jCvckiICFIAPvapsdTcQ6AA=`) |
| 2 | `...::test_every_request_carries_the_four_access_headers_and_a_verifiable_signature` | the fake's independent verification passes; the timestamp header is the clock's millisecond; the symbol endpoint receives **no** access header |
| 2 | `...::test_the_query_is_sent_exactly_as_signed` | `request.url.query` bytes equal the signed query; keys ascending |
| 3 | `...::test_a_multi_page_walk_returns_every_fill_once_and_stops` | 250 fills, three requests, 250 distinct ids, `next_cursor` `None` on the last page |
| 3 | `...::test_the_cursor_is_the_smallest_trade_id_never_the_order_id` | each `idLessThan` sent equals the smallest `tradeId` of the previous page, with the fake's order ids chosen so the `orderId` mutation loops or skips |
| 3 | `...::test_the_cursor_is_the_smallest_id_whatever_order_the_page_is_in` | a page served ascending, or shuffled, gives the same `next_cursor` |
| 3 | `...::test_a_venue_that_ignores_the_cursor_raises_instead_of_looping` | the same full page served again: `ExchangeSchemaError` on the second request, and the request count is bounded |
| 3 | `...::test_a_page_with_an_id_at_or_above_the_cursor_is_refused` | one fill equal to the cursor, one above |
| 3 | `...::test_a_short_page_has_no_next_cursor` | 99 fills: `None`; 100: a cursor; 0: `None` |
| 3 | `...::test_a_malformed_trade_id_is_a_schema_error` | `"0"`, `"012"`, `"12a"`, twenty digits, 5000 digits, an int, `null` |
| 3 | `...::test_a_malformed_cursor_from_the_caller_is_a_value_error_and_sends_nothing` | the fake saw zero requests |
| 4 | `...::test_each_status_maps_to_its_taxonomy_class` | 401 -> auth, 403 -> auth, 429 -> rate-limited with `retry_after_ms`, 500 and 503 -> unavailable, each the exact class, with an HTML body and with a JSON body |
| 4 | `...::test_each_in_band_code_maps_to_its_class` | one per row of the map table, on HTTP 400 and on HTTP 200 |
| 4 | `...::test_a_timestamp_error_is_never_an_auth_error` | `40008` and `40005`: `ExchangeUnavailableError`, and not an `ExchangeAuthError` |
| 4 | `...::test_a_header_missing_code_is_not_mapped` | `40001` on a 400 -> invalid request, on a 200 -> schema |
| 4 | `...::test_a_transport_failure_is_unavailable_and_chains_only_the_transport_error` | `__cause__` is the `httpx.TransportError`; no exception in the chain is an `httpx.HTTPStatusError` |
| 4 | `...::test_the_transport_replays_the_same_signed_request` | a 429 then a 200: both requests carry the same timestamp and signature (the decision, pinned) |
| 5 | `...::test_the_documented_example_normalizes_exactly` | the docs' example fill verbatim: `Decimal("0.0007")`, `Decimal("13000")`, `Decimal("9.1")`, fee `Decimal("0.0000007")` in `BTC`, `executed_at` from `1695865232579`, `quote_quantity_derived` `False`, the `raw_payload` round-trips and carries no envelope field |
| 5 | `...::test_a_missing_amount_is_derived_and_flagged` | absent, `null`, `""` |
| 5 | `...::test_a_positive_total_fee_is_refused` | and a zero fee with no coin is accepted as `None` |
| 5 | `...::test_bgb_deduction_is_refused` | `deduction: "yes"` |
| 5 | `...::test_an_amount_finer_than_the_fill_scale_is_refused` | nineteen places |
| 5 | `...::test_a_seconds_timestamp_fails_the_page` | `cTime: "1695865232"` |
| 5 | `...::test_the_interpreter_limits_are_schema_errors` | a 5000-digit amount, a fill nested 1500 deep, `1e1000000000000000000`, a `"\ud800"` symbol and trade id: every one an `ExchangeSchemaError` |
| 5 | `...::test_a_missing_or_mistyped_field_is_a_schema_error_naming_the_field` | parametrised over every required field; the value absent from the message |
| 5 | `...::test_the_symbol_is_split_by_the_venue_and_asked_once` | two pages, one symbol: one symbol request; the base and quote are the fake's |
| 5 | `...::test_a_symbol_answer_about_another_symbol_is_refused` | also: an empty `data`, two entries, a blank `baseCoin` |
| 5 | `...::test_an_unsafe_symbol_is_refused_before_a_url_is_built` | `"BTC/USDT"`, `"btcusdt"`, `"\ud800"`: no symbol request made |
| -- | `...::test_the_window_is_widened_by_a_millisecond_and_filtered` | with an inclusive fake: fills at `since - 1` and at `until` served and dropped; at `since` and `until - 1` kept. With an exclusive fake: nothing lost |
| -- | `...::test_a_fill_outside_the_widened_window_is_refused` | at `since - 2` |
| -- | `...::test_the_page_size_is_checked_before_the_edge_is_dropped` | 101 fills, one on the edge |
| -- | `...::test_a_window_at_the_epoch_sends_zero` | `startTime=0` |
| -- | `...::test_a_caller_mistake_costs_no_request` | a symbol given, a 31-day window |
| 6 | `...::test_a_request_older_than_retention_is_clamped_and_the_clamped_window_is_accepted` | 200 days back at a fixed `now`: `effective_since == now - 90 days + 5 minutes`, `clamped`; `fetch_fill_page` over `[effective_since, effective_since + 1 day)` sends `startTime` no earlier than `now - 90 days` |
| 6 | `...::test_a_retention_refusal_is_typed` | `40704` -> `ExchangeRetentionWindowError` |
| 7 | `tests/providers/exchanges/test_bitget_logging.py::test_no_credential_reaches_the_log` | through the production pipeline (the exchanges conftest's installer, **not** `capture_logs`): a success, a 401, a 429-then-200 retry and a transport failure. The signature, the API key, the passphrase and the secret sentinels are absent from stdout. Positive companion: `exchange_fills` and `provider_request_failed` are present |
| 7 | `...::test_no_credential_reaches_a_rendered_exception` | for each class the provider raises: `str`, `repr`, `args`, and `logger.exception` through the pipeline |
| 8 | `tests/providers/exchanges/test_bitget_settings.py::test_the_credentials_are_all_or_none` | each partial combination refuses; the message names the missing variables; no value appears |
| 8 | `...::test_a_blank_credential_is_refused_at_startup` | per variable |
| 8 | `...::test_an_unconfigured_venue_is_absent_not_built` | `exchange_providers` without credentials is empty and made no request; with them, holds a `BitgetProvider` |
| 8 | `...::test_the_provider_refuses_credentials_without_a_passphrase` | |
| 9 | `...::test_the_protocol_is_satisfied` | a module-level `_CONFORMS: ExchangeProvider = BitgetProvider(...)` checked by `mypy --strict` |
| -- | `tests/providers/test_http.py` (or the label tests) | `exchange_fills` and `exchange_symbol` are in `ENDPOINT_LABELS` and match `ENDPOINT_LABEL` |

**Mutations the verification must kill:**
- use the last fill's id as the cursor instead of the smallest;
- use `orderId` as the cursor;
- drop the "every id below the cursor" check;
- stop widening `startTime`, then separately stop widening `endTime`;
- drop the edge filter;
- apply the page-size check after the edge filter;
- negate the fee twice (store `totalFee` as is);
- accept a positive `totalFee`;
- accept `deduction: "yes"`;
- map `40008` to auth;
- remove `40014` from the map;
- sign the query unsorted, or sign it with its keys in a different order from the one sent;
- drop the `?` from the pre-hash;
- send an access header to the symbol endpoint;
- skip the symbol cache;
- accept a symbol-info answer for another symbol;
- treat a blank credential as absent.

## File ownership

Disjoint. Nobody edits a file on another row.

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/providers/exchanges/bitget.py`, `backend/src/portfolio/providers/exchanges/registry.py`, `backend/src/portfolio/providers/exchanges/__init__.py`, `backend/src/portfolio/providers/http.py`, `backend/src/portfolio/config.py`, `docs/providers.md`, `docs/operations.md` |
| tester | `backend/tests/**` |
| tech-lead | `docs/specs/014-*.md`, `backend/pyproject.toml`, `backend/.importlinter` |
| reviewer | nothing |

## Risks

- **None of this has met the real venue.** Measuring a signed endpoint needs a key, and rule 3
  keeps any key out of this repository and out of this work. The first real request is the
  owner's first sync after #15. Everything above is written to fail loudly, as a typed error
  naming a field, rather than to guess.
- **The owner's account could become UTA.** What a v2 call from a UTA key returns is not
  documented. A refusal is expected, and it would surface as an invalid-request or auth error.
  **An empty success would be indistinguishable from no trades**, which is why `data: null` is
  refused and why `docs/operations.md` says not to accept the upgrade. The v3 follow-up is the
  fix.
- **Retention may be "three months", not 90 days.** If the venue refuses the oldest window,
  `40704` makes it a typed `ExchangeRetentionWindowError`, and #15 clamps further.
- **A delisted symbol fails its page** if the symbol-info endpoint no longer knows it. The
  `status` enum includes `offline`, which suggests delisted pairs are still listed. Unconfirmed.
- **Trade ids are assumed numeric and one sequence per account.** A non-numeric id fails the
  page loudly. A collision across windows would be silently merged by #15's
  `ON CONFLICT DO NOTHING`, which is why detecting it is handed on.
- **The fee sign and BGB deduction** are refused rather than guessed. An owner who pays fees in
  BGB will see every page fail until a real fill shows what the fields mean. That is a loud
  failure with a named field, which is the intended direction.
- **The secret scanner.** `.gitleaks.toml` blocks `bitget_*_secret = "<16+ chars>"`. Synthetic
  test secrets are obvious and low-entropy, and never assigned to a name containing `bitget`.
  The golden-vector signatures are Base64 literals, and the tester runs the secret scan on the
  test module before committing it.

## Departures agreed during implementation

- **A vendor header could make `client.get` raise a bare `ValueError`, for every provider.**
  `parse_retry_after` and `parse_rate_limit` checked that a value was ASCII digits and then
  called `int()`, which refuses more than 4300 digits. `parse_rate_limit` runs on every
  response inside `RetryingTransport`, so a response carrying such a header escaped every
  provider's `except httpx.TransportError`, and chain `health()`'s never-raise contract.
  Measured by backend-dev and reproduced by the tech lead. It predates this issue, but
  criterion 9 cannot hold while it stands, so it is fixed here in `providers/http.py`: a
  named digit bound, past which a value is unusable (`None`). **The HTTP-date form of
  `Retry-After` had a third escape of the same kind:** a 20-digit year, hour or zone offset
  makes `parsedate_to_datetime` raise `OverflowError`, an `ArithmeticError`, which the
  `(TypeError, ValueError)` clause did not catch. That date is now read as absent too.
  backend-dev fuzzed the parser with 200,000 token combinations and found no fourth
  exception type.
- **A credential with an illegal header character leaked through a transport error.** A
  newline, a NUL, or surrounding whitespace in the API key makes h11 raise
  `httpx.LocalProtocolError` quoting the whole header value, and the design chained from
  transport errors. A non-ASCII character escaped as a bare `UnicodeEncodeError`. The API key
  and the passphrase must now be printable ASCII with no surrounding whitespace (an interior
  space is allowed). This is checked at startup (naming the variable) and in the provider's
  constructor (naming the field). A `LocalProtocolError` is translated `from None`. The secret
  is only HMAC input and stays unconstrained.
- **A trade id is the pattern and `int(value) <= 2**63 - 1`.** The pattern alone admits
  19-digit values above the signed 64-bit maximum, which the rule claims to exclude.
- **A corrupt compressed body escaped as `httpx.DecodingError`.** The tech lead measured it
  with the real client: `Content-Encoding: gzip` over a body that does not decompress raises
  while the client reads the body, above the transport, and `DecodingError` is not a
  `TransportError`. The provider now translates it to `ExchangeUnavailableError`. The same
  escape predates this issue in the chain and price providers (Esplora `health()`, Kraken
  `fetch`), and is filed as #75.
- **A settings refusal printed both ends of a credential.** Measured by the tech lead: when
  the new refusals fire, `str(ValidationError)`, which is what reaches the log when the
  container refuses to start, carried
  `input_value={'bitget_api_key': 'SENTI...ETBBBBBBBBBBBBBBBBBBBB'}`. That is the first five
  characters of the key and the last twenty of the secret. Pydantic elides the middle of the
  echoed input and keeps both ends, so `config.py`'s docstring claim that `str(exc)` carries
  no secret was false. It had been measured only against a value that fit in the elided part.
  `Settings` now sets `hide_input_in_errors=True`, which drops `input_value` from `str()` and
  `repr()`. `errors()` and `json()` still carry the whole input, which is #53's pinned hazard.
  The behaviour predates this issue for any refusal, but this issue adds three credentials,
  and refusals that fire exactly when they are set.
- **A success whose fills `data` is `null` stays a schema error. The tech lead briefly
  decided otherwise and reversed it after review.** The tolerance read `null` under
  `"00000"` as an empty page, on the argument that it can only mean "nothing". The reviewer's
  counter-case is the one that matters: a v2 call from a UTA-upgraded account is exactly
  where an undocumented answer is plausible, and an empty success there makes every window
  read as empty. #15 would then advance its checkpoints, and after 90 days that history is
  gone. A loud failure costs one fix after the first real sync; a silent empty history is
  permanent. **Loud-and-recoverable beats quiet-and-permanent**, which is the rule this
  project already applies to balances.
- **A duplicate trade id on an edge millisecond escaped the within-page check.** Found by
  review. The distinct-id rule lived in `assemble_fill_page` and so saw only the fills the
  edge drop kept: a pair sharing a `tradeId`, one at `since - 1 ms` and one inside the window,
  was accepted as one fill. The rule now runs on the raw page, beside the raw count and the
  cursor rule. This is spec 012's "a rule reasoned about alone" again: the duplicate rule
  predated the edge drop placed in front of it.
- **The public symbol endpoint can no longer produce an auth error.** Found by review. A 401
  or 403 there was classified `ExchangeAuthError`, but that call carries no credential, so a
  CDN or WAF refusing it would have made #15 mark a working key `auth_failed`. An auth-class
  answer on that call is now `ExchangeUnavailableError`, keeping its status and code.

## Handed on

- **#15:** an `ON CONFLICT DO NOTHING` hit whose stored `raw_payload` differs from the
  incoming fill's is a collision, not a re-sync, and should raise rather than drop the second
  fill. That covers a venue whose trade ids turn out not to be one sequence per account,
  whichever venue it is.
- **A follow-up issue for UTA** (`GET /api/v3/trade/fills`), filed with the pull request.
- **#75:** `httpx.DecodingError` in the chain and price providers.
- **#14:** `MAX_HEADER_DIGITS` is ten, so a 13-digit epoch-millisecond `x-ratelimit-reset`, a
  shape some venues use, is now read as absent where it used to cause a 30-second pause.
  Neither reading is right for such a header. If BingX sends one, it needs its own parser.
