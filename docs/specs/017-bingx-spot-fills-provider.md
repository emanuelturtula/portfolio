# 017 — BingX spot fills provider

Issue: #14
Status: done

## Problem

The sync from #15 imports fills from every configured venue, and so far only Bitget exists.
The owner also trades spot on BingX, so the cost of everything bought there is invisible. The
issue marks every venue fact as unconfirmed. The documentation, read in full, turned out to
contradict itself on the two facts the design depends on most, and a live probe contradicted
it on two more.

## What is confirmed, and how

There are three sources, and each fact names which one it comes from:

- **V3**: https://bingx-api.github.io/docs-v3/, the current docs, last updated 2026-09-19.
- **V1**: https://bingx-api.github.io/docs/, the older site, deployed 2026-01-22.
- **Probe**: two read-only scripts the **owner** ran on 2026-09-26/27 with their own read-only
  key. Nothing in this work saw the key. The scripts print codes, counts, JSON types, the
  symbols traded, and time relations. They never print an amount, an id, a key or a signature.
  The account held a few dozen fills in one symbol, the oldest under two weeks old. The
  details of the owner's account stay out of this public repository.

The scripts and their full output are in the tech lead's scratchpad, not in the repository.
What they established is below and goes into `docs/providers.md`.

| Fact | Docs say | Probe found | Used here |
|---|---|---|---|
| Endpoint | `GET /openApi/spot/v1/trade/myTrades`, signed, permission "Read" (V3, V1) | answered as documented | yes |
| `symbol` required? | V3: no. V1: yes | **no**. Without `symbol` the answer is code 0, with every fill and a `symbol` on each | omitted |
| Retention | "Can only check data within the past 7 days range" (V3, V1) | **wrong**. A time-bounded query returned fills more than ten days old, and spans of 14, 30, 90 and 365 days ending now all answered code 0 with every fill | 365 days, see Design |
| No time bounds | "the past 24 hours" (V3, V1) | **wrong**. Returns from the oldest fill, ascending, and `limit=5` gave the oldest five | never sent without bounds |
| `startTime` / `endTime` | milliseconds; inclusivity not stated | **both inclusive**. `[T, T]` returns the fill at `T`; `[T+1, …]` and `[…, T−1]` do not | `[since, until − 1 ms]` |
| Order | "sorted by time field, from smallest to largest" | ascending by time and by id | yes |
| Which fills a capped page keeps | not stated; "by default, the latest trade will be retrieved" hints at the newest | **the oldest in range**, with both bounds, with one, and with none, without a symbol (third probe, 2026-09-27) | pages forward from the newest millisecond |
| `fromId` | "Starting trade ID" | inclusive, ascending (`id >= fromId`) | not used, see Design |
| Trade id | `int64` JSON number | a JSON integer of about 26 bits, like the docs' own sample (36767057) | namespaced by symbol |
| `limit` | "Default 500, maximum 1000", and in the same page "limit = 500" | `1` and `5` honoured; `1001` accepted without error | 500 |
| Envelope | `{code, msg, data: {fills: [...]}}`, code 0 = success | also `retryable`, and `timestamp` on errors; **every error seen came on HTTP 200** with a non-zero `code` | yes |
| `commission` | `float64` | a bare JSON number, negative, in the base asset on buys | see Parsing |
| `quoteQty` | a string | `price × qty` exactly for 1 fill in 3. The docs' own sample carries `"17.997667582000002"`, float noise | see Parsing |
| Signing | HMAC-SHA256, lowercase hex, `X-BX-APIKEY` header, `signature` appended last. V3: sort every key including `timestamp`. V1: "without sorting" | the V3 recipe was accepted | sorted, signed exactly as sent |
| Wrong signature | `100001` | `100001`, HTTP 200 | auth |
| Timestamp 60 s old | `100421` | `100421`, HTTP 200 | unavailable |
| No key header | `100413` | `100413`, HTTP 200 | auth |
| Malformed symbol | `100400` | `100400`, HTTP 200 | invalid request |
| Empty window | not stated | code 0, `data.fills: []`, never `100204` | empty page |
| Server time | "all timestamps in milliseconds" | `/openApi/spot/v1/server/time` returns **seconds** | not used |
| Rate limit | 5/s per UID for myTrades; headers `X-RateLimit-Requests-Remain`/`-Expire` | the headers are present | `RateLimit(5, 1000)` |

**Not established, and designed around:**
- whether trade ids are unique across symbols (the account traded one symbol);
- whether the venue keeps fills older than a year;
- what it answers for a window older than it keeps.

## Scope

- `providers/exchanges/bingx.py`: capabilities, error map, credentials, the provider, and the
  pure parsing functions, exposed for tests.
- The registry builds BingX when both variables are set.
- Settings: `PORTFOLIO_BINGX_API_KEY` and `PORTFOLIO_BINGX_API_SECRET`, all or none, never
  blank, and the key header-safe.
