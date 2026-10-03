# 030 — Observability: redaction by value, request ids, and the sources' health

Issue: #23
Status: in progress

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
     characters after the first, on word boundaries;
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
| `late` | running, and `now - (last_tick_finished_at or started_at) > 2 × interval` |
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

(none yet)
