# Architecture

How the backend is arranged: how money is represented, which layer may import which, how the
application reaches the outside world, and how a request is authenticated. The first two are
the decisions that are easiest to undo by accident.

## Money

### The representation, end to end

| Where | Representation |
|---|---|
| Python | `decimal.Decimal` |
| SQLite | `TEXT`, via the `NumericText` type decorator |
| On-chain quantities | `INTEGER` base units (satoshis, sompi) plus a `decimals` column |
| Over the wire | a JSON **string** |
| TypeScript | a `string`, formatted with `decimal.js` |

One rule underneath all five rows: a monetary value never becomes a binary floating point
number, at any point in its life.

`float` is banned outright in `domain/`, `services/` and `providers/`. An AST test,
`backend/tests/security/test_no_float.py`, walks every module in the three and fails the
build, naming the file and the line, on:

- a float literal;
- the name `float` in any position: a call, an annotation, an `isinstance`;
- an import that binds the builtin under another name, reported at the import and at every
  use of the new name;
- true division of two integer literals, such as `1 / 3`.

A float produced at run time from names is beyond what a syntax walk can decide, so the
boundaries refuse one instead. `require_amount` in `domain/money.py` refuses anything that
is not a `Decimal` before converting it. `NumericText` and `BaseUnits` refuse a float on the
way into the database, and `BaseUnits` refuses one on the way back out. The wire type
`MoneyStr` refuses a JSON number.

The precision is 38 significant digits (`MONEY_PRECISION`), and the rounding mode is
`ROUND_HALF_EVEN` (`MONEY_ROUNDING`). Both are defined once, in `portfolio/domain/money.py`,
and the module's own arithmetic does not depend on the calling thread's decimal context:

- `quantize` is the single definition of how money rounds. It rounds in an explicit context
  built from those two constants, so a caller inside a `decimal.localcontext()` gets the
  same answer as one outside it.
- `add`, `subtract` and `multiply` are exact. They never round, whatever the ambient context
  says.
- `divide` rounds exactly once, through `quantize`.

At import, the module also raises the ambient precision to 38, in `decimal.DefaultContext`
and not only in `decimal.getcontext()`. The latter is thread-local, and this process does
real work in worker threads. That is a convenience for other code: it sets the precision
only, and nothing in `money.py` relies on it.

### Why `sqlalchemy.Numeric` is forbidden

SQLite has no decimal storage class. Its numeric affinity is an 8-byte IEEE-754 `double`,
and `sqlalchemy.Numeric` on SQLite converts through exactly that on the way in and on the
way out.

Measured on this project's SQLAlchemy version, against an in-memory SQLite database with a
`Numeric(38, 20)` column:

```
wrote   12345678901234567890.12345678901234567890
read    12345678901234567168.00000000000000000000
```

`typeof()` on the stored value reports `real`. No exception, and — checked, not assumed —
no warning either: the value is written, accepted, and returned as a `Decimal` of the
declared scale, so every type annotation in the codebase still reads correctly while 722
units of the amount have evaporated. A smaller value may survive the round trip intact,
which is worse rather than better: the corruption depends on the magnitude, so it passes
the first test anyone writes and fails in production. It surfaces months later as a total
that is off, with no commit to blame.

`NumericText` stores the canonical fixed-point string instead — `format(value, "f")`,
never scientific notation, always exactly the column's declared scale, one spelling of
zero. SQLite stores that byte for byte and hands it back unchanged, and `Decimal(text)`
reconstructs the exact value. The scale is a required constructor argument, because a
money column without a declared scale has no defined rounding.

Its three over-precision rules are deliberately different:

- digits past the declared scale are rounded away, by `quantize`, because that is what
  declaring a scale means;
- an amount with more integer digits than the scale leaves room for
  (`MONEY_PRECISION - scale`) is refused, because no rounding preserves it;
- a non-zero amount that would round to zero is refused, because zero is the one wrong value
  nobody downstream questions.

On-chain quantities do not use it. A satoshi and a sompi are indivisible and every chain
API reports them as integers, so `BaseUnits` stores the integer count, and the `decimals`
column on the same `balance_snapshots` row records the exponent to read it with. That
exponent is the one the provider declared when it read the balance, not a lookup in
`assets` at query time, so editing an asset row cannot reinterpret a reading already taken.
There is nothing to round, so nothing can round wrongly. The ceiling is SQLite's signed
64-bit integer; `BaseUnits` rejects anything past it rather than truncating, and refuses a
stored value that does not come back as an integer.