- `docs/providers.md` gets a BingX section with the table above. `docs/operations.md` gets a
  "Connecting the BingX account" section.

## Non-goals

- **Symbol discovery.** The venue answers without a symbol, so `candidate_symbols()` returns
  `()`. The issue makes discovery conditional on the venue requiring it, and it does not.
- **The fallback domain** `open-api.bingx.io`. The docs say it is for when the primary is
  down, and capped at 60 a minute. An outage is `unavailable`, and the next run asks again.
- **Futures, margin, the fund account, and anything but spot fills.**
- **Any frontend change.** `src/lib/exchanges.ts` from #16 already names
  `PORTFOLIO_BINGX_API_KEY` and `PORTFOLIO_BINGX_API_SECRET`, which this spec confirms. #16's
  test fixtures need `PLANS_BEFORE_FIRST_FETCH.bingx = true`. That is a one-line change,
  made once #16 is on `main` (see Handed on).

## Design

### Capabilities

```python
ExchangeCapabilities(
    exchange_key=ExchangeKey.BINGX,
    retention=timedelta(days=365),
    max_query_window=timedelta(days=30),
    page_size=500,
    cursor_kind=CursorKind.TIME,
    rate_limit=RateLimit(max_requests=5, per_ms=1000),
    requires_symbol=False,
)
```

- **Retention of 365 days is a declared bound, not a measured one.** The 7 days in the API
  docs is disproved: the probe read fills more than ten days old with a time-bounded query. The only
  longer statement BingX makes is its support centre's "trade records … available for up to
  one year", about the web export. Over-declaring would claim history the venue may not
  return. Under-declaring would drop the owner's own fills: at 7 days, the fills from more than
  ten days before the probe would never be read. With 365, `history_truncated` tells the owner that nothing
  before a year ago is promised, which is true.
- **`max_query_window` is 30 days.** Spans up to 365 days were accepted, so this is headroom,
  not a limit. A first backfill is 13 windows, each one or two requests.
- **`page_size` is 500**, the smaller of the page's two statements. See "When a page is
  full".
- **`requires_symbol` is `False`**, because the probe showed the query needs none.

### The cursor is a time, because the ids are probably per symbol

A trade id of about 26 bits cannot number every spot trade on a large venue. The docs' sample
for BTC-USDT is the same size. So ids are very likely a sequence **per symbol**. The two
cursor choices then compare like this:

| Cursor | Across symbols, if ids are per symbol |
|---|---|
| `fromId` (`TRADE_ID_AFTER`) | `id >= X` means something different in every symbol's sequence. A page from a symbol with low ids would be skipped for good, **silently** |
| time (`TIME`) | correct whatever the id scheme, because the venue orders every symbol's fills by time, ascending |

So the cursor is **the epoch millisecond of the newest fill on the previous page**, sent as
the next `startTime`:

1. The first page asks `startTime = epoch_ms(since)`, `endTime = epoch_ms(until) − 1`,
   `limit = 500`, and no symbol. Both bounds are inclusive, so this is exactly
   `[since, until)`.
2. Given the page's fills, let `m` be the newest `executed_at` in milliseconds.
   - **No fills:** `next_cursor = None`. The window is done.
   - **Fewer than 500 fills:** `next_cursor = None`. The venue returned everything in range.
   - **500 fills, `m` after this request's `startTime`:** `next_cursor = str(m)`. The next
     request starts **at** `m`, not `m + 1`, because more fills may share that millisecond.
     The fills at `m` are read twice, and #15's unique constraint makes the second read
     insert nothing.
   - **500 fills, all at this request's `startTime`:** `ExchangeSchemaError`. More than a
     page of fills in one millisecond cannot be paged past with a time cursor. That is a loud
     failure rather than a silent loss.
3. **Every fill must satisfy `startTime <= executed_at <= endTime`**, or the page is an
   `ExchangeSchemaError`, because the venue ignored a bound. `assemble_fill_page` checks the
   window, and the provider checks the cursor.

**It terminates by construction.** Each cursor is strictly greater than the one before, and
every cursor is at or below `until − 1 ms`. This covers a repeat and a cycle alike. The same
page served twice fails the "strictly after `startTime`" rule, or the fill-bound rule, on its
second request.

A cursor from the caller must be canonical digits, `\A(0|[1-9][0-9]{0,14})\Z`, inside
`[since, until − 1 ms]`. Anything else is a `ValueError` before any request, like Bitget's.

**#15 drops a time cursor when a window's `since` moves** (`cursor_survives_a_moved_since` is
`False` for `TIME`). That costs a re-read from the window's start, which the unique constraint
makes free.

### When a page is full

The docs give two maxima, 1000 and 500. The probe could not tell which the venue enforces,
because the account has a few dozen fills. So the provider asks for 500 and calls a page of exactly
500 full.

