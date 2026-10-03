# 030 — Observability: redaction by value, request ids, and the sources' health

Issue: #23
Status: done

## Problem

The logging pipeline redacts by **key name** only (`portfolio/logging.py`). It does not see:

- a secret inside a string, such as an exception message, the `event` text, or a URL;
- an address or an extended public key bound under a key whose name is not sensitive;
- any record written through the standard library rather than structlog: `httpx`,
  `aiosqlite`, `uvicorn`. Those reach stdout through a `"%(message)s"` handler without
  passing a single processor. Two named vendor floors close the two leaks measured so far.
  The module's docstring states that the general case is still open.

Nothing correlates the records one request writes. Uvicorn's access log bypasses structlog,
is not JSON in production, and carries the raw path and query.

`GET /api/health/detail` (#22) reports the backups only. The state of the balance sync, the
exchange sync, the prices, the four timers and the reconciliation is spread over other
endpoints or held only in memory. The timers record nothing at all about their ticks.

## Scope

1. Redaction by **value**, applied to every string in every record, structlog's and the
   standard library's alike:
   - every loaded secret;
   - extended public keys;
   - addresses;
   - the query string of any URL.
2. Every standard-library record goes through the same processors and the same renderer,
   uvicorn's included.
3. A request id bound to every record a request writes, and returned as `X-Request-ID`.
4. One structured `request_completed` line per request, replacing uvicorn's access log.
5. A sentinel test: every secret setting set to a sentinel, the application exercised at
   DEBUG through its error paths, and no sentinel in any output.
6. `GET /api/health/detail` gains the timers, the balance sync per chain, the exchange
   accounts, the prices and the reconciliation, beside `backup`.
7. The Health page shows each of them.
8. `diff-cover` in CI enforces 90% on changed lines, backend and frontend.

## Non-goals

- **Live calls to providers from the health endpoint.** The page refetches every minute, and
  the dashboard shares the query. A health check that calls a vendor's API would multiply
  the calls the rate limits are budgeted for. Each source's health is what its last recorded
  attempt says. `ChainProvider.health()` stays unused by production code.
- New dashboard notices. The backup notice from #22 stays the only one.
- Validating `PORTFOLIO_LOG_LEVEL`. An unknown value still falls back to INFO. Making it a
  startup failure is a separate change with its own deploy risk.
- Redacting by the registered wallets' exact values. The address patterns below catch every
  supported form whether or not it is registered, and need no database at logging time.
- Lowering anything. The issue's coverage numbers (backend 85/75, frontend 80/70) are below
  today's floors (backend 99.7% total with branches, domain 95/90 measuring 100/100, frontend
  100% on all four metrics). Rule 5 keeps the current floors, so that criterion is already met.

## Design: logging (`portfolio/logging.py`)

### The secret set

`secret_values(settings) -> frozenset[str]` takes every field of `Settings` whose value is a
`SecretStr`, unwraps it, and keeps the non-empty values. It finds the fields by walking the
model's fields, not from a list. A `SecretStr` setting added later is covered without editing
this module, and a test pins that every `SecretStr` field is found.

`configure_logging` builds the set once and hands it to the processor. A second call replaces
it. The set is never logged, never returned and never written anywhere.

### `redact_values`, a processor

It walks every string value in the event dict, recursively through mappings, lists, tuples
and sets as `_redact_value` does today, **including `event` and `exception`**. It runs after
`format_exc_info`, so a traceback is a string by then. In each string it replaces:

1. **Every loaded secret.** A secret of 8 characters or more is replaced wherever it occurs as
   a substring. A shorter one is replaced only where it is the whole string, because a
   3-character secret matched as a substring would redact ordinary words. Longest first, so
   one secret that contains another is replaced whole.
2. **Extended public keys:** one of `xpub ypub zpub tpub upub vpub Ypub Zpub Upub Vpub`
   followed by 100 or more Base58 characters. This covers the six prefixes
   `domain/addresses.py` refuses, plus the multisig forms.
