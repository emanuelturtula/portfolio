# 009 — Cached price provider and fiat valuation

Issue: #9
Status: done

## Problem

Two chains can be read and neither can be priced, so the application knows how much of each
asset the owner holds and nothing about what it is worth. Everything #6 through #8 built
counts in base units; this is the change where a count becomes a value.

It is also the first time money in the sense of rule 2 enters the system through a provider.
Balances are integers and integers are exact. **A price is a decimal number arriving from a
vendor**, and one of the three key-free vendors sends it as a JSON number rather than a
string, which is the precise shape rule 2 exists to refuse.

## Scope

- `providers/prices/`: a price provider protocol, three key-free sources and one keyed one.
- A `prices` table, its migration, and a repository.
- A refresh service that fills the table, and a valuation service that reads it.
- A `PriceLookup` result that is either a price or an explicit reason, never a zero.
- An `import-linter` contract making "no request path reaches a price provider" mechanical.
- `docs/providers.md`: the measured call budget, the coverage matrix, and what is assumed.

## Non-goals

- **No scheduler.** #10 owns it. This change delivers `refresh_prices()` as a service with
  no caller, and a CLI entry point so it can be run by hand and measured before #10 automates
  it. A scheduler invented here would be a second one to delete later.
- **No endpoint.** #10 and #11 expose this. Criterion 4's "in the response" is read as the
  service's returned model; see Acceptance criteria.
- **No price history.** The table holds the current price per pair. A time series is what a
  value-over-time chart needs, and nothing asks for one yet; the column set below does not
  foreclose it.
- **No conversion between fiat currencies.** USD and EUR are each fetched, never derived from
  the other through a cross rate.
- **No frontend.**

## Design

### What was measured, because the issue's premise turns out not to hold

The issue is built around CoinGecko's Demo quota — "roughly 10,000 calls per month… about 13
an hour" — and concludes that no request path may call a price API. **The conclusion is right
and the premise is not**, measured on 2026-09-23:

| Source | BTC/USD | BTC/EUR | KAS/USD | KAS/EUR | Key | Price type |
|---|---|---|---|---|---|---|
| Kraken `GET /0/public/Ticker` | yes | yes | yes | yes | none | **string** |
| Coinbase `GET /v2/prices/{pair}/spot` | yes | yes | **404** | **404** | none | **string** |
| Kaspa `GET /info/price` | — | — | yes | **no** | none | **JSON number** |
| CoinGecko | all | all | all | all | Demo key | not verified here |

And the decisive one: **Kraken returns all four pairs in a single call.**

```
GET /0/public/Ticker?pair=XXBTZUSD,XXBTZEUR,KASUSD,KASEUR
-> error: [], four results, every price a string
```

So an hourly refresh costs **one request per hour to one host**, 720 a month, against a vendor
that publishes no monthly quota at all. CoinGecko's 10,000 stops being the binding constraint
the moment the primary is key-free and batched.

**The rule still stands, for better reasons than the quota.** A request path that calls a
price API inherits that API's latency and its outages: a dashboard that renders in 80 ms
locally would block on a third party, and a vendor having a bad afternoon would take the
portfolio page down with it. Quota was the issue's argument; it is the weakest of the three.

### The float problem, which is this issue's real subject

Kraken and Coinbase send strings. **Kaspa sends `{"price": 0.04228645}` — a JSON number.**
`json.loads` turns that into a Python `float` before any of our code sees it, and the value
is no longer the one the vendor sent. Rule 2 is not "do not write `float`"; it is "do not let
a monetary value pass through binary floating point", and by the time a parser could refuse
it, it already has.

`decode_json` in `providers/base.py` — extracted in #8 — gains `parse_float=Decimal`, so a
JSON number arrives as a `Decimal` built from the literal text the vendor sent. This is not a
special case for prices: any future vendor rendering money as a number is covered by it, and
a balance parser that refuses a non-integer still refuses, because `Decimal("1.5")` is not an
`int` either.