If the venue silently capped below 500, a capped page would look complete, and the rest of
the window would be lost. Nothing in the docs or the probe suggests a cap below 500, and
`limit=5` was honoured exactly. This is recorded under Risks. The alternative, always paging
until an empty answer, costs a request on every window with fills. It buys nothing unless
the docs are wrong in a direction they give no hint of.

### Trade ids are namespaced by symbol

`external_trade_id = f"{symbol}:{id}"`, for example `BTC-USDT:36767057`. `NormalizedFill`
requires the id to be unique per account across symbols, and here that is not established.
Namespacing costs nothing if ids turn out to be global. If they are per symbol, it is the
difference between two fills and one silently dropped. The id must be a JSON **integer** in
`1..2**63 − 1`. `decode_json` returns it as an `int`, never through a float, and anything else
is a schema error.

### Parsing a fill

| `NormalizedFill` | From | Rule |
|---|---|---|
| `external_trade_id` | `symbol`, `id` | `f"{symbol}:{id}"`, as above |
| `external_order_id` | `orderId` | a JSON integer (61 bits in the probe), rendered with `str()`; `null` or absent gives `None` |
| `symbol` | `symbol` | `\A[A-Z0-9]{1,20}-[A-Z0-9]{1,20}\Z` |
| `base_asset`, `quote_asset` | `symbol` | split on the **last** `-`: the quote is `[A-Z0-9]{1,20}`, and the base is everything before it (see After review, R1). No symbol lookup is needed |
| `side` | `isBuyer` | exactly `true` or `false`, which give buy or sell |
| `quantity` | `qty` | `require_fill_amount`, as reported |
| `price` | `price` | `require_fill_amount`, as reported |
| `quote_quantity` | `quoteQty` | `require_fill_amount`, then **`from_binary_float`** (below); `quote_quantity_derived` is `False`. When `quoteQty` is absent, `null` or `""`, it is `derive_quote_quantity(quantity, price)` and `True` |
| `fee_amount` | `commission` | **`from_binary_float`, then negated**. A negative `commission` is a fee paid. A **positive** one is refused, as Bitget's is, until a real rebate shows what it means |
| `fee_asset` | `commissionAsset` | required when the fee is not zero; `None` when it is zero and the asset is absent or `""` |
| `executed_at` | `time` | `datetime_from_epoch_ms` of a JSON integer |
| `raw_payload` | the fill object | `encode_raw_payload(item)`: the element of `data.fills`, never the envelope |

`isMaker` is kept in `raw_payload` only.

**`from_binary_float` rounds to 15 significant digits, half-even, in `Decimal` arithmetic.**
BingX produces `commission`, and evidently `quoteQty`, from IEEE-754 doubles:
- the type is documented as `float64`;
- the docs' own sample shows `"17.997667582000002"`;
- a live response quoted in a third-party wrapper's source (CCXT; an illustration, not a
  source) shows `-0.00005820000000000001`, and `"4.9988562000000005"` for `quoteQty`.

A double carries 15 significant decimal digits faithfully (`DBL_DIG`), and any digits after
those are artefacts of the binary representation, not information the venue holds. Rounding
to 15 recovers the value the venue meant: `17.997667582000002` becomes `17.997667582`.
Without it, a fee like `-0.00005820000000000001` has 20 fractional digits, and
`NormalizedFill` refuses it (`FILL_SCALE` is 18). Every page holding such a fee would then
fail on every run.

The rule does not contradict "refuse, never round" (spec 012). That rule is about a column
rounding a value silently. This one is a documented decoding of a venue's float encoding,
applied to the two fields that carry it, and never to `price` or `qty`. Those are strings
the venue formats exactly, and a 19-digit KAS quantity must survive them intact. A value that
is still finer than `FILL_SCALE` after rounding is refused as before.

A missing field, or one of the wrong JSON type, is an `ExchangeSchemaError` naming the field,
never the value. `KeyError`, `TypeError` and `AttributeError` never escape the provider. The
seven classes are all it raises. The four interpreter limits from spec 012's closing section
apply exactly as they did to Bitget:
- the digit limit;
- nesting depth;
- the `Decimal` exponent range;
- lone surrogates.

### Signing and the request

- **The query** has its keys in ASCII order, including `timestamp`, and its values are
  digits only, so nothing needs encoding: `endTime`, `limit`, `startTime`, `timestamp`. Then
  comes `&signature=<hex>`, last. This satisfies V3's "sort every key", and V1's "without
  sorting" too, because the string sent is the string signed.
- `signature = hmac_sha256_hex(secret, query)`, the query without the signature. `signing.py`
  gains the hex variant beside the Base64 one. The secret is only ever unwrapped there.
- The header `X-BX-APIKEY` carries the API key. There is no other credential header.
- `timestamp = epoch_ms(clock())`. No `recvWindow` is sent, so the venue's 5-second default
  applies.