3. **Addresses**, every form this application accepts, mainnet and testnet alike:
   - bech32 and bech32m with the human-readable part `bc`, `tb` or `bcrt`, either case;
   - Base58Check P2PKH and P2SH, starting `1`, `3`, `m`, `n` or `2`, 25 to 34 Base58
     characters after the first, not preceded or followed by a letter or a digit (R14);
   - Kaspa: `kaspa`, `kaspatest`, `kaspasim` or `kaspadev`, then `:`, then 61 to 63
     characters of its alphabet.

   Nothing verifies a checksum. A false positive costs a redacted log word, and a false
   negative costs the owner's holdings, so the pattern errs toward redacting.
4. **The query string of any URL:** in `scheme://…?query`, everything from `?` to the next
   whitespace, quote or `#` becomes `?[REDACTED]`. The scheme, host and path stay. One
   exchange signs its requests in the query, and this is the mechanism the vendor list was
   standing in for.

Each replacement is the existing `REDACTED` marker. The key-name rule (`redact_sensitive`)
stays as it is, and runs first.

The residual, stated in the module docstring and pinned by a test: a value that is none of
the above is printed. For example, an exception message carrying an arbitrary row value.
`test_the_pipeline_does_not_redact_an_exception_message` keeps its sentinel, which is none of
the four. Its docstring is rewritten to say what *is* now covered and what is not.

### One pipeline for every record

The root handler's formatter becomes `structlog.stdlib.ProcessorFormatter`.

- structlog records end their chain with `ProcessorFormatter.wrap_for_formatter`.
- Standard-library records enter through a `foreign_pre_chain`: the log level, the logger's
  name, the timestamp, and `merge_contextvars`, so they carry the request id too.
- The formatter's own processors are shared by both: `format_exc_info`, `redact_sensitive`,
  `redact_values`, then the renderer (JSON in prod, the console renderer in dev).

So an `httpx`, `aiosqlite` or `uvicorn` record is redacted and rendered like any other. The
vendor floors (`SILENCED_VENDOR_LOGGERS`, `VENDOR_LOG_FLOOR`) stay as defence in depth. The
docstring that calls the general case "still open" is rewritten: it is now closed by
mechanism, and the lists are the second layer.

The existing security tests (`backend/tests/security/`, `EveryRecord`, `production_logging`)
must keep passing. Where one asserts the old rendering of a foreign record, the tester
updates it to the new one and says so.

### Uvicorn

`configure_logging` gives `uvicorn`, `uvicorn.error` and `uvicorn.access` no handlers of their
own and `propagate = True`, so their records reach the root handler and its formatter. It
also raises `uvicorn.access` to WARNING: `request_completed` replaces the access line. This
works under the Dockerfile's `uvicorn` command and under `--reload` in dev alike, because
`create_app` runs after uvicorn has applied its own logging configuration. The Dockerfile
does not change.

### The request id (`api/request_context.py`, new)

`RequestContextMiddleware` is a **pure ASGI** middleware, added last so it is the outermost of
the application's own middleware. It is not a `BaseHTTPMiddleware`: one of those runs the
rest of the application in a child task, so a context variable bound below it would never
reach the outer layers.

For each `http` scope:

- It makes `request_id = uuid4().hex`, calls `clear_contextvars()`, then
  `bind_contextvars(request_id=...)`.
- An inbound `X-Request-ID` is **ignored**: never trusted, never logged, never echoed.
- It adds `X-Request-ID: <id>` to the response start message.
- After the response, it logs `request_completed` with:
  - `method`;
  - `route`, the matched route's path template, or `"unmatched"` when nothing matched;
  - `status`;
  - `duration_ms`, an integer from a monotonic clock.

  It never logs the raw path or the query. The level is INFO, except DEBUG for
  `GET /api/health`, which the container's health check calls every 30 seconds.
- It does not clear the context at the end. Each request runs in its own task context, and
  the 500 handler runs after this middleware returns.