The AST ban in `backend/tests/security/test_no_float.py` bans the *name* `float`, and
`parse_float=` is a keyword argument rather than a name. If the walk reports it, the answer is
to make the walk precise, not to add an ignore.

Prices are `Decimal` end to end: `Decimal` in Python, `NumericText` in SQLite, a JSON string
over the wire, never `float`, never `sqlalchemy.Numeric`.

### A price is either a price or a reason, and never a zero

```python
@dataclass(frozen=True, slots=True)
class Price:
    asset_symbol: str
    quote_currency: str
    amount: Decimal
    source: str
    as_of: datetime
    stale: bool

class PriceUnavailable(StrEnum):
    NEVER_FETCHED = "never_fetched"
    EVERY_SOURCE_FAILED = "every_source_failed"
    UNSUPPORTED_PAIR = "unsupported_pair"
    NO_SOURCE_CONFIGURED = "no_source_configured"
```

Criterion 3 is the most important line in the issue and it is quoted here in full because the
design follows from it: *a portfolio silently showing 0 is worse than one showing an error,
because it is believed.*

So the valuation service returns a total **only when every held asset has a price**. A
portfolio with one unpriced asset does not return a smaller number; it returns the sum it
could compute, the list of assets it could not, and a flag saying the total is incomplete.
Anything that renders a total has to decide what to do with that, which is the point — a
number that silently omits a holding is indistinguishable from a number that includes it.

### Staleness is computed at read time, never stored

`stale` is `now - as_of > STALE_AFTER` with `STALE_AFTER = 1 hour`, evaluated when the price
is read and with the clock passed in. A stored `is_stale` boolean would be wrong one second
after it was written, and would need something to rewrite it — a background job whose only
purpose is to keep a derived field true.

**`as_of` is the time we observed the price, not the time the vendor says it was true**, and
that distinction is recorded because the name suggests otherwise. Measured: none of Kraken's
ticker, Coinbase's spot or Kaspa's price endpoint returns a timestamp. We know when we asked;
we do not know how old the answer was. A vendor that does supply one later can populate this
field more honestly without a migration.

### Sources are per pair, not one ordered list

Coinbase does not list KAS at all — measured, a 404 on both `KAS-USD` and `KAS-EUR` — so a
single global failover chain would try a source that can never answer. The registry maps
`(asset, quote_currency)` to an ordered tuple of sources:

| Pair | Order |
|---|---|
| BTC/USD, BTC/EUR | Kraken, Coinbase, CoinGecko (if keyed) |
| KAS/USD | Kraken, Kaspa, CoinGecko (if keyed) |
| KAS/EUR | Kraken, CoinGecko (if keyed) |
| anything else | `UNSUPPORTED_PAIR`, immediately and without a request |

**Kaspa's `/info/price` does not say what currency it is in.** The body is `{"price": ...}`
and nothing else; the documentation does not name a quote currency. USD is an inference from
the number's magnitude, which is not evidence. It is therefore wired as a **last-resort
USD-only** source with the assumption written at the call site, and it is the one source whose
docstring says out loud that its currency is assumed rather than known. Using a price whose
currency is a guess to value someone's holdings is exactly the failure criterion 3 describes.

### Criterion 2 is an `import-linter` contract, not a unit test

"A test asserts no request-path code reaches a price provider" is a statement about the import
graph, so it is checked as one:

```ini
[importlinter:contract:prices-are-never-fetched-in-a-request]
name = no request path reaches a price provider
type = forbidden
source_modules =
    portfolio.api.routers
forbidden_modules =
    portfolio.providers.prices
```

**Without `allow_indirect_imports`**, deliberately — unlike the existing thin-routers contract,
which sets it. Here the chain `router -> service -> price provider` is exactly the violation
worth catching, because it is the one a well-meaning change would introduce: somebody adds
"just refresh it if it is stale" to the valuation service, and every dashboard render becomes a
vendor call. A direct-only check would pass that change.