- **The transport replays a signed request** on a 429 or a 5xx, as it does for Bitget. A
  replay older than 5 seconds is refused with `100421`, which maps to unavailable, and the
  next run signs a fresh one. The replay is pinned by a test.
- The base URL is `https://open-api.bingx.com`, and the path label is `exchange_fills`. It
  is the existing label, so the transport logs `https://open-api.bingx.com/exchange_fills`
  and nothing else.

**The signature travels in the query string.** This is the issue's warning. The shared
transport already logs a label, never a path or a query. That is what spec 013 established,
and a test here proves it for this venue (criterion 3).

### Classifying an answer

A success is HTTP 200 **and** a JSON object whose `code` is the integer `0` (not `false`, not
`"0"`) **and** whose `data` is an object holding a `fills` array. Anything else:

- **A transport failure** raises `ExchangeUnavailableError`, chained from the transport
  error, whose message carries no URL.
- **A non-200, or a 200 with a non-zero `code`**, raises
  `exchange_error(status, venue_code_of(code), error_map=BINGX_ERROR_MAP, retry_after_ms=…)`,
  `from None`. The status decides alone when the body carries no usable code. Every error in
  the probe arrived on a 200.
- **A 200 with code 0 but not the documented shape** (no `data`, `data.fills` not an array,
  `data` null) raises `ExchangeSchemaError`. A missing list is never read as "no fills".
- **Never `raise_for_status()`.** Its message carries the full URL, and here the URL carries
  the signature.

`BINGX_ERROR_MAP`. Every key is `(None, code)` unless a status is named:

| Codes | Class | Why |
|---|---|---|
| `100001` signature mismatch, `100412` signature missing, `100413` key missing or wrong, `100419` IP not on the key's whitelist, `100414` account abnormal (deployed V1 spot common codes), `100441` account abnormal or KYC required (V3 spot table), `100401` authentication failed (V1's legacy list only) | `ExchangeAuthError` | the owner has to fix the key or the account. A signature mismatch is a wrong secret once the golden vectors prove the recipe. V3 may have renumbered `100414` as `100441`; both are mapped |
| `100004` permission | `ExchangeInsufficientScopeError` | the key lacks Read |
| `100421` timestamp mismatch | `ExchangeUnavailableError` | **never auth**: the transport replays signed requests, and a skewed clock is not a bad key. The same decision as Bitget's `40008` |
| `100410`, `109429` rate limit; `(418, None)` "IP banned after 429" | `ExchangeRateLimitedError` | `109429` is listed under Futures in V3. The deployed V1 bundle's changelog of 2025-10-11 reports "Old error code 100410 has been updated to new error code 109429, meaning: APIRateLimit", effective 2025-10-16, among futures codes. It is mapped defensively and recorded as such. A 418 must not read as a refused request that a person fixes |
| `100500` busy (V3), `100503` busy (V1 only) | `ExchangeUnavailableError` | retry next run |
| `100400` parameter error, `100204` "data not found / span too wide", `100404` path, `100490` pair offline | `ExchangeInvalidRequestError` | a request we built. The probe shows an empty window is code 0, so `100204` is never an empty answer |

`100403` is deliberately **unmapped**. V1 lists it as an authorisation failure, and V3's Account table uses it for "not the main account". A code with two meanings goes to the fallback: on a 200 that is a schema error, which is loud and needs a person.

**Nothing maps to `ExchangeRetentionWindowError`**, because what BingX answers for a window
older than it keeps is unknown. If it is an error, the unmapped code on a 200 is a schema
error, which is loud, and the first real case gets a mapping. If it is an empty success, it
is indistinguishable from no trades. That is why retention is declared at a bound that the
owner's history sits well inside (Risks).

### Credentials and settings

- `Settings` gains `bingx_api_key` and `bingx_api_secret`, both `SecretStr | None = None`.
- `_refuse_unsafe_configuration` refuses the following, naming the variable and never a
  value:
  - a partial set, naming the missing variable;
  - a blank value;
  - a key an HTTP header cannot carry: a space at either end, a control character, or
    anything outside printable ASCII. That is the #13 lesson: h11 quotes the whole header
    value in its error. The secret is never sent, so it is not checked.
- `bingx_credentials(settings) -> Credentials | None`.
- `BingXProvider(client, credentials, *, clock=utc_now)` refuses `Credentials` carrying a
  passphrase (`ValueError`), because BingX has none, and one set means the caller is confused.
- `exchange_providers` holds BingX when configured, beside Bitget.

### Logging

There is no log call in the provider, as for Bitget. The transport logs the label URL.

### Modules