The unhandled-exception handler (`api/errors.py`) runs in Starlette's `ServerErrorMiddleware`,
outside every middleware. It reads the id from the context variables and sets `X-Request-ID`
on its 500 response, so every response carries it, 500 included. `request_failed`,
`request_refused` and `unhandled_exception` carry the id through `merge_contextvars`, with no
edit at their call sites.

## Design: the sources' health

### The timers (`services/scheduler.py`)

`IntervalScheduler` records, through its injected clock and in memory:

- `started_at`, set when the loop starts;
- `last_tick_started_at`;
- `last_tick_finished_at`;
- `last_tick_succeeded: bool | None`.

`status(now) -> SchedulerStatus` answers `state`, `last_tick_at` (the last finished tick) and
`last_tick_succeeded`. The state rule is pure, in `domain/health.py`:

| `state` | When |
|---|---|
| `stopped` | the scheduler exists and its task is not running |
| `late` | running, and either a tick is in flight with `now - last_tick_started_at > 2 × interval`, or none is and `now - (last_tick_finished_at or started_at) > 2 × interval` (R13) |
| `ok` | running, and not late |

The loop's first tick can wait up to one interval after start. A tick in flight longer than
two intervals is also `late`, which is the point. A timer that was never built, because its
setting turned it off, is served as `disabled` by the health service. Four timers are served,
by the names they already have: the balance sync, the price refresh, the exchange sync and
the backup.

### The sources

Each source's health is what its last recorded attempt says. No vendor is called.

- **`chains`**, one entry per chain key that appears in the latest finished balance run or in
  the wallet registry, sorted by key. Each entry has:
  - `state`: `ok` (that run's outcome for the chain succeeded), `failing` (it failed or was
    partial), or `never` (no finished run has an outcome for it);
  - `last_success_at`: the `finished_at` of the newest run whose outcome for that chain
    succeeded. The read orders by run id, an `INTEGER`, never by a `TEXT` datetime;
  - `last_error_kind`: the latest run's `error_kind` when failing, otherwise null.

  **Never `detail`**: it is the provider's text, which this endpoint does not need.
- **`exchanges`**, one entry per exchange account, by `exchange_key`. Each has:
  - `sync_state`, the account's `sync_status` value;
  - `last_synced_at`;
  - `balances_state`: `ok` (a reading and no error), `failing` (an error) or `never`;
  - `balances_read_at`.
- **`prices`**: `state` (`fresh`, `stale` when `now - latest_fetched_at > STALE_AFTER`, or
  `never`) and `latest_fetched_at`. `PriceRepository.latest_fetched_at` exists.

### The reconciliation

A pure `summarize_reconciliation(view)` in `domain/health.py` reduces `ReconciliationView` to:

- `state`. The first that applies, in this order:
  1. `not_computed`: `computed_at` is null.
  2. `mismatch`: any asset's status is not `match`.
  3. `incomplete`: any exchange is not compared, or any wallet is stale, unread or
     `chain_failed`.
  4. `match`.
- `computed_at`;
- `assets_compared`, `assets_mismatched` and `sources_not_compared`, as integers.

No quantity, asset name or tolerance is served. The page links to the existing
reconciliation view for those. The dev measures `reconciliation()` on a realistic scratch
database and reports its time. It runs once a minute per open page.

### The service (`services/health.py`, new) and the endpoint

`HealthService.detail(user_id) -> HealthDetail` composes:

- `BackupService.status()`;
- the four timers' status, handed in from `app.state` through the dependency;
- the sources;
- the reconciliation.

**A section that fails does not fail the others.** Any exception while building `chains`,
`exchanges`, `prices` or `reconciliation` is logged as `health_section_failed`, with
`section` and `error_type`, and that section is served as `{"state": "unavailable"}`, with
every other field null or empty. `backup` and the timers already never raise.

The router stays thin: parse, call, serialize. The response model grows beside `backup`:

```
backup:          (unchanged)
schedulers:      [{name, state: ok|late|stopped|disabled, last_tick_at, last_tick_succeeded}]
chains:          {state: ok|unavailable, items: [{chain_key, state: ok|failing|never,
                  last_success_at, last_error_kind}]}
exchanges:       {state: ok|unavailable, items: [{exchange_key, sync_state, last_synced_at,
                  balances_state: ok|failing|never, balances_read_at}]}
prices:          {state: fresh|stale|never|unavailable, latest_fetched_at}
reconciliation:  {state: match|mismatch|incomplete|not_computed|unavailable, computed_at,
                  assets_compared, assets_mismatched, sources_not_compared}
```

Every state is a `StrEnum` whose members are the wire form, so the generated TypeScript
types are unions and the page's wording tables can be total. **No configuration value is
served**: no interval, path, URL, key, tolerance or age limit. The endpoint stays
authenticated, and nothing is added to `PUBLIC_API_PATHS`. `summary` and `operationId`
(`getHealthDetail`) stay. The summary text is updated.

## Design: frontend

The Health page gains four sections after **Backups**: **Timers**, **Balance sync** (chains),
**Exchanges** with **Prices** beside it, and **Reconciliation**.

- Each section renders every state with a wording table in `lib/health.ts`. Each table is
  total over its union, as `lib/backups.ts` does.
- Dates are rendered as the backup section renders them.
- `unavailable` is one sentence: "Could not be read. The log says why."
- The reconciliation section links to the existing reconciliation view.
- The query stays `useHealthDetail()`. Nothing new is fetched.
- The page works at 375 px with no horizontal scroll.
- The dashboard does not change.

## Design: CI

- `diff-cover` is added to the backend's dev dependencies.
- In `ci.yml`, on `pull_request` only:
  - the backend test job writes `coverage.xml` and runs
    `diff-cover coverage.xml --compare-branch=origin/main --fail-under=90`;
  - the frontend test job writes LCOV and runs `diff-cover` on it the same way.
- The checkout fetches enough history for the comparison.
- The dev confirms the `diff-cover` version reads LCOV. If it does not, the dev reports
  that rather than dropping the frontend half.
- A push to `main` does not run it.
- A test pins the steps, as the workflow guardrails pin the others.
- `scripts/check.py` does not change.

## Documentation

`docs/operations.md` gets a section on logs:

- one JSON line per record in production;
- `request_id` and `X-Request-ID`, and how to find one request's records;
- `request_completed`;
- what is redacted (by key name, by value, by pattern, URL queries) and what is not (the
  residual);
- the vendor floors as the second layer.

It also covers `GET /api/health/detail` section by section: each state and what to do about
it.

## Acceptance criteria

1. The key-name rule is unchanged. Every loaded secret is redacted wherever it occurs in any
   string of any record: `event`, `exception`, nested values, and standard-library records. A
   `SecretStr` setting is covered by construction, and a test pins that every one is found.
2. Extended public keys of every listed prefix, and addresses of every supported form, are
   redacted wherever they occur. Tests use testnet fixtures only (`tb1`, `bcrt1`,
   `kaspatest:`, `tpub`). The mainnet alternatives are proven on the pattern itself, never by
   committing a mainnet-shaped literal.
3. Every standard-library record goes through the same processors and renderer, `uvicorn`'s
   included. It is JSON in prod.
4. **A full URL with a signature query parameter is never logged.** Proven with the vendor
   floor lifted, through a standard-library logger and through structlog alike.
5. Every record written during a request carries one `request_id`. Every response carries
   `X-Request-ID`: 200, 401, 404, 422 and 500. An inbound `X-Request-ID` is ignored. Two
   concurrent requests never share or swap an id.
6. `request_completed` carries the method, the route template, the status and `duration_ms`,
   never the raw path or query. It is at DEBUG for the health check. Uvicorn's access line no
   longer appears.
7. **Sentinel test:** every `SecretStr` setting set to a distinct sentinel, and the real app
   run in prod at DEBUG through sign-in, a balance sync, an exchange sync, a price refresh
   with failing providers, and a 500. No sentinel appears on stdout or in any record.