`test_no_float.py` bans `Numeric` everywhere in the backend package, together with its
subclasses `DECIMAL`, `Float` and `REAL`, which reach SQLite the same way. An aliased
import is caught too.

### Why money is never aggregated in SQL

Because a money column is `TEXT`, and every SQL operation that treats it as a number
coerces it to a float first:

- `SUM(amount)` and `AVG(amount)` apply numeric affinity, which is the `double` again —
  the exact thing `NumericText` exists to avoid, reintroduced in the one place where it
  is applied to every row at once.
- `ORDER BY amount` on `TEXT` sorts lexicographically, so `"9.00"` sorts after `"10.00"`.
  Casting it to make the sort correct is the float coercion again.
- `WHERE amount > :threshold` has both problems: a string comparison that is not a
  numeric one, or a cast that is lossy.

So: **load the rows and aggregate in Python**, with `Decimal`. No `SUM()`, no `ORDER BY`,
no `<` or `>` on a money column. Sorting by value is done in Python too, on the `Decimal`
values, after loading.

The cost of that is the reason it is affordable here: one user, thousands of rows. A
portfolio with a decade of daily fills is a table of tens of thousands of rows on a
machine with gigabytes of memory, and reading all of them costs milliseconds. Trading
correctness for a performance gain nobody would notice is a bad trade, and a product whose
core output is a set of numbers cannot afford numbers that are almost right.

`NumericText` deliberately does not make this ergonomic. A zero-padded, offset-encoded
storage form that sorted correctly as text was considered and rejected: it would make
`ORDER BY` on a money column work, and making the forbidden approach comfortable is worse
than leaving it visibly broken.

Half of this is mechanical. `test_no_float.py` fails the build on `SUM(`, `AVG(` or
`TOTAL(` in any SQL string in the backend package, docstrings aside, and on `func.sum`,
`func.avg` or `func.total`. An `ORDER BY` or a comparison on a money column is not caught
by any test; it is for review to catch.

## Layering

```
{ api , cli } -> services -> { repositories , providers } -> db -> config -> domain
domain        -> nothing
```

Enforced by `import-linter` contracts in `backend/.importlinter`, run in CI and by
`python scripts/check.py`. The first line is the `portfolio` layers contract, top to
bottom. A layer may import any layer below it, and the two packages inside one pair of
braces are siblings that may not import each other.

- **`api`** is the HTTP entry point. Its routers parse a request, call a service, serialize
  the result. They may not import `repositories`, `providers`, `sqlalchemy` or `httpx`; a
  separate `forbidden` contract says so by name, because "thin" is otherwise a matter of
  opinion.

  What makes that hold in practice is that a router is never handed a database session.
  `api.dependencies` opens the session, builds the service around it and yields the
  service; the router's signature mentions neither `AsyncSession` nor a repository, so
  the contract has nothing to catch because there is nothing to write.
- **`cli`** is a sibling of `api`, not a layer above it. Both are entry points onto the
  same services, and neither imports the other. `python -m portfolio create-user` and the
  first-start bootstrap both create the account through `AuthService.create_user`, which
  applies the password policy, and the login endpoint verifies through the same hasher.
  That is the point: an account created from the command line and one created by the
  bootstrap path cannot end up under different rules.
- **`services`** hold the business logic and may not import `fastapi`. A service that
  raises `HTTPException` has put an HTTP concern in the only layer that should be
  testable without one.
- **`repositories` and `providers`** are siblings: the database side and the outside-world
  side never import each other. Both may use the layers below them. A provider reads its
  base URLs and keys from `config`, and the price and fill parsers read `PRICE_SCALE` and
  `FILL_SCALE` from `db.models`, so that a parser refuses a value its column would round.
- **`db`** sits above `config` and `domain`. This is the one place a persistence concern is
  allowed to depend on the domain vocabulary, and it exists so that `NumericText` rounds
  by `domain.money.quantize` rather than carrying a second copy of the rounding rule.
  Two copies of "how money rounds" is how the two stop agreeing.
- **`config`** sits directly above `domain`. It reads the environment, and making it a
  layer is what makes the one direction that matters a violation: a `domain` module
  importing `get_settings` would put an environment read inside the layer whose whole
  property is that it has none. `config` imports `domain` itself, so the bootstrap password
  is measured against the same password policy as every other password.