| Path | Change |
|---|---|
| `backend/src/portfolio/providers/exchanges/bingx.py` | new |
| `backend/src/portfolio/providers/exchanges/signing.py` | `hmac_sha256_hex` beside the Base64 variant |
| `backend/src/portfolio/providers/exchanges/registry.py` | BingX when configured |
| `backend/src/portfolio/providers/exchanges/__init__.py` | docstring: BingX exists |
| `backend/src/portfolio/config.py` | the two settings and their refusals |
| `docs/providers.md` | a BingX section: the facts table with its dates and URLs, the probe's method, what is not established, and the decisions above. "Not done yet" updated |
| `docs/operations.md` | "Connecting the BingX account": a **Read**-only key (the default), the IP whitelist optional, the two variables in `secrets.env`, the recreate, and what `100421` and `100419` mean |

### Rejected alternatives

| Rejected | Why |
|---|---|
| `requires_symbol=True` with discovery from balances and order history | the venue does not need it; discovery is the issue's named way to import a silent subset |
| a `fromId` cursor (`TRADE_ID_AFTER`) | correct only if ids are one sequence per account, which the id size argues against, and wrong silently if not |
| retention 7 days, as documented | disproved, and it would drop the owner's fills from more than ten days back |
| no retention (`None`) | would promise history since 2009 from a venue that states a year |
| `limit=1000` | if the venue enforces 500, a 500-fill page would look short, and the rest of the window would be lost |
| storing `commission` and `quoteQty` exactly as the JSON text | float noise past `FILL_SCALE` would fail pages forever; within it, it would store artefacts as amounts |
| rounding `price` and `qty` too | they are exact strings; rounding would corrupt a 19-digit quantity |

### After review (R1–R8)

This section overrides the design above wherever the two disagree.

**R1. Symbols are split on the last hyphen, and the base is wide.** The design said BingX
spells every spot symbol `BASE-QUOTE`, citing the probe. That was wrong twice over: the
probe printed only the count and three examples, and the reviewer's fetch of the live
public list on 2026-09-27 found 38 of 2273 that fail the pattern. Among them:
- `STRK-OLD-USDT` and `H_OLD-USDT`, pairs renamed when a token migrated;
- `$U-USDT`, `D.O.G.E.-USDT` and `ATOM(ARC20)-USDT`;
- `MØTH-USDT`.

A rename can turn a pair the owner holds into one of these, and then every page containing it
would fail forever. The rules now are:
- The quote is `[A-Z0-9]{1,20}` after the **last** hyphen. Every live quote matches, and the
  venue's own validation lists `USDT`, `USD1`, `USDT2`, `USDC`, `ETH` and `BTC`.
- The base is everything before it: 1–40 characters, refusing whitespace, control characters
  and anything not UTF-8, and accepting the rest.
- `A-B-C` is accepted, as base `A-B`.

**R2. A renamed pair can duplicate a fill.** The namespaced id embeds the symbol, so a fill
re-read under a new name gets a new `external_trade_id` and is inserted again instead of
meeting the collision check. That needs a rename inside a window read twice, which in practice
means #15's five-minute overlap. It is recorded as a risk, not designed around.

**R3. `109500` maps to unavailable.** V3's changelog of 2026-09-05 moved a sibling endpoint from
`code=0, data=[]` to `109500` for a backend that is down. It is mapped defensively, as
`109429` is.

**R4. A credential that does not encode as UTF-8 is refused at startup**, for both venues,
naming the variable. Before this, a non-UTF-8 secret passed `Settings`, and `signing` raised a
bare `UnicodeEncodeError` whose `args` held the whole secret.

**R5. No detail of the owner's account in the repository.** The probe's findings are recorded
as facts about the venue, and the account is described only as "a few dozen fills in one
symbol, the oldest under two weeks old". Test fixtures use neutral symbols.

**R6. The real sync, end to end.** A test runs `BingXProvider` on the fake venue through the
exchange sync service and repository. It shows that a window of more than 500 fills imports
each fill once, and that the overlap millisecond's second read inserts nothing and raises no
conflict. The secrets test runs with BingX configured.

**R7. Small things.**
- `docs/operations.md` says this application *assumes* a year, not that BingX promises one.
- The float-noise test uses a value that rounding actually changes and that is then still
  refused.
- `frontend/src/lib/exchanges.ts` no longer calls the BingX variable names a guess.
- The empty state names operations sections 12 (Bitget) and 14 (BingX).

**R8. `limit` keeps the oldest fills in range, settled by a third probe (2026-09-27).** The
design assumes the venue fills a capped page with the **oldest** fills in range and pages
forward. The second probe had shown that only for a query with no bounds, and the docs' "by
default, the latest trade will be retrieved" hinted the other way. So the owner ran a third
probe, exactly as the provider asks: both bounds, no symbol. The results:

- `limit` 5 and 2 over `[oldest − 1 h, now]`, and 5 over a one-hour span holding a burst of
  fills, returned the **oldest** fills in range every time. So did `startTime` alone and
  `endTime` alone.