8. `GET /api/health/detail` serves `schedulers`, `chains`, `exchanges`, `prices` and
   `reconciliation` beside `backup`, with every state reachable. It serves no configuration
   value, calls no provider, and a failing section is `unavailable` while the rest answer.
   It is `401` without a session.
9. The Health page renders every state of every section at 1280 px and 375 px. Frontend
   coverage stays at 100%.
10. `diff-cover` at 90% runs on pull requests for both sides, and a test pins it.
11. Coverage floors are unchanged.
12. No float, no SQL aggregation or ordering on money or `TEXT` datetimes. The layering
    contracts hold, nothing is added to `PUBLIC_API_PATHS`, and the OpenAPI types are
    regenerated with no drift.
13. The full gate passes.
14. The documentation covers the logs and the health sections as above.

## File ownership

| Agent | Files |
|---|---|
| `backend-dev-23` | `backend/src/portfolio/logging.py`, new `api/request_context.py`, `api/errors.py`, `main.py`, `services/scheduler.py`, new `services/health.py`, new `domain/health.py`, `api/routers/health.py`, `api/schemas/health.py`, `api/dependencies.py`, any repository needing a new read (`sync_runs.py`, `exchanges.py`, `prices.py`), `backend/pyproject.toml`, `backend/uv.lock`, `.github/workflows/ci.yml`, `frontend/vite.config.ts` (the LCOV reporter only), `docs/operations.md`, and the regenerated `frontend/src/api/generated/schema.ts` |
| `frontend-dev-23` | `frontend/src/pages/HealthPage.tsx`, new files under `frontend/src/pages/health/`, new `frontend/src/lib/health.ts`, `frontend/src/api/health.ts` if a type alias is needed |
| `tester-23` | every test file on both sides, `frontend/src/test/**`, `tests/deploy/**`, and the gate. Sole gate owner |

The tech lead owns this spec and does the browser check at 1280 px and 375 px.

## Rulings

- **R1. Five sections, not four (`frontend-dev-23`).** Exchanges and Prices are two h3
  sections, so each heading can be found on its own.
- **R2. The reconciliation link goes to the dashboard's holdings check.** There is no
  reconciliation route. The link is a router `Link` to `/#holdings-check`, and it shows in
  every state. Declarative React Router does not scroll to a hash on a client-side
  navigation, and that is accepted.
- **R3. One alert for the new sections when the request fails.** With the existing Backups
  alert, a failed request shows two alerts, not five.
- **R4. `exchanges[].last_synced_at` is the last *successful* sync.** The repository sets it
  only together with `sync_status = ok`. The page labels it "Last successful sync".
- **R5. The request id is a hyphenated UUID, and nothing is exempt from redaction
  (`backend-dev-23`).** About 2.9% of `uuid4().hex` values match the Base58 address pattern,
  and the first implementation exempted a 32-hex `request_id` from value redaction. An
  exemption is a hole: a 32-hex API secret bound under that key would print. `str(uuid4())`
  has hyphens, so no run of characters long enough to look like an address exists, and no
  exemption is needed.
- **R6. The logged route is `root_path` plus the matched route's template.** These are the
  ASGI scope's documented fields. The first implementation read FastAPI's undocumented
  `scope["fastapi"]["effective_route_context"]`. If `root_path` plus the template does not
  give `/api/...` for every API route, the undocumented key stays, behind a test that fails
  loudly when a FastAPI upgrade removes it.
- **R7. No handler ever receives an unredacted structlog record.** With the redaction only in
  the root handler's `ProcessorFormatter`, a structlog record's `msg` is the raw event dict
  until that formatter runs. Any other handler, such as pytest's `caplog`, a test's
  `EveryRecord`, or one added later, would see the secret. So both redaction processors also
  run in structlog's own chain, before `wrap_for_formatter`. The formatter still runs them for
  standard-library records. Running them twice is harmless: `[REDACTED]` matches no rule,
  unless a loaded secret is a piece of the marker (R17).