This is also a partial answer to #39, which asks what replaces the guarantee
`allow_indirect_imports = True` gave up. It is not the whole answer and does not close it.

**And the contract is what splits the service in two.** This spec originally put
`refresh_prices()`, `lookup_price()` and `value_portfolio()` in one `services/prices.py`,
which the implementer refused, correctly, for two reasons.

The first: #11 will add a valuation endpoint, whose router imports the valuation service. In
the single-module layout that service also imports `providers.prices`, so the contract fails
on a change that is entirely legitimate -- the endpoint reads the table and never touches a
vendor. A guard that fails on correct code is a guard somebody weakens, and weakening this one
is the outcome the contract exists to prevent. `providers/http.py` already makes this argument
about a control that renders every request `<unlabelled>`: a control which makes the system
useless is one somebody removes.

The second is sharper. In the single-module layout the `services.prices -> providers.prices`
edge exists on day one, so **the contract can never newly fire for the case it was written
for**. "Somebody adds 'just refresh it if it is stale' to the valuation service" would be
indistinguishable from the status quo, because the import is already there. The guard would be
present, green, and structurally incapable of catching its own stated scenario -- the vacuity
the contract's comment describes as a temporary condition, made permanent by a layout.

So `services/prices.py` is provider-free and `services/price_refresh.py` owns the vendor edge,
which makes it the single module a reviewer has to read to answer "can a request reach a
vendor". Its docstring says so, because the isolation is the guarantee.

### Layout

| Path | Holds |
|---|---|
| `providers/prices/base.py` | `PriceQuote`, the `PriceSource` protocol, `sources_for`, `SUPPORTED_PAIRS` |
| `providers/prices/registry.py` | `price_sources`. Separate from `base.py` because holding it there makes the package import itself in a circle |
| `providers/prices/kraken.py` | one batched call for every pair |
| `providers/prices/coinbase.py` | one call per pair; BTC only |
| `providers/prices/kaspa.py` | KAS/USD only, currency assumed |
| `providers/prices/coingecko.py` | keyed; skipped entirely when no key is configured |
| `providers/http.py` | two labels added to `ENDPOINT_LABELS`, without which a price request logs `<unlabelled>` |
| `providers/base.py` | `decode_json(..., parse_float=Decimal)` |
| `db/models.py`, `db/migrations/versions/v0004_prices.py` | the table |
| `repositories/prices.py` | upsert by pair, read all |
| `repositories/assets.py` | resolving an asset id from a symbol, which `prices.asset_id` needs |
| `services/prices.py` | `lookup_price()`, `value_portfolio()`, `Price`, `PriceUnavailable`. **Imports no provider.** |
| `services/price_refresh.py` | `refresh_prices()`. The only module in `services/` that may import `providers` |
| `cli.py` | `refresh-prices`, so the budget can be measured before #10 automates it |
| `backend/.importlinter` | the contract above |

## API contract

None. No endpoint, no route, no schema change, so `backend/tests/test_openapi.py` must report
no drift.

## Data model

One table. The migration is reversible: `downgrade()` drops it, and nothing else references it.

```sql
CREATE TABLE prices (
    id               INTEGER PRIMARY KEY,
    asset_id         INTEGER NOT NULL REFERENCES assets(id),
    quote_currency   TEXT    NOT NULL,
    amount           TEXT    NOT NULL,   -- NumericText(scale=12)
    source           TEXT    NOT NULL,
    as_of            TEXT    NOT NULL,   -- UtcDateTime
    fetched_at       TEXT    NOT NULL,
    UNIQUE (asset_id, quote_currency)
);
```

- **`UNIQUE (asset_id, quote_currency)`** is what makes this the current price rather than a
  history: the refresh upserts. A history table would need `as_of` in the key and an index; the
  column set here does not prevent that later.