- Without a symbol, the bounds are inclusive as well: `[T, T]` holds the fill at `T`, and
  `[T + 1, …]` and `[…, T − 1]` do not.
- Two of the account's fills share one millisecond. That is the case the overlap at `m`
  exists for.

The cursor takes the newest millisecond by value, so the order *within* a page does not
matter. Which fills fill a capped page does matter, and it is now established.

**Accepted, not changed:**
- A 401 or 403 from a CDN becomes `auth_failed`, through #12's shared fallback. Every BingX
  error the probe saw came on a 200.
- The transport-failure error's `__cause__` holds httpx's request, whose URL carries the
  signature. No `str`, `repr`, `args` or log rendering shows it, and Bitget's is the same.
- `fills_seen` counts the overlap's double read.

## API contract

None changes. `GET /api/exchanges` lists `bingx` once it is configured, as #15 built it.

## Data model

None. `exchange_key` already allows `bingx`.

## Acceptance criteria

These are verbatim from the issue, each with this spec's reading:

1. **Endpoint, parameters, window cap and retention confirmed against live documentation and
   recorded in `docs/providers.md`.** They are confirmed against the live docs **and**
   against the live API through the owner's probe, because the docs contradict themselves.
   Where they differ from the probe, the probe wins and the difference is recorded.
2. **Signature verified by golden vectors.** BingX publishes no usable vector: V1's example
   does not reproduce. There are two vectors instead:
   - V3's documented recipe run verbatim, which gives
     `fe041f159118c90ac13eab4d32f9e2d75b80ca6fe17ca8acd290aba864753ce2`;
   - the provider's own request, which gives
     `c181531feee1cc42ce6ac986aafca6c9b590a7b746809b820df601860b2f9d6e`.

   Both were computed outside this code with `openssl dgst -sha256 -hmac 'SECRET_KEY' -hex`,
   and the command sits beside each literal. The fake venue also re-verifies every request
   with `hmac` directly.
3. **A test asserts the signature never reaches a log record.** It also covers the key and
   the secret, through the production log pipeline, on a success, a refusal, a replayed
   request and a transport failure, and in every rendered exception. The search uses short
   windows of each value, per the #13 lesson.
4. **`requires_symbol` set correctly, with the symbol-discovery path tested.** It is `False`,
   on the probe's evidence. The discovery path is `candidate_symbols() == ()`, and a test
   asserts no request carries `symbol`. The issue's condition for discovery, "if the venue
   requires enumerating symbols", does not hold.
5. **Pagination terminates and is guarded against a non-advancing cursor.** The time cursor
   strictly increases, a full page stuck in one millisecond raises, and a fill before the
   cursor raises.
6. **Error mapping covered for auth, rate-limit and server errors.** Every row of the map, on
   HTTP 200 and on the statuses the docs list.
7. **`Decimal` normalization is exact.** `decode_json` parses numbers to `Decimal`, never
   `float`. `price` and `qty` are stored as reported. The two float-encoded fields are rounded
   by a stated rule, in `Decimal` arithmetic, and tests pin its inputs and outputs.

Added by this spec:

8. The BingX credentials are two settings, all or none, never blank, the key header-safe, and
   a venue without them is absent from `exchange_providers`.
9. Every failure is one of the seven classes, whatever the venue sends.

## Test plan

This follows spec 014's discipline:
- expected values come from outside the code;
- every absence check has a positive companion;
- bodies are hand-written strings, never `json.dumps`.

The fake venue (`backend/tests/providers/exchanges/bingx_harness.py`) behind an
`httpx.MockTransport` holds scripted fills across **two symbols whose ids overlap**. It
implements:
- inclusive `startTime`/`endTime`;
- `limit`, with an optional silent cap;
- ascending time order;
- an optional "ignore `startTime`" defect.

It **verifies every request's signature** with `hmac` over the query bytes received.