- **R8. Accepted as built.**
  - `domain/health.py` takes Protocols, because `domain` cannot import a service type.
  - The chains are those of the newest finished run with an outcome for them, plus the
    active wallets' chains.
  - A reconciliation with nothing to compare is `match`.
  - A request refused before routing logs `route: "unmatched"`.
  - The frontend CI job's timeout goes from 5 to 10 minutes for the `uv` install that
    `diff-cover` needs. It is measured again after the first run.
  - Vitest's LCOV `projectRoot` is the repository root, because `diff-cover` matched no file
    against `SF:src/...` and passed silently.
- **R6, as measured.** `root_path` plus the template gives `/wallets/{wallet_id}` for all 25
  API operations. In FastAPI 0.141, `include_router(prefix=...)` keeps the original route and
  never extends `root_path`. So the undocumented `effective_route_context` stays, and a test
  pins the prefixed template for every API operation. A FastAPI upgrade that removes the key
  then fails that test rather than logging wrong routes.
- **R9. A request with no route template (`backend-dev-23`).** Starlette sets no
  `scope["route"]` for the single-page application, its assets, or FastAPI's documentation
  paths, so all of them logged `route: "unmatched"`, even with a 200. Now:
  - a path outside `/api` logs `route: "spa"`, at DEBUG, because one page load fetches
    several assets;
  - `/api/openapi.json`, `/api/docs` and `/api/docs/oauth2-redirect` log their own path. They
    have no parameter, so the path is the template. Take them from the application's
    configured URLs, not from a copy of the strings;
  - anything else under `/api` with no route stays `"unmatched"`. That is a 404, or a
    request refused before routing.
- **R10. Accepted residuals.**
  - A standard-library record reaches a handler other than the root's as the library wrote
    it. In production the root handler is the only one, and it redacts. A record factory or
    a per-logger filter would close this for handlers nobody has added, at the cost of
    touching every record twice. The module docstring states it.
  - A loaded secret that is a substring of `[REDACTED]` gains a bracket on each pass.
    Nothing is revealed.
- **R11. From the tech lead's browser check.** The page passes at 1280 px and at 375 px, with
  no horizontal scroll. The endpoint served all six keys, `X-Request-ID` appeared on a 200
  and a 404, and an inbound id was ignored. Two changes came out of it:
  - A `disabled` timer shows its state only, not "Last tick: none since the server started".
    A switched-off timer never ticks, so that row says nothing.
  - `duration_ms` uses `time.perf_counter_ns()`. On Windows `time.monotonic()` moves in
    steps of about 15.6 ms, so the development log printed only 0, 15, 16 and 31.
- **R12. Keys are redacted like values, and the value rules repeat until nothing changes
  (`tester-23`, D1 and D2).**
  - D1: `ValueRedactor` walked mapping values only, so a dict keyed by address, such as
    `balances={<address>: "0.5"}`, reached stdout whole. That is the likely shape of the
    hurried log line the address rule exists for. A string key now goes through the same
    value rules. Two keys that both redact to `[REDACTED]` collapse into one entry, which is
    accepted: the alternative keeps the address.
  - D2: one pass applies the rules in a fixed order, and a replacement can create the word
    boundary an earlier rule needed. A Kaspa address followed directly by a Base58 one left
    the second whole after one pass, and the root formatter makes only one pass over a
    standard-library record. The rules now repeat over a string until it stops changing.
    `[REDACTED]` matches no rule, so this ends, and a bound of a few passes guards it anyway.
    R17 qualifies this for a secret that is a piece of the marker.
    Both are pinned by tests.