- **`NumericText(scale=12)`.** One column holds a sub-cent asset and a five-figure one at the
  same time — KAS quoted near 0.042 and BTC near 86,000, both measured today — so the scale has
  to serve both. Twelve decimal places keeps a KAS price exact well past its quoted precision
  and leaves `MONEY_PRECISION - 12` digits before the point, which is more than any fiat price
  of a crypto asset will need. `scale` has no default and is a decision, per `db/types.py`.
- **`quote_currency`** is `TEXT` with a `CHECK` limiting it to `USD` and `EUR`, matching how
  `chain_key` is constrained. Adding a currency is then a migration, which is the honest cost.
- No index beyond the unique constraint. The table has one row per pair — four today.
- **Money is never aggregated in SQL.** The valuation service loads rows and sums in Python.

## Acceptance criteria

Verbatim from #9, numbered, with interpretations marked.

1. Prices are persisted with their source and an `as_of` timestamp.
   *Interpretation:* `as_of` is the observation time. No vendor supplies a quote time —
   measured on all three — and the docstring says so rather than implying otherwise.
2. A test asserts no request-path code reaches a price provider.
   *Interpretation:* an `import-linter` contract **without** `allow_indirect_imports`, so the
   `router -> service -> provider` chain is caught, plus a test asserting the contract itself
   exists and fails when violated.
3. A missing price yields a null value **with an explicit reason, never a zero**.
4. Prices older than one hour are flagged as stale in the response.
   *Interpretation:* this change has no endpoint, so "the response" is the service's returned
   model. `stale` is computed at read time from an injected clock, never stored.
5. Works both with and without an API key.
   *Interpretation:* with no key, CoinGecko is not merely skipped at call time — it is absent
   from the source list, so no code path can reach it.
6. Failover is exercised by a test.
7. The monthly call budget is calculated and written down in `docs/providers.md`.
8. USD and EUR are both supported.
   *Interpretation:* both fetched independently. **Never derived from one another**: valuing a
   EUR portfolio with a USD price and a cross rate introduces a second vendor's error into
   every number, silently.

## Test plan

| # | Criterion | Test |
|---|---|---|
| 1 | source and time are stored | `tests/db/test_prices_repository.py::test_a_price_round_trips_with_its_source_and_time` |
| 1 | the decimal survives the round trip exactly | `::test_a_sub_cent_price_round_trips_without_losing_a_digit` |
| 1 | upsert replaces, never duplicates | `::test_refreshing_a_pair_replaces_its_row_rather_than_adding_one` |
| 2 | the contract exists and is strict | `tests/test_import_contracts.py::test_a_request_path_may_not_reach_a_price_provider` |
| 2 | and it would catch the indirect chain | `::test_the_contract_is_not_direct_imports_only` |
| 3 | no price is a reason, not a zero | `tests/services/test_prices_service.py::test_a_missing_price_is_a_reason_rather_than_a_zero` |
| 3 | a partial portfolio does not return a smaller total | `::test_a_portfolio_with_one_unpriced_asset_reports_an_incomplete_total` |
| 3 | and names what it could not price | `::test_an_incomplete_total_names_the_assets_it_could_not_price` |
| 4 | an hour-old price is stale | `::test_a_price_older_than_an_hour_is_stale` |
| 4 | a fresh one is not | `::test_a_price_read_within_the_hour_is_not_stale` |
| 4 | staleness is read-time, not stored | `::test_the_same_row_is_fresh_then_stale_as_the_clock_moves` |
| 5 | no key means no CoinGecko in the list | `tests/providers/prices/test_registry.py::test_without_a_key_the_keyed_source_is_absent_not_skipped` |
| 5 | a key puts it last | `::test_a_configured_key_appends_the_keyed_source` |
| 6 | the primary failing falls over | `tests/providers/prices/test_failover.py::test_a_failed_primary_falls_over_to_the_next_source` |
| 6 | and records which source answered | `::test_the_stored_source_is_the_one_that_actually_answered` |
| 6 | every source failing is a reason | `::test_every_source_failing_yields_every_source_failed` |
| 7 | the budget is written and arithmetically right | `tests/providers/test_documentation.py::test_the_document_states_the_measured_call_budget` |
| 8 | both currencies are fetched | `tests/providers/prices/test_kraken.py::test_one_call_returns_every_configured_pair` |
| 8 | EUR is never derived from USD | `tests/services/test_prices_service.py::test_a_eur_value_never_comes_from_a_usd_price` |
| float | a JSON number becomes a Decimal, exactly | `tests/providers/test_base.py::test_a_json_number_is_decoded_as_a_decimal_not_a_float` |
| float | the Kaspa price keeps every digit | `tests/providers/prices/test_kaspa.py::test_the_price_keeps_the_digits_the_vendor_sent` |
| float | and a float never reaches the table | `tests/security/test_no_float.py` (existing, now scanning `providers/prices/`) |
| pairs | an unsupported pair costs no request | `tests/providers/prices/test_registry.py::test_an_unsupported_pair_is_refused_without_a_request` |
| migration | reversible, and the guard baseline holds | `tests/db/test_migrations.py` (existing pattern) |