- **`domain`** imports nothing from the application: no I/O, no clock, no network, no
  ORM. The current time is passed in as an argument. That is what makes a domain
  calculation reproducible from its inputs alone, in a test and in an incident.

`portfolio.main` sits in no layer, with `portfolio.logging` and `portfolio.web`. It is the
composition root: it builds the engine, the HTTP client, the providers and the timers, and
hands each service what it needs. "Who calls a provider, and when" below says why that
matters.

### The contracts

| Contract | What it forbids |
|---|---|
| `portfolio` | an import that points up the diagram, or across between siblings |
| `thin-routers` | `api.routers` importing `sqlalchemy`, `httpx`, `repositories` or `providers` directly |
| `framework-free-services` | `services` importing `fastapi` |
| `prices-are-never-fetched-in-a-request` | `api.routers` reaching `providers.prices`, directly or through anything else |
| `api-never-reaches-an-exchange-provider` | anything under `api` reaching `providers.exchanges`, directly or through anything else |
| `domain-is-pure` | `domain` importing a framework, an HTTP client, or a standard-library module that does I/O or introduces nondeterminism |

The clock is a call rather than an import, so `domain-is-pure` cannot see it.
`backend/tests/security/test_domain_has_no_clock.py` forbids it by walking the syntax tree
of every module under `domain/`.

`backend/tests/test_import_contracts.py` pins the file to exactly these six contracts. For
the prices, exchange and domain contracts it also plants a violation and asserts that the
contract reports it, because a forbidden module that nothing imports yet makes a contract
pass without checking anything.

## Providers

Everything the application learns from outside the process, it learns through
`portfolio.providers`. This section is the shape of that package. `docs/providers.md` holds
the detail: each vendor's endpoints, rate limits and retention windows, what is confirmed
and what is not, how to add a provider, and the logging rules a provider follows.

### Three families

**Chain balance providers**, in `providers/chains/`, read what an address holds.

- Protocol: `ChainProvider`, in `providers/base.py`, with `capabilities`,
  `validate_address`, `fetch_balances` and `health`. `validate_address` is synchronous and
  offline: it delegates to the address codecs in `domain/`.
- Capabilities: `ChainCapabilities` declares `chain_key`, `decimals` and
  `max_addresses_per_call`. `KaspaProvider` sizes its batches with `chunk_addresses`;
  `EsploraProvider`, at 1, reads one address per request. Either way the caller hands over
  every address and never needs to know which chain can batch.
- Implementations: `EsploraProvider` for Bitcoin and `KaspaProvider` for Kaspa. Each can fail
  over from a primary to a fallback instance of its vendor's API through `EndpointSet`, in
  `providers/endpoints.py`. Bitcoin has a fallback by default; Kaspa only when one is
  configured.
- Lookup: each class registers itself with `@register_chain_provider(ChainKey.X)`, and
  `providers/chains/__init__.py` imports each module by name. `get_chain_provider(chain_key,
  client)`, in `providers/registry.py`, builds one. Nothing is discovered: a provider that
  was never imported fails as `UnknownChainError` at the call site.

**Price sources**, in `providers/prices/`, read what an asset costs in USD or EUR.

- Protocol: `PriceSource`, in `providers/prices/base.py`, with `name`, `pairs` and `fetch`.
- Capabilities: `pairs`, every pair the source can answer. `sources_for` filters the global
  order by it, so a pair a vendor does not list costs no request.
- Implementations: `KrakenPriceSource`, `CoinbasePriceSource`, `KaspaPriceSource` and
  `CoinGeckoPriceSource`, built in that order by `price_sources(client)`, in
  `providers/prices/registry.py`. CoinGecko is built only when its key is configured.
- Failover is across vendors, per pair, in `fetch_prices`. A source that answers part of
  what it was asked leaves the rest to the next one, and a pair nobody answered comes back
  as unanswered rather than as a zero.

**Exchange providers**, in `providers/exchanges/`, read an account's spot fills and its spot
balances.

- Protocol: `ExchangeProvider`, in `providers/exchanges/base.py`, with `capabilities`,
  `fetch_fill_page`, `candidate_symbols` and `fetch_balances`. Fills come one page at a
  time, because the sync commits a checkpoint between pages.
- Capabilities: `ExchangeCapabilities` declares `retention`, `max_query_window`,
  `page_size`, `cursor_kind`, `rate_limit` and `requires_symbol`. The exchange sync plans
  its windows and pages from them without knowing which venue it is talking to.
  `rate_limit` is the one nothing reads: the shared limiter below is stricter than either
  venue's documented limit.