| # | Test | Must assert |
|---|---|---|
| 1 | `tests/providers/test_documentation.py::test_the_document_records_the_confirmed_bingx_facts` | `docs/providers.md` names `/openApi/spot/v1/trade/myTrades`, `X-BX-APIKEY`, the 365-day bound and why, "inclusive", the two dates, and a `bingx-api.github.io` URL |
| 1 | `...::test_the_document_records_where_the_docs_were_wrong` | the 7 days, the 24 hours and the `symbol` conflict are named as contradicted by the probe |
| 2 | `tests/providers/exchanges/test_bingx.py::test_the_signature_matches_the_documented_recipe` | V3's string gives `fe041f15…53ce2`, with the openssl command beside it |
| 2 | `...::test_the_signature_of_the_request_the_provider_sends` | a fixed clock and window give exactly `endTime=1695899999999&limit=500&startTime=1695800000000&timestamp=1684814440729`, and `c181531f…9d6e` |
| 2 | `...::test_the_query_is_sent_exactly_as_signed_and_the_signature_is_last` | the query bytes received equal the signed string plus `&signature=`; keys ascending; `X-BX-APIKEY` present; no other credential header |
| 3 | `tests/providers/exchanges/test_bingx_logging.py::test_no_credential_reaches_the_log` | the production pipeline; the signature, the key and the secret absent in short windows; `exchange_fills` present |
| 3 | `...::test_no_credential_reaches_a_rendered_exception` | per class: `str`, `repr`, `args`, `logger.exception` |
| 4 | `...::test_no_request_names_a_symbol_and_discovery_is_empty` | `candidate_symbols() == ()`; `symbol` absent from every query; the capabilities say `requires_symbol=False`; a symbol passed by a caller is a `ValueError` with no request sent |
| 5 | `...::test_a_multi_page_walk_returns_every_fill_and_stops` | 1,200 fills across two symbols. Requests start at each page's newest millisecond, every fill is returned, the last page has `next_cursor=None`, and the request count is bounded |
| 5 | `...::test_fills_sharing_the_boundary_millisecond_are_all_read` | 3 fills at the millisecond where a full page ends: all 3 appear across the two pages |
| 5 | `...::test_a_full_page_stuck_in_one_millisecond_raises` | 500 fills at `startTime`: `ExchangeSchemaError`, one request |
| 5 | `...::test_a_venue_that_ignores_start_time_raises_instead_of_looping` | a fill before the cursor: schema error |
| 5 | `...::test_a_short_page_ends_the_window` | 499 fills: `None`; 500: a cursor; 0: `None` |
| 5 | `...::test_a_malformed_cursor_from_the_caller_costs_no_request` | `"01"`, `"-1"`, `"1.5"`, before `since`, at `until` |
| 5 | `...::test_the_bounds_are_since_and_until_minus_one` | an inclusive fake: fills at `since` and `until − 1 ms` kept; at `until`, refused as outside the window |
| 6 | `...::test_each_in_band_code_maps_to_its_class` | one per row, on HTTP 200 and on 400 |
| 6 | `...::test_each_status_maps_to_its_class` | 401 auth, 403 auth, 418 and 429 rate-limited, 500/503/504 unavailable, with an HTML body and a JSON body |
| 6 | `...::test_a_timestamp_error_is_never_auth` | `100421` |
| 6 | `...::test_an_unmapped_code_on_a_200_is_a_schema_error` | e.g. `100999` |
| 6 | `...::test_code_zero_without_fills_is_a_schema_error` | `data` absent, `null`, `fills` absent, `fills` not an array, and code `false` or `"0"` |
| 6 | `...::test_a_transport_failure_is_unavailable_and_carries_no_url` | the chain holds no `HTTPStatusError`; no message contains `signature` |
| 6 | `...::test_the_transport_replays_the_same_signed_request` | a 429 then a 200: identical query bytes |
| 7 | `...::test_the_documented_sample_fill_normalizes_exactly` | the docs' sample verbatim (`"46820.155"`, `"0.1430254"`, `"6696.471396937"`, `-0.000046483255`, `"BTC"`, `1704961925000`, `isBuyer: true`): exact `Decimal`s, fee positive in BTC, a buy |
| 7 | `...::test_float_artefacts_are_rounded_to_fifteen_significant_digits` | `"17.997667582000002"` → `17.997667582`; `-1.2493e-7` → fee `1.2493E-7`; `-0.00005820000000000001` → `0.0000582`; a clean value unchanged; half-even at the 15th digit |
| 7 | `...::test_price_and_qty_are_never_rounded` | a 19-significant-digit `qty` survives exactly; one past `FILL_SCALE` is refused |
| 7 | `...::test_a_positive_commission_is_refused` | and zero with no asset is accepted as `None` |
| 7 | `...::test_the_trade_id_is_namespaced_and_overlapping_ids_stay_distinct` | `ETH-USDT:7` and `BTC-USDT:7` on one page are two fills |
| 7 | `...::test_a_malformed_trade_id_is_a_schema_error` | a string, `0`, `-1`, `2**63`, a float, `null` |
| 7 | `...::test_the_symbol_is_split_on_its_hyphen` | and `"KASUSDT"`, `"A-B-C"`, `"kas-usdt"` and `"\ud800"` are refused |
| 7 | `...::test_the_interpreter_limits_are_schema_errors` | 5000 digits, 1500 deep, `1e1000000000000000000`, a lone surrogate |
| 7 | `...::test_a_missing_or_mistyped_field_is_a_schema_error_naming_the_field` | parametrised; the value is absent from the message |
| 8 | `tests/providers/exchanges/test_bingx_settings.py::…` | all or none; blank; an unsafe key; absent when unconfigured; present when configured; a passphrase refused |
| 9 | `...::test_the_protocol_is_satisfied` | `_CONFORMS: ExchangeProvider = BingXProvider(...)`, checked by `mypy --strict` |
| — | `...::test_retention_clamps_a_first_backfill_to_a_year` | `clamp_to_retention` at a fixed `now`; `history_truncated` against 2009 |

