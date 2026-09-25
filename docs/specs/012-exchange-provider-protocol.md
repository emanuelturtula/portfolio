# 012 — Exchange provider protocol, normalized fill and error taxonomy

Issue: #12
Status: implementing

## Problem

Nothing in the backend can describe a trade. The chain seam (#6) reads a balance, which is
one integer per address; an exchange hands back a stream of executions, each with a price, a
fee and an id, behind signed requests whose failures mean very different things -- a key
that was revoked, a key that lacks read permission, a throttle, an outage, a window older
than the venue keeps. #13 and #14 each implement one venue and #15 drives them, and all three
need one vocabulary for "a fill", "what this venue can do" and "why this call failed" before
any of them can be written without inventing its own.

## Scope

- `ExchangeKey` and `FillSide` in `domain/`, the vocabulary a `CHECK` constraint mirrors.
- `NormalizedFill`, `FillPage`, `FillWindow`, `ExchangeCapabilities`, `RateLimit`,
  `CursorKind` and the `ExchangeProvider` protocol.
- The error taxonomy, the error map as pure data, and the function that turns
  `(http_status, venue_code)` into an exception.
- HMAC-SHA256 signing helpers (hex and Base64), verified against published vectors.
- `Credentials`, `SecretStr`-backed, with a `__repr__` and `__str__` that cannot leak.
- Pure helpers every venue needs and would otherwise write twice: the retention clamp, the
  page assembler that enforces the fetch contract, the non-advancing-cursor guard,
  epoch-millisecond conversion without a float, and the raw-payload encoder.
- Two tables, `exchange_accounts` and `exchange_fills`, with
  `UNIQUE(exchange_account_id, external_trade_id)`, and migration `0006_exchanges`.
- `docs/providers.md` gains an "Exchange providers" section.

## Non-goals

| Not here | Where it belongs |
|---|---|
| Any real venue: endpoint paths, cursor parameters, venue error codes, retention numbers | #13 (Bitget), #14 (BingX) |
| `PORTFOLIO_BITGET_*` / `PORTFOLIO_BINGX_*` settings | #13, #14 -- each venue's variables arrive with the venue |
| Endpoint labels for fills in `ENDPOINT_LABELS` | #13, #14 -- a label lands with its call site, per `docs/providers.md` |
| A provider registry (`register_exchange_provider`) | #13, the first provider that registers. The chain registry takes one argument; an exchange factory also needs `Credentials`, and the first real caller should shape it |
| Sync state on `exchange_accounts` (status, `auth_failed`, checkpoints, `requested_since`/`effective_since`) | #15 |
| A fills repository, `ON CONFLICT DO NOTHING`, `seen` vs `inserted` | #15 |
| Splitting a range into query windows, newest first, with the five-minute overlap | #15 |
| Cycle detection across pages (A -> B -> A) | #15, which owns the loop; see "The cursor guard" |
| A credential health check endpoint | not planned; #15 learns about a bad key from a sync |
| Anything under `frontend/` or `api/` | #15, #16 |

## Design

### Layout

| Path | Holds |
|---|---|
| `domain/exchanges.py` | `ExchangeKey` (`bingx`, `bitget`), `FillSide` (`buy`, `sell`) |
| `domain/money.py` | gains `multiply(left, right)` under the money context |
| `providers/exchanges/__init__.py` | docstring only; no provider exists yet |
| `providers/exchanges/errors.py` | the taxonomy, `ErrorMap`, `build_error_map`, `STATUS_FALLBACKS`, `classify_error`, `exchange_error`, `venue_code_of` |
| `providers/exchanges/signing.py` | `hmac_sha256_hex`, `hmac_sha256_base64` |
| `providers/exchanges/credentials.py` | `Credentials` |
| `providers/exchanges/base.py` | `CursorKind`, `RateLimit`, `ExchangeCapabilities`, `FillWindow`, `NormalizedFill`, `FillPage`, `ExchangeProvider`, `RetentionClamp`, `clamp_to_retention`, `assemble_fill_page`, `require_fill_amount`, `derive_quote_quantity`, `datetime_from_epoch_ms`, `epoch_ms`, `encode_raw_payload` |
| `db/models.py` | `ExchangeAccount`, `ExchangeFill`, `FILL_SCALE`, the new `CHECK` texts |
| `db/types.py` | `NumericText`'s too-large refusal stops quoting the amount (see below) |
| `db/migrations/versions/v0006_exchanges.py` | the two tables |

### The error taxonomy sits inside the existing hierarchy, not beside it

Seven classes, and each is also the `ProviderError` subclass whose meaning it shares, so
"retry or not" is answered by the hierarchy #6 already built:

```
ProviderError
├── ProviderUnavailableError                     (retry later)
│   ├── ExchangeUnavailableError        + ExchangeError
│   └── ProviderRateLimitedError
│       └── ExchangeRateLimitedError    + ExchangeError   .retry_after_ms: int | None
└── ProviderResponseError                        (do not retry; needs a person)
    ├── ExchangeAuthError               + ExchangeError
    │   └── ExchangeInsufficientScopeError
    ├── ExchangeInvalidRequestError     + ExchangeError
    │   └── ExchangeRetentionWindowError
    └── ExchangeSchemaError             + ExchangeError
```

`ExchangeError(ProviderError)` is the marker base: `except ExchangeError` catches everything
an exchange provider may raise, and every class carries `status: int | None` (inherited) and
`venue_code: str | None`.

- **Insufficient scope is a subclass of auth**, because both are terminal and both need the
  owner to fix the key; #15 marks the account `auth_failed` for either. The subclass still
  lets #16 show the right hint ("the key lacks read permission" against "the key was
  refused").
- **Retention window is a subclass of invalid request**: the venue refused a request we
  built. It is separate because #15 can react to it (clamp further) where a generic refusal
  needs a person.
- **Schema error is the one class that takes a free-text `detail`.** A parser has to say
  which field was wrong. Every other class builds its message from a fixed per-class summary
  plus `(HTTP <status>, venue code <code>)`, and **its constructor has no parameter a message
  could be passed through** -- which is how criterion 8 becomes a property of the type rather
  than of every call site remembering it. A `detail` names a field and a rule, never a value.

Rejected: a flat exchange hierarchy under `ProviderError`. It would leave `except
ProviderUnavailableError` -- the existing spelling of "transient" -- blind to an exchange
outage, and give "retry or not" two answers in one package. Rejected: reusing
`ProviderResponseError` itself for the schema error. `decode_json` raises it, so an exchange
parser that forgot to translate would leak a class outside the seven; the provider translates
at its boundary instead, and the protocol docstring says the seven are all it raises.

### The error map is pure data, and lookup precedence is fixed

```python
type ErrorMap = Mapping[tuple[int | None, str | None], type[ExchangeError]]
```

A venue declares its map with `build_error_map(entries)`, which validates at import and
returns a read-only mapping: a status outside 100-599, a code that fails `venue_code_of`, the
key `(None, None)`, or a value that is not an `ExchangeError` subclass is a `ValueError` when
the module loads, not a misclassification in production.

`classify_error(status, venue_code, error_map)` resolves in this order, first match wins:

1. `(status, code)` -- exact
2. `(None, code)` -- the code under any status (BingX-style in-band errors arrive on a 200)
3. `(status, None)` -- the status with any code
4. `STATUS_FALLBACKS`: 401 and 403 -> auth; 408 -> unavailable; 429 -> rate-limited
5. any other 4xx -> invalid request; any 5xx -> unavailable
6. anything else, including a 200 with an unmapped code -> schema error

Step 6 is deliberate: an in-band code we have no mapping for is an answer we do not
understand, not a refusal we do. Steps 4-6 are the same for every venue, so a venue's map
only lists what differs. 403 defaults to auth rather than scope because a CDN block and an
IP allowlist both arrive as 403 and "fix the key" covers them; a venue maps its own scope
code explicitly.

`exchange_error(status, venue_code, *, error_map, retry_after_ms=None) -> ExchangeError`
classifies and constructs. It takes no body, no message and no URL, by signature.

### A venue code is carried only if it cannot be anything else

The code comes out of a response body, which a venue fills as it likes. `venue_code_of(raw)`
returns a string only for an `int` (not a `bool`) or a string whose text matches
`\A-?[0-9]{1,10}\Z` -- the digit bound applies to an `int` too, since a long integer could be
an account id -- and `None` for everything else; `ExchangeError.__init__` passes its `venue_code` through the
same function, so a code that reached the constructor by another route is held to the same
rule. Both target venues are believed to use numeric codes (unconfirmed; see Risks). A
digits-only string of ten characters cannot be an API key, a signature or an address, which
is the reason for the shape. **The venue's `msg` field is never carried anywhere**: it is
exactly the field that echoes request parameters.

### Signing helpers take a `SecretStr`, never a `str`

```python
def hmac_sha256_hex(secret: SecretStr, message: str) -> str: ...
def hmac_sha256_base64(secret: SecretStr, message: str) -> str: ...
```

Both UTF-8 encode key and message and call `get_secret_value()` inside, so no provider holds
the raw secret in a local. Which string each venue signs (Bitget's
`timestamp + METHOD + path + query + body`, BingX's query string) is venue-specific and
belongs to #13 and #14. The result is itself sensitive for the length of a request's receive
window; nothing here logs it, and a venue that puts it in a query string relies on the
transport logging `request_target` (scheme, host, label) and never a path or a query.

### `Credentials` masks by construction

A frozen, slotted dataclass: `api_key: SecretStr`, `api_secret: SecretStr`,
`passphrase: SecretStr | None = None`. `__post_init__` refuses a plain `str` (`TypeError`) and
a blank value (`ValueError`), naming the field and never the value. `__repr__` and `__str__`
return a fixed form such as `Credentials(api_key=<redacted>, api_secret=<redacted>,
passphrase=<redacted>)`, with `passphrase=None` when absent -- whether a passphrase exists is
configuration, not a secret. The API key is treated as secret too: rule 3 names API keys.

### `NormalizedFill` refuses what the column would transform

```python
@dataclass(frozen=True, slots=True)
class NormalizedFill:
    external_trade_id: str
    external_order_id: str | None
    symbol: str                 # the venue's spelling, e.g. "BTCUSDT"
    base_asset: str
    quote_asset: str
    side: FillSide
    quantity: Decimal           # base asset, > 0
    price: Decimal              # quote per base, > 0
    quote_quantity: Decimal     # > 0; as reported unless derived
    quote_quantity_derived: bool
    fee_amount: Decimal         # signed: positive paid, negative a rebate
    fee_asset: str | None       # None only when fee_amount is zero
    executed_at: datetime       # timezone-aware
    raw_payload: str            # canonical JSON of the venue's own fill object
```

`__post_init__` raises `ExchangeSchemaError` for: a non-`Decimal` amount (a `bool` or a
`float` included), a non-finite one, a quantity, price or quote quantity at or below zero,
any amount with more integer digits than `MONEY_PRECISION - FILL_SCALE`, **any amount with
more fractional digits than `FILL_SCALE`** (tested as `quantize(value, FILL_SCALE) != value`,
so trailing zeros are not a false refusal), an empty or whitespace `external_trade_id`, a
naive `executed_at`, a `fee_asset` of `None` beside a non-zero fee, or a `side` that is not a
`FillSide`. No message quotes an amount: a fill quantity is the owner's holdings.

The fractional-digits refusal is the point of this section. `NumericText` rounds excess
fractional digits silently -- correct for a price, wrong for a fill whose quote quantity is
stored "as reported" -- so a fill the column would change is refused before it gets there.
**A value a column would transform is refused by the parser that received it**, which is the
rule `docs/providers.md` already states for prices.

`FILL_SCALE = 18` in `db/models.py`, beside `PRICE_SCALE`. Eighteen places cover every token
denominated in wei and leave twenty integer digits, more than any fill needs.

**`quote_quantity` is stored as reported.** When a venue omits it,
`derive_quote_quantity(quantity, price)` returns `quantize(multiply(quantity, price),
FILL_SCALE)` and the provider sets `quote_quantity_derived=True`. `multiply` is new in
`domain/money.py` and returns the **exact** product, assembled from the two coefficients as
integers the way `from_base_units` is -- not `left * right`, which rounds under the calling
thread's context (default precision 28), and not a multiply under `_MONEY_CONTEXT` either,
which would round a product past 38 digits and then let `quantize` round it a second time:
two half-even roundings in a row can land one unit off in the last place. `quantize` is the
only rounding, and it is where the 38-digit ceiling is enforced. The derived value is rounded,
necessarily; that is what the flag says.

**`external_trade_id` must be unique per account across every symbol.** A venue whose ids are
unique only per symbol must namespace them (for example `BTC-USDT:12345`), or the unique
constraint turns two different fills into one and silently drops the second. The docstring
says so and #14 must check it.

`require_fill_amount(value, *, field)` is the parser-side boundary: it accepts a JSON string
of a decimal number, a `Decimal` from `decode_json`, or an `int`, and refuses a `bool`, a
`float`, a malformed or non-finite string with `ExchangeSchemaError`.

`encode_raw_payload(document)` renders the venue's decoded fill object as canonical JSON:
sorted keys, no whitespace, and every `Decimal` written as the literal digits it was decoded
from, so `decode_json(encode_raw_payload(d)) == d` holds and `0.00012300` stays
`0.00012300`. It refuses any value that `decode_json` cannot produce (`TypeError`: a provider
bug, not a vendor's). The provider passes the fill object, **never the envelope or the
request**, which is where a key or a signature could be.

### Timestamps without a float

`datetime_from_epoch_ms(value)` takes an `int` or a digit string and returns an aware UTC
`datetime`, built as `EPOCH + timedelta(milliseconds=value)` -- `fromtimestamp(ms / 1000)` is
a float division and fails the ban. A `bool`, a negative value, a `float` or a malformed
string is an `ExchangeSchemaError`. `epoch_ms(moment)` is the inverse for building a request:
`(moment - EPOCH) // timedelta(milliseconds=1)`, refusing a naive `datetime` with
`ValueError`.

### Capabilities are declared, and two of them are consumed here

```python
class CursorKind(StrEnum):
    TRADE_ID_BEFORE = "trade_id_before"   # page backwards from the last trade id seen
    TRADE_ID_AFTER = "trade_id_after"     # page forwards from the last trade id seen
    TIME = "time"                         # the window's start advances
    NONE = "none"                         # one page per window; a full page means split

@dataclass(frozen=True, slots=True)
class RateLimit:
    max_requests: int
    per_ms: int
    # min_interval_ms: derived, ceil(per_ms / max_requests), integer arithmetic

@dataclass(frozen=True, slots=True)
class ExchangeCapabilities:
    exchange_key: ExchangeKey
    retention: timedelta | None       # None: the venue keeps everything
    max_query_window: timedelta
    page_size: int
    cursor_kind: CursorKind
    rate_limit: RateLimit
    requires_symbol: bool
```

`ExchangeCapabilities.__post_init__` refuses a page size below one and a non-positive query
window or retention; `RateLimit` validates itself, refusing either field below one or not a
plain `int`, because `RateLimit(0, 1000).min_interval_ms` would otherwise divide by zero
(`ValueError`). Which `CursorKind` each venue uses is
#13's and #14's to confirm; the enum lists the shapes both are believed to have.

`clamp_to_retention(requested_since, *, now, capabilities) -> RetentionClamp` returns both
instants -- `effective_since = max(requested_since, now - retention + RETENTION_MARGIN)` --
and `clamped` as a derived property. `RETENTION_MARGIN` is five minutes, a guess recorded as
one: without it, the oldest window is at the retention edge when computed and past it when
the request lands. A `requested_since` after `now`, or a naive argument, is a `ValueError`.
It never raises for a request older than retention; surfacing `effective_since` instead of
throwing is #13's criterion and this is where it is decided once.

### The fetch contract is enforced by construction

```python
class ExchangeProvider(Protocol):
    @property
    def capabilities(self) -> ExchangeCapabilities: ...
    async def fetch_fill_page(
        self, window: FillWindow, *, cursor: str | None, symbol: str | None
    ) -> FillPage: ...
    async def candidate_symbols(self) -> Sequence[str]: ...
```

Not `@runtime_checkable`, for the reason `ChainProvider` is not. `fetch_fill_page` returns a
page and not an async generator, because the sync must commit a checkpoint between pages.
`candidate_symbols` is the discovery hook for a venue with `requires_symbol`; a venue that
does not need it returns an empty sequence. It is in the protocol now so that #14 does not
change a contract #13 already implements.

`FillWindow(since, until)` is half-open, both aware, `since < until` (`ValueError`
otherwise).

`assemble_fill_page(window, fills, *, capabilities, cursor, next_cursor, symbol) -> FillPage` is
the `align_balances` of this seam. A provider parses its response into `NormalizedFill`s and
hands them here, and the rules follow from the code:

| Case | Outcome |
|---|---|
| the window is longer than `max_query_window` | `ValueError` -- the caller's mistake |
| `symbol` given and not `requires_symbol`, or missing and required | `ValueError` |
| a fill executed outside `[since, until)` | `ExchangeSchemaError` -- an answer about something not asked |
| `symbol` given and a fill for a different symbol | `ExchangeSchemaError` -- the same, for a per-symbol query |
| two fills in the page share an `external_trade_id` | `ExchangeSchemaError` |
| more fills than `page_size` | `ExchangeSchemaError` |
| `next_cursor` equal to `cursor` (and not `None`) | `ExchangeSchemaError` -- pagination stopped advancing |

The last row is the cursor guard, `require_cursor_advanced(cursor, next_cursor)`, exposed on
its own as well. It catches a venue repeating a cursor; it cannot catch one cycling between
two, which needs the history only the sync loop has (#15). No message names a trade id or an
amount.

### Data model

`exchange_accounts`:

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER` PK | |
| `user_id` | `INTEGER` NOT NULL, FK `users.id` `ON DELETE CASCADE` | as `wallets` |
| `exchange_key` | `TEXT` NOT NULL | `CHECK (exchange_key IN ('bingx', 'bitget'))`, named `exchange_key` |
| `created_at` | `UtcDateTime` NOT NULL | |

`UNIQUE (user_id, exchange_key)` as `uq_exchange_accounts_user_exchange`. Credentials come
from the environment, one set per venue, so one account per venue is the only configuration
that can exist; relaxing it later needs a credential story first.

`exchange_fills`:

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER` PK | |
| `exchange_account_id` | `INTEGER` NOT NULL, FK `exchange_accounts.id` `ON DELETE RESTRICT` | fills are an immutable event log; deleting an account must not take its history with it |
| `external_trade_id` | `TEXT` NOT NULL | `CHECK (external_trade_id <> '')`, named `external_trade_id` |
| `external_order_id` | `TEXT` NULL | |
| `symbol`, `base_asset`, `quote_asset` | `TEXT` NOT NULL | venue spelling |
| `side` | `TEXT` NOT NULL | `CHECK (side IN ('buy', 'sell'))`, named `side` |
| `quantity`, `price`, `quote_quantity`, `fee_amount` | `NumericText(FILL_SCALE)` NOT NULL | no `CHECK` -- see below |
| `quote_quantity_derived` | `Boolean` NOT NULL | `CHECK (quote_quantity_derived IN (0, 1))`, named `quote_quantity_derived` |
| `fee_asset` | `TEXT` NULL | |
| `executed_at` | `UtcDateTime` NOT NULL | the venue's clock |
| `raw_payload` | `TEXT` NOT NULL | |
| `ingested_at` | `UtcDateTime` NOT NULL | our clock |

`UNIQUE (exchange_account_id, external_trade_id)` as `uq_exchange_fills_account_trade` --
criterion 7, and what #15's `ON CONFLICT DO NOTHING` will stand on.

**The empty-string `CHECK` is the constraint that makes the unique one mean anything.** Two
fills with `external_trade_id = ''` collide, and under `ON CONFLICT DO NOTHING` the second is
dropped without a word. `NormalizedFill` refuses an empty id; the column refuses it again for
a writer that bypasses it.

**No `CHECK` on a money column, deliberately.** `quantity > 0` on a `TEXT` column is a
comparison SQLite performs by numeric affinity -- the float coercion rule 2 forbids. Signs and
scale are enforced by `NormalizedFill`, in Python, where they are exact.

No index beyond the unique constraint, which leads with `exchange_account_id`. The reader that
needs an index arrives with #15 or M4 and adds it then, as `balance_snapshots` did.

**Migration `0006_exchanges`**, down-revision `0005_balances`, creates both tables and is
reversible: the downgrade drops `exchange_fills` then `exchange_accounts`. Every `CHECK` text is
duplicated verbatim from `db/models.py`, with the comment the earlier migrations carry.

### `NumericText` stops quoting an amount

Its too-large refusal says `f"NumericText cannot store {value}: ..."`, and its docstring
records that this was left alone "rather than changed under an unrelated issue" until a
quantity reached the column. This is that issue. The message names the scale and the digit
ceiling and not the value, matching the rounds-to-nothing refusal beside it.

### Rejected alternatives

| Rejected | Why |
|---|---|
| an async generator of fills | hides the page boundary the sync must checkpoint on (the issue's own argument) |
| recomputing `quote_quantity` as `quantity * price` | a one-unit disagreement with the venue's rounding haunts reconciliation forever |
| a free `message` on every exchange error | criterion 8 would be a convention at every raise instead of a property of the type |
| carrying the venue's `msg` "for context" | it is the field that echoes request parameters |
| letting `NumericText` round a fill | "as reported" would be false for exactly the values nobody checks |
| `CHECK (quantity > 0)` in SQL | numeric-affinity comparison on a money column is the float path rule 2 bans |
| a registry in this issue | its factory signature depends on `Credentials` and settings #13 introduces; the first real caller shapes it |
| `datetime.fromtimestamp(ms / 1000)` | a float division in `providers/` |

## API contract

None. No endpoint changes; `openapi.json` is untouched.

## Acceptance criteria

Verbatim from the issue, with this spec's reading where one is needed.

1. `NormalizedFill` with `quote_quantity` as reported plus a `derived` flag, and the raw
   payload retained for forensics.
   *Reading:* the flag is `quote_quantity_derived`; "as reported" includes "not rounded by
   the column", so an amount finer than `FILL_SCALE` is refused rather than stored rounded.
2. `ExchangeCapabilities` declares retention window, maximum query window, page size, cursor
   kind, rate limit, and whether the venue requires per-symbol enumeration.
3. Error taxonomy: auth, insufficient scope, rate-limited (with `Retry-After`), temporarily
   unavailable, invalid request, retention-window, schema error — each one unit-tested
   through the error map with no HTTP mocking.
   *Reading:* "with `Retry-After`" is `ExchangeRateLimitedError.retry_after_ms`, set by
   `exchange_error(..., retry_after_ms=...)`; the provider parses the header with the
   existing `parse_retry_after`. The error tests import no HTTP library.
4. HMAC signing helpers verified against fixed golden vectors using synthetic secrets.
   *Reading:* the vectors are RFC 4231's, which are published independently of this code.
5. `Credentials` is `SecretStr`-backed with no `__str__` or `__repr__` that can leak it.
6. No model field stores secret material; a test asserts it.
   *Reading:* "model" is every ORM table this issue adds and every dataclass a provider
   returns (`NormalizedFill`, `FillPage`, `ExchangeCapabilities`, `RateLimit`, `FillWindow`,
   `RetentionClamp`). `Credentials` is the one type that holds secrets and is excluded by
   name.
7. `UNIQUE(exchange_account_id, external_trade_id)` enforced by the database.
8. An auth error's message never includes the response body, which can echo request
   parameters.
   *Reading:* extended to every exchange error except the schema error's `detail`, and to
   `repr`, `args` and every attribute, not only `str`.

## Test plan

Every expected value comes from outside the code under test. Every absence check has a
positive companion proving the thing searched was real.

| # | Criterion | Test | Must assert |
|---|---|---|---|
| 1 | reported value kept | `tests/providers/exchanges/test_base.py::test_a_reported_quote_quantity_is_kept_when_it_disagrees_with_the_product` | reported `quote_quantity` one unit in the last place away from `quantity * price` survives construction unchanged, `derived` is `False` |
| 1 | derived value flagged | `...::test_a_derived_quote_quantity_is_the_product_rounded_to_the_fill_scale` | expected literal written by hand, including a product needing more than 18 places |
| 1 | derivation ignores the thread context | `...::test_derivation_uses_the_money_context_not_the_thread_context` | same result inside `localcontext(prec=10)` |
| 1 | column would transform -> refused | `...::test_an_amount_finer_than_the_fill_scale_is_refused` | 19 fractional digits refused; 18 accepted; trailing zeros past 18 accepted |
| 1 | amount bounds | `...::test_non_positive_and_oversized_amounts_are_refused` | zero and negative quantity/price/quote, 21 integer digits |
| 1 | no amount in messages | `...::test_a_refused_fill_names_the_field_and_not_the_amount` | the offending digits absent from `str` and `repr`; the field name present |
| 1 | types refused | `...::test_float_bool_and_non_finite_amounts_are_refused` | |
| 1 | fee rules | `...::test_a_rebate_is_negative_and_a_non_zero_fee_needs_an_asset` | |
| 1 | trade id | `...::test_an_empty_or_blank_trade_id_is_refused` | |
| 1 | raw payload | `...::test_the_raw_payload_round_trips_through_the_shared_decoder` | `decode_json(encode_raw_payload(d)) == d`; `"0.00012300"` and `1E+2` spelt as received; keys sorted |
| 1 | raw payload | `...::test_encode_raw_payload_refuses_a_value_the_decoder_cannot_produce` | `float`, `set`, `datetime` -> `TypeError` |
| 1 | parser boundary | `...::test_require_fill_amount_accepts_strings_decimals_and_ints_only` | |
| 1 | epoch ms | `...::test_epoch_milliseconds_convert_exactly_both_ways` | a hand-written instant with a non-zero millisecond; round trip; naive refused |
| 1 | epoch ms | `...::test_a_malformed_epoch_value_is_a_schema_error` | `bool`, `float`, negative, `"12a"` |
| 2 | capabilities | `...::test_capabilities_declare_every_field_the_issue_names` | the field set pinned |
| 2 | capabilities | `...::test_capabilities_refuse_impossible_declarations` | each refusal, one per case |
| 2 | rate limit | `...::test_the_minimum_interval_rounds_up` | `RateLimit(3, 1000).min_interval_ms == 334` |
| 2 | retention clamp | `...::test_a_request_older_than_retention_is_clamped_not_refused` | both instants, `clamped`, margin applied in the right direction |
| 2 | retention clamp | `...::test_a_request_inside_retention_and_an_unlimited_venue_are_untouched` | |
| 2 | retention clamp | `...::test_a_future_or_naive_request_is_refused` | |
| 2 | page contract | `...::test_assemble_fill_page_refuses_each_broken_contract` | one case per table row, each asserting the class |
| 2 | page contract | `...::test_a_window_edge_is_half_open` | a fill at `since` kept, one at `until` refused |
| 2 | cursor guard | `...::test_a_cursor_that_stops_advancing_raises` | positive companion: an advancing cursor and a `None` pass |
| 2 | protocol | `tests/providers/exchanges/test_protocol.py` | a module-level `_CONFORMS: ExchangeProvider = FakeExchangeProvider()` plus a planted wrong signature that `mypy --strict` rejects, as `tests/providers/test_protocol.py` does |
| 3 | each class via the map | `tests/providers/exchanges/test_errors.py::test_every_taxonomy_class_is_reachable_through_an_error_map` | seven parametrised cases, each asserting the exact class (not a superclass) |
| 3 | precedence | `...::test_lookup_precedence_is_exact_then_code_then_status_then_fallback` | a map where all four could match; remove entries one at a time |
| 3 | fallbacks | `...::test_status_fallbacks` | 401, 403, 408, 429, 400, 404, 500, 503, 200-with-code, 302 |
| 3 | retry-after | `...::test_a_rate_limit_carries_retry_after_in_milliseconds` | and `None` when absent |
| 3 | retry semantics | `...::test_each_class_is_retryable_exactly_when_it_is_unavailable` | `isinstance` against `ProviderUnavailableError`/`ProviderResponseError` per class |
| 3 | map validation | `...::test_build_error_map_refuses_a_malformed_entry_at_construction` | bad status, bad code, `(None, None)`, non-exchange class |
| 3 | code shape | `...::test_a_venue_code_is_carried_only_when_numeric` | int, digit string, negative kept; `bool`, alpha, 11 digits dropped |
| 3 | no HTTP | the test module imports neither `httpx` nor `respx` | reviewer checks |
| 4 | hex vectors | `tests/providers/exchanges/test_signing.py::test_hex_digest_matches_rfc_4231` | test cases 1 and 2, hex literal from the RFC |
| 4 | base64 vectors | `...::test_base64_digest_matches_rfc_4231` | the same cases, Base64 literal computed with `openssl` and the command recorded beside it |
| 4 | secret type | `...::test_the_helpers_refuse_a_plain_string_secret` | `mypy` rejects it statically; at run time a `str` has no `get_secret_value` |
| 5 | repr/str | `tests/providers/exchanges/test_credentials.py::test_no_rendering_contains_a_secret` | sentinel values absent from `str`, `repr`, `format`, f-string `!r`/`!s`, `%s`, `dataclasses.asdict` rendered; positive companion: `get_secret_value()` returns the sentinel |
| 5 | through the real log pipeline | `...::test_a_logged_credentials_object_is_masked` | via `tests/logging_harness.py`, not `capture_logs`; positive companion: the log line exists |
| 5 | construction | `...::test_a_plain_string_or_blank_secret_is_refused` | message names the field, not the value |
| 6 | no secret fields | `tests/security/test_exchange_secret_fields.py::test_no_exchange_model_field_is_named_like_a_secret` | every column of both tables and every field of the listed dataclasses against `portfolio.logging.is_sensitive_key` |
| 6 | companion | `...::test_the_field_scan_would_catch_a_secret_field` | a planted `api_secret` field is flagged; the scan saw a non-zero number of fields |
| 6 | no secret types | `...::test_no_exchange_model_field_is_typed_as_a_secret` | no `SecretStr`/`Credentials` annotation among those fields |
| 7 | unique | `tests/db/test_exchange_fills.py::test_the_same_trade_twice_on_one_account_is_refused_by_the_database` | `IntegrityError` naming `uq_exchange_fills_account_trade`; positive companion: the same id on a *different* account inserts |
| 7 | empty id | `...::test_an_empty_trade_id_is_refused_by_the_database` | written through a raw insert, bypassing `NormalizedFill` |
| 7 | other checks | `...::test_side_and_derived_flag_are_constrained` | |
| 7 | history kept | `...::test_an_account_with_fills_cannot_be_deleted` | |
| 7 | exact amounts | `...::test_fill_amounts_round_trip_exactly` | 18-place values, `0.00012300` |
| 7 | migration | `tests/db/test_migrations.py` additions | 0006 sits on 0005; reverses on its own leaving the rest standing; each new `CHECK` reflected and compared against the model constant |
| 8 | auth message | `tests/providers/exchanges/test_errors.py::test_an_auth_error_carries_nothing_from_the_body` | a 401 envelope whose `msg` and `code` hold a sentinel: sentinel absent from `str`, `repr`, `args`, `vars`/slots, `venue_code`; positive companion: the status is present |
| 8 | by signature | `...::test_refusal_constructors_accept_no_message` | `inspect.signature` of each non-schema class and of `exchange_error` has no parameter that takes free text |
| 8 | logged | `...::test_a_logged_auth_error_carries_nothing_from_the_body` | `logger.exception` through the real pipeline |
| -- | domain | `tests/domain/test_exchanges.py` | enum values pinned |
| -- | domain | `tests/domain/test_money.py::test_multiply_is_exact` | a product past 38 significant digits exact, and unchanged inside `localcontext(prec=10)` |
| -- | column message | `tests/db/test_money_types.py` update | the too-large refusal no longer contains the amount; it still names the scale |

**Mutations the verification must kill**, stated before the run so a survivor is judged
against an expectation: swap two precedence steps in `classify_error`; map 403 to unavailable;
loosen the venue-code pattern to `\w+`; drop the fractional-digits refusal; construct
`quote_quantity` from the product when reported; flip the sign of `RETENTION_MARGIN`; make
`until` inclusive; drop the cursor guard; remove `uq_exchange_fills_account_trade` from the
model *and* the migration; remove the empty-id `CHECK`; make `multiply` use the thread context.
Replacing `Credentials.__repr__` with the dataclass default is an **equivalent mutant** for the
leak property (`SecretStr` masks either way) and is recorded rather than tested around.

## File ownership

Disjoint. Nobody edits a file on another row.

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/domain/exchanges.py`, `backend/src/portfolio/domain/money.py`, `backend/src/portfolio/providers/exchanges/**`, `backend/src/portfolio/db/models.py`, `backend/src/portfolio/db/types.py`, `backend/src/portfolio/db/migrations/versions/v0006_exchanges.py`, `docs/providers.md`, `docs/architecture.md` |
| tester | `backend/tests/**` |
| tech-lead | `docs/specs/012-*.md`, `backend/pyproject.toml`, `backend/.importlinter` |
| reviewer | nothing |

## Risks

- **Nothing vendor-specific here is confirmed**, by design. That both venues use numeric
  error codes, epoch-millisecond timestamps and one of the four `CursorKind` shapes is belief,
  not measurement. If #13 or #14 meets an alphanumeric code, `venue_code_of` drops it and the
  classification falls to the status -- still safe, less specific -- and the pattern widens in
  that issue, with the evidence.
- **BingX may return auth failures on HTTP 200.** An unmapped in-band code falls to schema
  error: the run fails loudly and does not retry, but the account is not marked
  `auth_failed`. #14 must map its auth codes.
- **Per-symbol trade ids.** If a venue's ids are unique only within a symbol, the unique
  constraint silently drops fills unless the provider namespaces them. Recorded on
  `NormalizedFill` and here; #14 must verify.
- **`FILL_SCALE = 18` is a guess about fee precision.** A venue reporting a fee with 19
  fractional digits is refused, and one refused fill fails its page. Refusal is the loud
  direction; if it happens, the scale moves with evidence.
- **Strict positivity.** A venue reporting a zero `quote_quantity` for a dust trade would fail
  its page. Loud, and preferred to importing an unaccountable fill.
- **`RETENTION_MARGIN` is a guess**, and it trades up to five minutes of the oldest history for
  not being refused at the edge. A venue that measures retention in calendar days may need
  more; #13 finds out.
- **The secret scanner.** `.gitleaks.toml` blocks `bitget_*_secret = "<16+ chars>"` and the
  default rules flag high-entropy strings beside `secret`/`key`. Synthetic test secrets must be
  obvious and low-entropy (RFC 4231's `Jefe`, `synthetic-not-a-real-secret`) and never named
  after a venue.