- Implementations: `BitgetProvider` and `BingXProvider`, built by
  `exchange_providers(client)`, in `providers/exchanges/registry.py`, only for the venues
  whose credentials are configured. A venue without credentials has no provider object at
  all, so no code path can sign a request without a key.
- Requests are signed with HMAC-SHA256 by `providers/exchanges/signing.py`, over a
  `Credentials` value that cannot render its secret.

### What the three have in common

- **Structural protocols, checked by `mypy --strict`.** None of the three is
  `@runtime_checkable`: `isinstance` against a protocol compares attribute names and
  nothing about their signatures. A provider satisfies its protocol by its shape, and the
  type checker decides whether it does.
- **Contracts enforced by construction.** A provider hands what it parsed to a shared
  helper rather than promising to be careful. `align_balances` returns one result per
  requested address, in order, with an address the vendor did not mention as a zero.
  `assemble_fill_page` and `assemble_balances` do the same for fills and balances.
  `fetch_prices` discards a response that answers a pair nobody asked about.
- **One error vocabulary.** `providers/errors.py` defines `ProviderUnavailableError` (the
  vendor did not answer: retry later, keep the last reading), its subclass
  `ProviderRateLimitedError`, `ProviderResponseError` (it answered, and the answer cannot be
  trusted) and `UnknownChainError`. The seven exchange errors in
  `providers/exchanges/errors.py` subclass them. No message carries an address, a URL or a
  response body.
- **Money crosses the boundary exactly.** `decode_json`, in `providers/base.py`, is the one
  JSON decoder. It parses a JSON number with `parse_float=Decimal`, so a price arrives with
  the digits the vendor sent, and it refuses `NaN` and `Infinity`. Chain balances stay
  integer base units, prices and fills are `Decimal`, and every duration is an integer
  number of milliseconds.

### The shared HTTP client and the host rate limiter

`build_http_client()`, in `providers/http.py`, returns the `httpx.AsyncClient` every
provider is constructed with. `portfolio.main.lifespan` builds one per application,
publishes it as `app.state.http_client` and closes it at shutdown. `cli.run_price_refresh`
builds one for the life of one command. No provider builds its own.

The rules live in a transport, `RetryingTransport`, rather than in a helper function. A
helper has to be remembered; a transport cannot be bypassed, so every request through the
client follows the rules whether or not its author knew them. Every request gets:

- **`HostRateLimiter`**: a minimum interval between two requests to the same host and port,
  `DEFAULT_MIN_HOST_INTERVAL_MS`, one second. It is acquired before every attempt, retries
  included. A response whose `ratelimit-remaining` is zero pushes the next slot back by the
  reset it names, and never brings one forward;
- bounded retry under `RetryPolicy`: three attempts, full-jitter exponential backoff, a
  30-second ceiling, on a transport error, a 429 or a 5xx, honouring `Retry-After`;
- retry for `GET` and `HEAD` only, unless one request declares itself safe to repeat with
  `IDEMPOTENT_EXTENSION`. Kaspa's batch balance read is a `POST` and opts in that way;
  widening the policy instead would also make every future exchange `POST` retryable.

The client adds two settings of its own: explicit connect, read, write and pool timeouts
(`DEFAULT_TIMEOUT`), and no redirects followed. They are client settings rather than
transport rules, so a single request could override them; none does.

**One client per process is a requirement, not tidiness.** The limiter's state lives on the
transport and the transport lives on the client, so a second client would keep its own idea
of the interval, and the effective floor would silently become half of what it says.

**The transport does not translate failures.** A transport error leaves it as the
`httpx.TransportError` it is, and a failing status leaves as a response. The provider
decides which `ProviderError` each one is, because that is a judgement about what the vendor
meant, not about how the bytes moved.

#### How a request is logged

- The transport logs each request's outcome, at error for a failure and at debug otherwise,
  and a warning for each retry. Its target is `request_target`: `{scheme}://{host}/{label}`.
  **The path and the query are never logged.** Both chain APIs put the address in the path, and BingX signs its
  requests in the query string.
- The label is a constant the provider sets in the request's `endpoint` extension, such as
  `ADDRESS_BALANCE`. A label that is not in `ENDPOINT_LABELS` is logged as `<unlabelled>`,
  so saying more about a request is an edit to a named constant.