**Mutations the verification must kill:**
- `endTime = until` (not minus one);
- `startTime = cursor + 1` (skips a shared millisecond);
- `next_cursor` from the oldest fill, or from the last fill as served;
- drop the stall guard;
- drop the "fill before the cursor" check;
- `limit = 1000`;
- sign unsorted, or sign a different order from the one sent;
- put `signature` before another key;
- round `price` or `qty`;
- skip `from_binary_float` on either field;
- round down instead of half-even;
- stop negating `commission`;
- accept a positive one;
- stop namespacing the id;
- map `100421` to auth;
- map `100441` to anything but auth;
- drop `109429` or `(418, None)`;
- read code `false` as success;
- treat a missing `fills` as empty;
- treat a blank credential as absent.

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/providers/exchanges/bingx.py`, `.../signing.py`, `.../registry.py`, `.../__init__.py`, `backend/src/portfolio/config.py`, `docs/providers.md`, `docs/operations.md` |
| tester | `backend/tests/**` |
| tech lead | `docs/specs/017-*.md`, `backend/pyproject.toml`, `backend/.importlinter` |
| reviewer | nothing |

## Risks

- **The probe had one symbol.** "No `symbol` returns every symbol" is what V3 documents, and
  what an answer carrying `symbol` on each fill implies, but no account with two symbols has
  confirmed it. If it returned one symbol only, fills would be missing silently. That is the
  failure the issue fears most. The owner's first sync after a second symbol is traded is the
  check, and `docs/operations.md` says to compare the page's fill count with the venue's.
- **A silent cap below 500** would lose the rest of a full window. There is no evidence for
  one.
- **Retention past a year is unknown**, and so is what the venue answers for a window older
  than it keeps. An error is loud; an empty success is not. The owner's history starts under two weeks
  before the probe, well inside the bound.
- **Ids per symbol is an inference** from their size. Namespacing makes it harmless either
  way.
- **`from_binary_float` assumes every artefact lies past the 15th significant digit.** That is
  what a double guarantees. A venue value genuinely needing 16 or more significant digits in
  `commission` or `quoteQty` would be rounded, by at most half a unit in the 15th digit.
- **The secret scanner.** `.gitleaks.toml` matches `bingx…secret = "<16+ chars>"`. Test
  secrets stay `SECRET_KEY`-style and low-entropy, and are never assigned to a name holding
  `bingx`.

## What the plan got wrong

### The docs were wrong on four facts the design rested on, and only the venue could say so

Read in full, the docs said:
- 7 days of retention;
- a default of the last 24 hours;
- a required `symbol` (V1);
- every timestamp in milliseconds.

The owner's probe disproved all four. Designed from the docs, this provider would have
declared 7 days and dropped the owner's fills from more than ten days back. It would also
have built the symbol discovery the issue warns imports a silent subset. The probe was a
stdlib script with a read-only key, run by the owner. It printed no value that identifies
the account, and it cost an hour.

**When a venue's docs contradict themselves, a read-only probe run by the owner is worth more
than any further reading.** Write it to answer the questions the design depends on.

### A probe was cited for more than it printed

The spec said the probe had seen "every spot symbol `BASE-QUOTE` (probe: 2273 symbols)". The
probe printed a count and three examples. The reviewer fetched the list: 38 symbols fail,
some renamed by token migrations, and any of them would have failed every page it appeared
in. The same mistake nearly happened a second time. "`limit` keeps the oldest" had been
observed only for a query with **no** bounds, and the provider always sends both.

**A probe must ask exactly as the code will ask: the same parameters, the same absences.**
Cite it only for what it printed.

### The gate enforced a different number from the one it showed

`fail_under = 99.7` with coverage.py's default `precision = 0` passed 99.65%, while printing
"FAIL Required test coverage of 99.7% not reached". Every floor recorded in
`pyproject.toml` had been enforced half a point lower. backend-dev found it from an exit
code that disagreed with its own message. It is now `precision = 2`.

### #13's library question applies to the codec too

Spec 014 asked, of every library a credential passes through, what it raises and what its
message quotes. `str.encode` was not on that list. A non-UTF-8 secret raised a
`UnicodeEncodeError` whose `args` carried the whole secret, outside the seven classes. It is
now refused at startup and at the type, for both venues.

## Handed on

- **Once #16 is on `main`:** set `PLANS_BEFORE_FIRST_FETCH.bingx = true` in
  `frontend/src/test/exchangeFixtures.ts`. BingX makes no symbols call, so its plan commits
  before the first fetch, as Bitget's does. `src/lib/exchanges.ts` already names the two
  variables correctly.
- **The owner's first BingX sync** is the first test of paging past one page and of a second
  symbol.