- **R13. From the review (`reviewer-23`).**
  - **M1, must-fix. The URL rule was quadratic, and a client with no session controls its
    input.** `request_refused` logs the path of every `/api` request refused for want of a
    session, and `\b[A-Za-z][A-Za-z0-9+.\-]*://…` rescans the rest of a run from every word
    boundary. A 62 KB path of `a.a.a.…` cost 2.5 s for one log line, and a 200 KB path cost
    12 s, on the event loop. So the URL query is no longer found by a backtracking regex. The
    string is split into tokens on whitespace and quotes. In a token holding `://` and, after
    it, a `?`, everything from that `?` to the next `#` or the token's end becomes
    `?[REDACTED]`. That is one linear scan. Every other rule is checked for the same
    quadratic shape, and a test bounds the time of adversarial inputs of 200 KB per rule:
    `a.` repeated, `a://` repeated, `tb1` repeated, `kaspatest:` repeated, `xpub` repeated,
    and a long secret prefix repeated.
  - **S2. A URL glued to a word character** (`fetch_https://…?sign=…`) kept its query. The
    token scan above has no word boundary, which closes this too.
  - **S3. `logging`'s error fallback wrote the raw record to stderr.** When formatting
    raises, `Handler.handleError` prints the original message and its arguments, which
    bypasses every rule. The root handler is now a subclass whose `handleError` writes one
    fixed line to stderr: the exception's type and the logger's name, never the message or
    its arguments. The sentinel test (criterion 7) reads stderr as well as stdout.
  - **S4. `late` meant one interval in flight, not two.** The loop sleeps an interval *after*
    a tick finishes. The rule now has two cases:
    - while a tick is in flight (`last_tick_started_at` after `last_tick_finished_at`),
      `late` when `now - last_tick_started_at > 2 × interval`;
    - otherwise, `late` when `now - (last_tick_finished_at or started_at) > 2 × interval`.

    The spec's table and `docs/operations.md` say this.
  - **Overlapping secrets left a fragment.** Secrets that overlap in a string, such as
    `XXXXYYYYZZ` and `YYYYZZZZWW` in `XXXXYYYYZZZZWW`, rendered `[REDACTED]ZZWW`. Every
    secret's occurrences are now found as spans, overlapping spans are merged, and each merged
    span is replaced once.
  - **The failure heading.** When the request fails, `DetailSections` heads its alert with an
    `h3`, not an `h4`, so it is not read as a child of Backups.
  - **Accepted.** An address glued to a letter or a digit prints. The `ADDRESS_PATTERNS`
    docstring says so.

  Checked clean by the review:
  - 200 concurrent requests never swapped an id, and an inbound id was never echoed;
  - foreign records and uvicorn's are redacted, and so are `exc_info` and `stack_info`;
  - both `diff-cover` steps fail at 0% on changed lines;
  - the layering holds, and no float is on a money path;
  - the endpoint serves no configuration value and calls no provider;
  - the frontend's wording tables are total.
- **R14. An address joined by an underscore is redacted (`backend-dev-23`).** `\b` counts
  `_` as part of a word, so `wallet_<address>`, `snapshot_<address>.json` and
  `<address>_balance` printed whole. `f"wallet_{address}"` is exactly the hurried shape the
  rule is for. Every address rule now starts with `(?<![0-9A-Za-z])` instead of `\b`, and
  the Base58 rule also ends with `(?![0-9A-Za-z])`. The run rules of R16 and R18 end where
  their alphabet ends. A hyphen still separates, so R5's reasoning about
  UUIDs holds. The tests that pinned `\b` in the pattern text pin the new boundaries
  instead.
  - **Accepted:** an address glued to a letter or a digit, and two addresses glued together,
    where the first's pattern eats into the second. Neither can be separated without
    verifying checksums. The `ADDRESS_PATTERNS` docstring and `docs/operations.md` say so.
- **R15. What pins the repeat loop after R14 (`tester-23`).** Under R14 the D2 case, a Kaspa
  address glued straight onto a Base58 one, is the accepted "glued to a letter" residual,
  and it prints whole however many passes run. Every address rule now needs a non-alphanumeric
  character on both sides, so an address replacement can no longer create a match for an
  earlier rule. What still can: a loaded secret containing marker characters. With the
  secret `D]-tail0`, the text `<tb1 address>-tail0` becomes `[REDACTED]-tail0` after one
  pass, and only a second pass finds the secret. So:
  - the D2 pin is dropped;
  - the loop stays, pinned by the marker-secret case, so reducing it to one pass is not an
    equivalent mutant;
  - a brute-force property checks that one pass equals the fixpoint for address, key and URL
    tokens joined by every separator, `""` and `"_"` included, with no secret loaded. It
    flags any interaction this analysis missed.