- `strip_query` removes the query, the fragment and any userinfo from a URL. It is not what
  the transport logs: for a chain request the address is in the path, so removing the query
  alone would still leak it.
- Underneath, `portfolio/logging.py` holds the `httpx` and `httpcore` loggers at WARNING,
  because `httpx` logs every request URL at INFO. Its redaction pipeline also replaces the
  query of any URL in any record, with addresses, extended keys and loaded secrets, so a
  URL that reaches a log by another route still loses its query.

### The extended-key scanner

- `ExtendedKeyScanner`, in `providers/base.py`, is the one optional capability a chain
  provider may have: `scan_extended_key(key, known)` returns an `ExtendedKeyScan`. Only
  `EsploraProvider` implements it.
- It is `@runtime_checkable`, unlike the three protocols above, and on purpose. The balance
  sync holds a `ChainProvider` and has to ask at run time whether it can also scan. A chain
  with an extended-key wallet and a provider that cannot scan fails as `internal`, rather
  than skipping the wallet and reading it as a zero. `mypy` still checks the signature.
- Neither public Esplora instance answers a lookup by extended key, so derivation is local.
  BIP32 public derivation, the SLIP-0132 prefixes and the gap-limit rule live in
  `domain/extended_keys.py`, with `domain/secp256k1.py` and `domain/ripemd160.py`. They are
  pure: no socket, no clock, no database. The provider owns only the reads.
- A scan refuses everything it can before its first request: a key that does not parse, a
  key for the other network than `PORTFOLIO_BITCOIN_NETWORK`, a persisted address that does
  not validate. It then reads every persisted address on the receive branch, then the change
  branch, and extends each branch until its last `GAP_LIMIT` (20) addresses are unused, up to
  `MAX_ADDRESSES_PER_BRANCH` (1000).
- Every read takes the same path as `fetch_balances`, one at a time, so every request
  acquires the host limiter. A first scan is at least forty requests, so at least forty
  seconds against one host.
- `BalanceSyncService` loads the persisted addresses through `DerivedAddressRepository`
  before the reads. It writes the new and newly used ones in the chain's own commit, with
  the wallet's snapshot, which is the sum of what the scan read.

### Who calls a provider, and when

Three timers, two endpoints and one command, and each reaches a provider through a service
that is handed its providers rather than building them:

| Caller | Service | Handed | Writes |
|---|---|---|---|
| the `balance-sync` timer; `POST /api/balances/sync` | `BalanceSyncService` | `provider_for`, which calls `get_chain_provider` over the shared client | `balance_snapshots`, `derived_addresses`, `sync_runs` |
| the `price-refresh` timer; `python -m portfolio refresh-prices` | `PriceRefreshService` | the sources `price_sources` built | `prices` |
| the `exchange-sync` timer; `POST /api/exchanges/sync` | `ExchangeSyncService` | the mapping `exchange_providers` built | `exchange_fills`, `exchange_sync_windows`, `exchange_balances`, `exchange_sync_runs` |

- **The timers** are `IntervalScheduler` instances, in `services/scheduler.py`, that
  `portfolio.main.lifespan` starts and stops. By default the balance and exchange syncs run
  every 15 minutes and the price refresh every 60. Each is its own task with its own switch,
  so a failing vendor of one kind stops no other. Each runs at startup only when its last
  run is older than one interval, so a container that crash-loops does not hit a public API
  on every restart. The price timer counts only successful refreshes, so while every source
  fails it refreshes once per restart (`docs/providers.md`). The exchange timer exists only when at least one venue is configured. A
  fourth timer takes backups and calls no provider.
- **The two endpoints** go through a `SyncCoordinator`, in `services/sync_coordinator.py`,
  one per kind, and so do the balance and exchange timers' ticks. The price timer has none:
  no endpoint can ask for a refresh, so there is nothing to join. A second caller joins the run in flight
  instead of starting another, so a double-clicked refresh costs a public index nothing.
- **Nothing in a request path reaches a price source.** The price refresh has no endpoint,
  and `services/prices.py`, through which the dashboard reads prices, imports no provider.
  A request renders from the `prices` table.
- **`GET /api/health/detail` calls no vendor.** It reports each source's last recorded
  outcome. `ChainProvider.health()` is part of the protocol, and no production code calls
  it.
- **`portfolio.main` and `portfolio.cli` are the only places a provider is constructed.** A
  service depends on the protocol and never on which vendors exist, which is what lets a
  test drive a whole sync with fakes and no network.