### The three that carry the weight

**The float test must compare against the vendor's digits, not against a `Decimal` the test
built the same way the code did.** `test_the_price_keeps_the_digits_the_vendor_sent` asserts
against the literal string in the fixture body — `"0.04228645"` — because an expectation built
by the code under test is the verifier sharing state with its subject, which this project has
now catalogued seven times. The companion asserts that decoding the same body with plain
`json.loads` produces something *different*, so the test proves the hook is doing work.

**Criterion 3's test must drive a real total.** Asserting that `lookup_price` returns a reason
is necessary and insufficient; the failure the criterion describes is a *portfolio total* that
quietly omits a holding. So the test builds a two-asset portfolio, prices one, and asserts the
returned total is marked incomplete and names the missing asset — never that it equals the one
price it could find.

**Criterion 2's test must prove the contract can fail.** A contract file that is present but
misconfigured passes silently. The test writes a module that imports a price provider through
a service into a temporary package and asserts `lint-imports` reports it, in the same shape
`tests/providers/test_protocol.py` proves the `mypy` check can fail.

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/providers/**`, `backend/src/portfolio/db/**`, `backend/src/portfolio/repositories/**`, `backend/src/portfolio/services/**`, `backend/src/portfolio/config.py`, `backend/src/portfolio/cli.py`, `docs/providers.md`, `docs/operations.md` |
| tester | `backend/tests/**` |
| tech-lead | `docs/specs/009-*.md`, `backend/.importlinter`, the `fail_under` line in `backend/pyproject.toml` |

`backend/.importlinter` is the tech lead's because criterion 2's contract is an architectural
decision, and because the tester must be free to assert against it without having written it.

## Coverage

The floor is 99.5 and only ratchets. The standard recorded in `backend/pyproject.toml` is nine
units of headroom; this change adds a table, a migration and four parsers, so the floor moves
only if the measurement supports it against that standard.

## Risks

- **Kaspa's `/info/price` has no documented currency.** USD is inferred. It is last in one
  pair's list and absent from the other, so the blast radius is one asset in one currency when
  Kraken and CoinGecko are both unavailable — but it is a guess being used to value money.
- **No vendor supplies a quote time**, so `as_of` is an observation time for all four sources
  and a price can be older than it appears by however long the vendor cached it. Kraken
  publishes no cache header; Coinbase and Kaspa's own API do.
- **Kraken is a single point of failure for KAS/EUR**, the only key-free source for that pair.
  Losing it means that pair falls to CoinGecko or to a reason.
- **The Demo key path is untested against the real vendor.** CoinGecko's response shape is
  taken from documentation, not measured, because measuring it needs a key this repository
  must not contain. It is the one source whose parser meets production first.
- **`parse_float=Decimal` changes every JSON decode in `providers/`**, including the two
  balance providers that merged this week. Their parsers refuse non-integers either way, and
  the existing suites are the control — but it is a shared decoder and the blast radius is
  every provider.

## What this plan got wrong

### "Never a zero" was false along a path this spec never asked about

The spec quotes criterion 3 in full and builds the whole result type around it: a missing price
is a reason, never a zero. It asked what happens when a price is **absent**. It never asked
what happens when a price is **present and destroyed by the column**.

`require_price` refuses `amount <= 0` -- and runs *before* the value is quantized.
`NumericText` then rounded a positive price down to zero and normalised the result instead of
refusing it, so a vendor sending `0.0000000000005` produced a stored `"0.000000000000"`, and
`value_portfolio` reported that total with `complete=True`. Measured end to end against a real
migrated database. Not reachable with today's four pairs, and the headline claim of the change
was false anyway.

**Three tests pinned it as intended behaviour** -- the repository's own over-precise round trip,
a row of the banker's-rounding table, and the negative-zero normalisation case. Each was the
column destroying an amount, written down as a rounding property, in a module whose entire
purpose is that money is not destroyed.

The rule this earns: **a guard placed before a transformation does not constrain that
transformation's output.** Ask where a value is last *changed*, not where it is first checked.
The guard now lives in `NumericText`, because a non-zero amount that quantizes to zero is a
value the column destroyed, and that will be true of every money column this project adds.

### The float hook was named precisely and was the wrong one by half

The spec identified the hazard exactly -- a vendor sending a price as a JSON number, `json.loads`
producing a `float` before any of our code runs -- and prescribed `parse_float=Decimal`.

`NaN`, `Infinity` and `-Infinity` do not route through `parse_float`. They go through
`parse_constant`, whose default returns a Python float, so the decoder written to keep floats
out of `providers/` was still producing one, invisible to the AST ban because the source
contains no float literal and no `float` name. A `NaN` price then compares false in every
direction, so a "refuse a non-positive price" guard passes it, and a total containing it is
`NaN` without a word.

**When you configure a hook to close a hole, enumerate the other hooks the same library offers
over the same data.** `json.loads` has three. The spec named one.

### The layout and the contract were one decision written as two

The Layout table put `refresh_prices`, `lookup_price` and `value_portfolio` in one module.
The contract forbids `api.routers -> ... -> providers.prices` with indirect imports included.
Together those mean #11's valuation endpoint fails the contract on its first line, for a change
that never touches a vendor -- and, worse, that the contract could never *newly* fire for the
case it was written for, because the offending edge would exist from the first commit.

The implementer caught it before writing either. A contract without `allow_indirect_imports`
constrains the module graph, so the graph has to be designed with the contract in hand; they
are one decision. The split -- `services/prices.py` provider-free, `services/price_refresh.py`
owning the vendor edge -- is what makes the contract able to fail, which is the only property
that makes a contract worth having.

### An enforcement claim in `CLAUDE.md` turned out to be one third true

Rule 2 says money is never aggregated in SQL and names an AST test in
`backend/tests/security/` as the enforcement. That test bans float literals and the name
`float`. Nothing bans `func.sum`, `order_by` or a comparison against a money column.

The gap was harmless while no money column existed. **This change shipped the first one**, and
milestone 3 adds one per accounting table. A `SUM()` over a `TEXT` money column does not fail
in SQLite -- it coerces each value to a float and returns a plausible wrong answer, which is
the failure this project ranks above an error. Filed as #58; the diff itself is clean, checked
by review rather than by a test, which is exactly the situation rule 2's own preamble warns
about: a rule that only lives in a document is a rule that erodes.

### A note on what did not go wrong

Review mutated sixteen guards and every one was killed, so the suite was load-bearing before any
of the findings above were written. All seven findings were about behaviour no test *covered*,
not about tests that passed for the wrong reason -- which is a different and better place for a
change to be than the previous three issues were at the same stage.

The Bitcoin and Kaspa suites were the control for the shared-decoder change and stayed
byte-identical through it, through seven review items and through two rounds of fixes.