- **R16. From the delta review (`reviewer-23`): no must-fix or should-fix.** It found every
  rule linear, the worst 200 KB shape at 57 ms against 12 to 27 s before. S2 and S3 are
  closed, the S4 boundaries hold, and the earlier clean checks still pass. Three nits:
  - **N1, fixed.** A chain of Kaspa addresses glued together lost one address per pass, so
    the fifth and later survived the four-pass bound. The Kaspa rule now matches a whole run
    of glued Kaspa addresses in one match.
  - **N2, documented.** These URL forms keep their query, and the docstring and §18 now
    list them:
    - a quote inside the query, which ends the token;
    - an unencoded `#` inside the query;
    - a `?` inside the fragment;
    - JSON-escaped slashes (`:\/\/`).

    None is a regression. `httpx` percent-encodes `'` and `#` in a query built from
    `params=`, and no response body is logged.
  - **N4, accepted and documented.** A tick is "in flight" when its start is after the last
    finish, by the wall clock. A clock stepped back between a finish and the next start makes
    a tick in flight be measured from the last finish again, which is the old one-interval
    threshold, until the clock catches up.
- **R17. The loop does matter for glued addresses (`tester-23`, D3).** R15 said an address
  replacement can no longer create a match for an earlier rule. That holds for every
  separator except none at all. With two addresses glued together, the first one's `[REDACTED]`
  gives the second the non-alphanumeric boundary it lacked, and only a second pass sees it.
  This happens after a Kaspa address, and after a bech32 one when the next starts with `b`.
  So the property is restated:
  - with every separator but `""`, one pass equals the fixpoint;
  - with `""`, the fixpoint is reached within the bound, and applying `redact_text` again
    changes nothing.

  The docstrings that describe glued pairs say exactly what was measured. So does the one
  claiming `[REDACTED]` matches no rule: it does when a loaded secret is a piece of the
  marker, and `MAX_REDACTION_PASSES` is what stops it there (I1, pinned). Four crafted
  secrets that are all fragments of the marker outlast the bound (I2). That needs a
  credential containing `[REDAC`, and it is not pinned.
- **R18. Glued bech32 addresses match as one run, like Kaspa (`backend-dev-23`).** `b` is
  outside the bech32 alphabet, so in a run of glued `bcrt1…` addresses each pass redacted
  only one more, and the fifth and later survived the bound. N1 had the same shape. The
  bech32 rule now matches a run, `(?:(?:bc|tb|bcrt)1<alphabet>{11,})+`, with the existing
  anchors. Both run rules nest a quantifier, so their timing is measured on adversarial
  runs whose end anchor fails, not only on runs that match. Examples are a glued run
  followed by a letter or digit, and runs broken by one character outside the alphabet. They
  must stay linear.
- **R19. The Kaspa run's third length rule is greedy and case-sensitive, and the
  glued-address heuristics stop here (`tester-23`, D4).** In R16's third alternative the
  lazy length `{61,63}?` stopped at 61 when the payload's 62nd character was itself a
  Base58 start (`2`). That left two payload characters glued in front of the next address,
  which then printed whole on every pass. The alternative is now greedy, and the Base58
  start in its lookahead is case-sensitive, `(?-i:[13mn2])`, as the Base58 rule itself is.
  Measured in memory over 5 Kaspa forms and 12 followers, the tester found no follower
  printed and no pair other than two markers. Runs still match as one.
  - **From here on, a new finding about addresses glued with no separator is documented,
    not fixed.** Without verifying checksums, every such rule is a heuristic, and each fix
    so far has moved the edge rather than removed it. R14 already accepts glued addresses
    as a residual. The time limits from M1 still bind any rule.