### Why the boundaries run where they do

- **A provider imports nothing above it.** `providers` sits below `services` in the layers
  contract, so it cannot import `services`, `api` or `cli`. A provider is a client of
  someone else's API and knows nothing of who asked, which is what lets one class serve a
  timer, a CLI command and a test with no server running. It does not import `fastapi`
  either, but no contract names that package for `providers`: `framework-free-services`
  names it for `services` and `domain-is-pure` for `domain`. For `providers` it holds by
  convention.
- **A router imports no provider.** `thin-routers` forbids the direct import. A provider
  call inherits the vendor's latency, its outages and its rate limit, so a request path that
  reaches one by accident turns a page load into a vendor call.
- **For prices and exchanges, the indirect path is forbidden too.**
  `prices-are-never-fetched-in-a-request` and `api-never-reaches-an-exchange-provider` are
  written without `allow_indirect_imports`, so `router -> service -> provider` fails as
  surely as a direct import. That is the change a well-meaning edit introduces: "just
  refresh it if it is stale" in a read-side service. The exchange contract covers all of
  `portfolio.api`, schemas and dependencies included, because `providers.exchanges` is the
  package that holds the owner's credentials.
- **The sanctioned paths go through `portfolio.main`.** `POST /api/balances/sync` and
  `POST /api/exchanges/sync` call a coordinator whose runner is a closure `main.py` built
  over the providers. `portfolio.main` is in no layer and not under `portfolio.api`, so
  that path crosses no contract. `app.state` publishes only `configured_exchanges`, the set
  of venue keys, and never the providers.

## Authentication

Single user, one password, opaque server-side sessions. No JWT and no refresh tokens: a
stateless token cannot be revoked, and revocation is the only session behaviour this
product actually needs.

Two hashes, for two different threats:

- **Argon2id** over the password, because a password is low entropy and guessable, so the
  hash has to be slow and memory-hard. The cost parameters are settings, floored at the
  OWASP minimum in production, and measured on the deployment hardware rather than
  copied — see `docs/operations.md`.
- **SHA-256** over the session token, because the token is 32 bytes from
  `secrets.token_urlsafe` and no amount of offline work recovers 256 bits of entropy. The
  hash only has to be preimage resistant. Running Argon2id per request would add a quarter
  of a second to every page load for no gain.

Only the hash is stored, so a leaked database file yields no usable session.

Two expiries apply at once: a sliding idle window and a hard ceiling that activity never
extends, 7 and 30 days by default (`PORTFOLIO_SESSION_IDLE_DAYS` and
`PORTFOLIO_SESSION_ABSOLUTE_DAYS`). Whichever comes first ends the session.

Authorization is **deny-by-default, in middleware**. Any path under `/api` outside
`PUBLIC_API_PATHS` requires a session, and that allowlist holds exactly two paths:
`/api/health`, for the container's health check, and `/api/auth/login`. The API's own
documentation, `/api/docs` and `/api/openapi.json`, is not on it. A `Depends` on each
router would be the more conventional shape and is the wrong one here: forgetting it on one
endpoint is the exact failure mode, and a rule that can be forgotten is a rule that
eventually is. A contract test walks every registered route and asserts `401` without a
cookie, and a second pins the allowlist's contents.

The same middleware, `RequestGuardMiddleware` in `api/middleware.py`, enforces a matching
`Origin` and a `Content-Type` of `application/json` on every request whose method is not
`GET`, `HEAD` or `OPTIONS`. A missing `Origin` is refused like a wrong one. The
content-type rule is the one that does the work: a form-encoded POST is the shape an HTML
form can send cross-site without a preflight, so refusing it closes CSRF without a token
round-trip. `SameSite=Lax` on the cookie is the belt to that pair of braces.

See `docs/specs/003-single-user-password-login.md` for the decisions and what they cost.

## Related documents

- `CLAUDE.md` — the working agreement, and the enforcement behind each rule.
- `docs/providers.md` — each provider's endpoints, limits and confirmed facts, and how to
  add one.
- `docs/operations.md` — running the instance: the account, the password hash, sessions.
- `docs/deployment.md` — how a merge becomes a running container.
- `docs/accounting.md` — how fills become cost basis, average cost and realized P&L, with
  worked examples.
- `docs/adr/` — architecture decision records, starting with weighted-average cost basis and
  why it is not a tax figure.
- `docs/specs/` — the per-issue implementation specs.
