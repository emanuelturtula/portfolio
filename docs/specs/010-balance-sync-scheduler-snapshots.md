# 010 — Balance sync service, scheduler and snapshot history

Issue: #10
Status: implementing

## Problem

Two chain providers exist and nothing calls them. `build_http_client()` has no owner, the
registry has no caller, and the only balance a user can see is the one they read on a block
explorer themselves. This change gives the providers a caller, a schedule, somewhere to put
what they return, and a record of every attempt.

It is also the first change in this milestone to add an endpoint, which means it is the
first one where the layering contracts have two real ends to check.

## Scope

- Three tables: `sync_runs`, `sync_run_chains`, `balance_snapshots`, and one migration.
- A sync service that reads every active wallet's balance, grouped by chain, with **each
  chain isolated from the others' failures**.
- A coordinator that guarantees one run at a time in this process.
- A scheduler task owned by the application lifespan, on a configurable interval.
- The shared `httpx.AsyncClient`, built and closed in the lifespan — the wiring #6 through
  #9 each deferred to this issue.
- Four endpoints: trigger a sync, read current balances (valued), read one wallet's
  history, read the sync runs.
- `ChainKey.asset_symbol`, because the read path needs a chain's symbol and must not import
  the package that currently holds one.
- **Scheduling #9's price refresh.** Added after implementation began; see below.

### The price refresh, added to scope after this spec was first committed

The first draft of this spec scoped #10 to balances and never mentioned prices, and
`backend-dev` was right to refuse to widen it on their own. It was already in scope and the
spec had simply lost it. `docs/providers.md` on merged `main` says so in as many words:

> **Building the price sources in the lifespan, and scheduling a refresh.** ... **#10 owns
> the scheduler**, and a scheduler invented in #9 would have been a second one to delete.

What made the omission expensive rather than tidy: `GET /api/balances/current` is the first
consumer of the price cache in the running application, and nothing fills that cache. A
fresh deployment would report every holding `unpriced` forever, until an operator ran
`portfolio refresh-prices` by hand -- which is #11's flagship endpoint answering
`"total": "0", "complete": false` on a correctly installed system.

`PORTFOLIO_PRICE_REFRESH_INTERVAL_MINUTES` defaults to **60**, matching `STALE_AFTER` in
`services/prices.py`, which `docs/providers.md` already describes as "one hour, matching the
refresh interval #10 will schedule". A failed price refresh does not stop the balance
scheduler, and vice versa. A price-refresh history table is **not** in scope: prices carry
`fetched_at` per row and #59 owns "why did it fail".

## Non-goals

- **The dashboard.** #11 owns every pixel. This change ships the JSON and the regenerated
  client types, nothing that renders them.
- **A partial multi-address read.** One address's refusal still aborts that chain's batch;
  that is #54, filed by #7, and changing it is a change to the provider contract rather
  than to its caller. What this change does own is that the *other* chain still succeeds.
- **A whole-read deadline.** #50. A run is bounded by each attempt's timeout and by the
  retry policy, not by a budget for the whole sync. Named here because a scheduler is what
  makes an unbounded read matter: the next tick arrives whether or not the last one ended,
  and the coordinator's answer to that is to skip, not to pile up.
- **Exchange balances, cost basis, profit and loss.** Milestone 3.
- **A sync health endpoint.** #23. `sync_runs` is the table it will read; this change fills
  it and exposes it, and stops there.
- **Tuning the provider constants.** Still module constants, still #10-or-later per
  `docs/providers.md`; nothing measured here argues for changing one.

## Design

### The per-address cache that #7 deferred here is the snapshot table

`docs/providers.md` records "a per-address cache — **#10 owns it**". The answer is that it
already exists and it is `balance_snapshots`: the previous reading, with the instant it was
taken, durable across restarts, and visible to an operator. A second in-memory cache in
front of it would be a copy of a table we are adding in this change, with a different
lifetime and no way to see it.

What the deferral was really protecting against is a manual refresh that hammers a public
API. That is answered by the coordinator below rather than by a cache: a second caller does
not start a second run, it joins the one in flight. A cache would have answered it by
returning a stale number that looks exactly like a fresh one, which is the failure
`ProviderUnavailableError` exists to prevent.

`docs/providers.md` gets edited to say so, rather than left claiming the work is outstanding.

### Failure isolation is per chain, and the error kind says whose fault it was

Wallets are grouped by `chain_key`. Each group is one coroutine; the groups run
concurrently under `asyncio.gather`. No coroutine raises — each returns a `ChainOutcome` —
so `return_exceptions=True` is not used, which matters because it would also absorb
`CancelledError` and turn a shutdown into a recorded chain failure.

Two catch clauses, deliberately not one:

| Caught | Recorded as | Why separate |
|---|---|---|
| `ProviderError` | the provider's own kind: `unavailable`, `rate_limited`, `response`, `unknown_chain` | the vendor failed, which is what #6's vocabulary is for |
| a rejected address (wrong network) | its own kind, **no traceback** | the owner's mistake. Filed as `internal` it logs a traceback every tick forever, which is this row's failure pointed the other way -- corrected after implementation found it |
| any other `Exception` | `internal`, logged with the traceback | our bug, and calling it "Kaspa is unavailable" is how a code defect gets read as a vendor outage for months |

Both keep the other chain's results. The alternative — catching only `ProviderError` and
letting a `TypeError` fail the run — was rejected because it loses good Bitcoin data to a
Kaspa parser bug, which is the exact outcome the issue's opening paragraph refuses.

### One run at a time, by joining rather than refusing

`SyncCoordinator` holds the in-flight `asyncio.Task` and an `asyncio.Lock` over the
check-and-set. A second caller — the scheduler tick, or a second click — does not start a
run and does not get a 409: it awaits the in-flight task under `asyncio.shield` and returns
that run's summary, with `joined: true`.

Rejected alternatives: a 409 makes the client poll for a result it could have been handed;
an unshielded await lets a disconnecting browser cancel a scheduled sync.

The lock covers only the check-and-set, not the run. There is no `await` between the check
and the assignment today, so the lock is strictly speaking redundant in a single-threaded
loop — it is there because that argument is invisible to whoever adds an `await` to the
line between them.

### Every run writes its row before it does any work

The `sync_runs` row is inserted at `status='running'` with `finished_at IS NULL`, and
updated when the run ends. Criterion 4 says *every* run writes a row; a row written only at
the end is not written by a run the process died in the middle of.

That makes `running` a real status, and an orphan a real state: a `running` row whose
process is gone. The lifespan sweeps them to `interrupted` at startup, before the scheduler
starts. This is one status and one sweep more than the issue asks for, and it is here
because the alternative is a table where a crashed run and a live run look identical.

**Every run sweeps too, before it opens its own row** -- added in review. A run whose
close-out commit fails (a locked database after the busy timeout) otherwise stays `running`
until the next process start, which on the Pi can be weeks. Sweeping at the start of a run is
safe for the same reason the startup sweep is: the coordinator allows one run at a time and
there is one process, so any `running` row that exists when a run begins is not live.

### Timing is measured twice, on purpose

`started_at` and `finished_at` are wall-clock (`UtcDateTime`) and answer *when*.
`duration_ms` is an `INTEGER` from `providers.http.monotonic_ms` and answers *how long*.
They are not redundant: the difference of two wall-clock reads is wrong by however much the
clock was stepped between them, and a Pi that syncs its clock mid-run would otherwise
record a negative duration. `float` is not available in `services/`, which is the same
reason every duration in `providers/` is an integer millisecond.

### Base units cross the wire as strings, and this is not the money rule

Rule 2 sends monetary values as JSON strings. Base units are `INTEGER`, so the rule does
not reach them — and they still have to be strings, for a different reason that was
measured rather than assumed:

```
KAS supply ~28.7e9 x 1e8 sompi  = 2.87e18
Number.MAX_SAFE_INTEGER         = 9.007e15
```

A Kaspa balance above roughly 90 million KAS does not survive `JSON.parse`. That is a
plausible address rather than a hypothetical one, and the failure is silent: the number
arrives rounded, renders fine, and is wrong in the last digits. So `confirmed` and
`pending` are strings over the wire, alongside the `Decimal` amount that is a string
because rule 2 says so. The reasons differ and both are written at the schema.

### `ChainKey.asset_symbol`, because the read path may not import the price package

Valuing a balance needs the asset symbol for a chain. Today `BTC` and `KAS` live in
`providers/prices/base.py`, and `services/balances.py` is imported by a router — so reaching
for them there would make `api.routers -> ... -> providers.prices` a real edge and fail
`prices-are-never-fetched-in-a-request`.

**That contract has never failed on a real chain.** It would fail here, correctly, on the
obvious implementation. The symbol is a property of the chain, not of the price package, so
it moves to `domain/chains.py`, which imports nothing. `providers/prices/base.py` keeps its
constants, and a test asserts the two agree — the same arrangement that holds
`_ASSET_KIND_CHECK` and its migration together.

### Balances are fetched in a request; prices never are

`POST /api/balances/sync` reaches a chain provider from a request path, and that is the
point of the endpoint. The asymmetry with #9's contract is deliberate: the user asked for
this read and is waiting for it, where nobody asks for a price refresh and every dashboard
render would trigger one. `thin-routers` already allows the indirect chain; no new contract
is added, and the spec says why rather than leaving a reader to infer it from a missing
line.

### Timestamps may be compared in SQL. Money still may not

History is filtered with `observed_at >= :since` and ordered by it. That is a comparison on
a `TEXT` column, which rule 2 forbids for money — and is safe here for the reason it is
unsafe there. `UtcDateTime` normalises every value to UTC and SQLAlchemy writes SQLite
datetimes at a fixed width, so lexicographic order is chronological order. `NumericText`
has a fixed *scale* and a variable number of integer digits, so `"9"` sorts after `"10"`.
The rule is about variable-width digits, not about `TEXT`, and a test pins the ordering
against values that would expose it.

The fixed width is a property of the *writer*, not of the column: a value written by hand
without microseconds sorts before the same instant written through `UtcDateTime`. Every
write in the application goes through the type, and a test pins that the fixtures use the
writer's format too, so the argument holds where it is used and is stated as conditional.

"Latest snapshot per wallet" is nonetheless resolved by `MAX(id)`, an `INTEGER`: snapshots
are append-only, so identity order is insertion order, and it needs no argument about
collation at all.

### Modules

| Path | What |
|---|---|
| `domain/chains.py` | `asset_symbol` on `ChainKey` |
| `db/models.py` | `SyncRun`, `SyncRunChain`, `BalanceSnapshot` |
| `db/migrations/versions/v0005_balances.py` | the three tables, reversible |
| `repositories/balances.py` | write snapshots, read current and history |
| `repositories/sync_runs.py` | open a run, finish it, sweep orphans, list runs |
| `services/balance_sync.py` | the sync itself; the only new module importing `providers` |
| `services/sync_coordinator.py` | one run at a time; joins rather than refuses |
| `services/scheduler.py` | the interval loop, start and stop |
| `services/balances.py` | read side: current, valued, history. Imports no provider |
| `api/schemas/balances.py` | request and response models |
| `api/routers/balances.py` | the four endpoints |
| `api/dependencies.py` | `get_balance_service`, `get_sync_coordinator` |
| `api/middleware.py` | untouched — every new path is authenticated by default |
| `main.py` | the client, the sweep, the scheduler, and closing all three |
| `config.py` | the new settings |

## API contract

Every path is under `/api` and none is added to `PUBLIC_API_PATHS`, so all four require a
session by rule 8. **Monetary and base-unit fields are JSON strings**, marked below.

### `POST /api/balances/sync`

Body: none. Response `200`:

```json
{
  "run_id": 41,
  "trigger": "manual",
  "joined": false,
  "status": "partial",
  "started_at": "2026-09-24T00:00:00Z",
  "finished_at": "2026-09-24T00:00:03Z",
  "duration_ms": 3128,
  "wallets_total": 5,
  "wallets_succeeded": 3,
  "wallets_failed": 2,
  "chains": [
    {"chain_key": "bitcoin", "status": "success", "wallets_read": 3, "error_kind": null, "detail": null},
    {"chain_key": "kaspa", "status": "failed", "wallets_read": 0, "error_kind": "unavailable", "detail": "No configured endpoint answered."}
  ]
}
```

`joined` is `true` when this request attached to a run already in flight instead of
starting one; `trigger` is then that run's trigger, not `"manual"`.

`detail` is the provider error's message, which those providers are already written never to
quote a body or an address into. No endpoint URL appears: `request_target` logs a label, and
this field carries the same discipline.

### `GET /api/balances/current`

Query: `quote_currency` (`EUR` or `USD`, default `EUR`; anything else is a 422). The first
draft showed the field in the response and never said where it came from, and the first
implementation accepted any string -- `?quote_currency=gbp` answered 200 with every holding
`never_fetched`, which tells an operator the refresh has not run when the cause is a currency
this application does not price.

Response `200`:

```json
{
  "quote_currency": "EUR",
  "total": "1234.56",
  "complete": false,
  "as_of": "2026-09-24T00:00:03Z",
  "wallets": [
    {
      "wallet_id": 7,
      "chain_key": "bitcoin",
      "label": "cold",
      "asset_symbol": "BTC",
      "confirmed": "123456789",
      "pending": null,
      "decimals": 8,
      "quantity": "1.23456789",
      "value": "1234.56",
      "price": {"amount": "1000.00", "source": "kraken", "as_of": "...", "stale": false},
      "observed_at": "2026-09-24T00:00:03Z"
    }
  ],
  "unpriced": [{"asset_symbol": "KAS", "quantity": "10.00000000", "reason": "never_fetched"}]
}
```

`total`, `value`, `quantity`, `price.amount`, `confirmed` and `pending` are **strings**.
`complete` is false whenever any holding could not be priced **or any active wallet has
never been read**, and `total` is then the sum of what could be computed. The unread wallets
are listed in `unread: [{wallet_id, chain_key, asset_symbol}]`, beside `unpriced`.

The first draft defined `complete` by pricing alone, and review reproduced the cost: a BTC
wallet read and a KAS wallet never read answered `complete: true` with a total that left
Kaspa out. That is #9's own failure -- a number that silently omits a holding -- arriving
through a missing *reading* rather than a missing price, and the draft had written it into
the contract.

A wallet with no snapshot yet appears with `confirmed: null` and `observed_at: null` rather
than a zero. A zero balance and an unread wallet are different facts and the dashboard is
allowed to say which. A wallet with an *old* snapshot is not unread: its `observed_at` says
how old, and whether that is too old is #11's decision to render.

### `GET /api/wallets/{wallet_id}/balances`

Query: `since` (ISO-8601, optional), `cursor` (opaque, optional), `limit` (1..1000,
default 500). `since` and `cursor` together is a 422, and so is a malformed cursor.

| Parameters | Rows |
|---|---|
| neither | the latest `limit` readings |
| `since` | the first `limit` readings at or after `since` |
| `cursor` | the next `limit` readings strictly after the `(observed_at, id)` it encodes |

All three are ordered `(observed_at, id)`, oldest first. The response carries
`next_cursor`, non-null exactly when there may be more rows forward.

**Keyset pagination was added in review**, because the draft's "forward cursor from `since`"
did not advance: `since` is inclusive and has no tie-break, so re-asking from the last
`observed_at` with `limit=1` returned the same row forever. It matters because paging is the
only way to chart more than one page -- at one reading every fifteen minutes, 1000 rows is
about ten days. The cursor encodes a timestamp and an integer and nothing else.

The latest-window default was itself a correction: the draft's literal reading returned a
year-old wallet's *oldest* 500 readings, which is the wrong end for the only consumer there
is, #11's chart.

Response `200`: `{"wallet_id": 7, "decimals": 8, "snapshots": [{"observed_at": ..., "confirmed": "...", "pending": null, "quantity": "...", "sync_run_id": 41}], "next_cursor": null}`, oldest first.

`404` when the wallet is not the caller's, via the existing `WalletNotFoundError` — an
archived wallet still answers, because its history is the reason archiving is a timestamp.

### `GET /api/balances/runs`

Query: `limit` (1..200, default 50). Response `200`: `{"runs": [<the sync response shape>]}`,
newest first. This is what makes criterion 4 observable without opening the database.

## Data model

Migration `v0005_balances`, **reversible**: `downgrade` drops the three tables in dependency
order. No existing table is altered, so there is nothing to back-fill and nothing a
downgrade would lose beyond what it created.

```sql
CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL,            -- CHECK: 'scheduled' | 'manual' | 'startup'
    status TEXT NOT NULL,             -- CHECK: 'running' | 'success' | 'partial' | 'failed' | 'interrupted'
    started_at DATETIME NOT NULL,
    finished_at DATETIME,             -- NULL while running and for an interrupted run
    duration_ms INTEGER,              -- monotonic, NULL until finished
    wallets_total INTEGER NOT NULL,
    wallets_succeeded INTEGER NOT NULL DEFAULT 0,
    wallets_failed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_sync_runs_started_at ON sync_runs (started_at);

CREATE TABLE sync_run_chains (
    id INTEGER PRIMARY KEY,
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    chain_key TEXT NOT NULL,          -- CHECK: the ChainKey members
    status TEXT NOT NULL,             -- CHECK: 'success' | 'failed'
    wallets_read INTEGER NOT NULL,
    error_kind TEXT,                  -- CHECK: NULL | 'unavailable' | 'rate_limited' | 'response' | 'unknown_chain' | 'address_rejected' | 'internal'
    detail TEXT,
    UNIQUE (sync_run_id, chain_key)
);

CREATE TABLE balance_snapshots (
    id INTEGER PRIMARY KEY,
    wallet_id INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    confirmed INTEGER NOT NULL,       -- base units
    pending INTEGER,                  -- signed; NULL means the chain cannot answer
    decimals INTEGER NOT NULL,
    observed_at DATETIME NOT NULL,
    UNIQUE (wallet_id, sync_run_id)
);
CREATE INDEX ix_balance_snapshots_wallet_observed ON balance_snapshots (wallet_id, observed_at);
```

`decimals` is stored on the snapshot rather than read from `assets` at query time, so that
changing an asset row cannot reinterpret history that was already recorded.

`pending` keeps #7's tri-state exactly: `NULL` is "this chain does not answer the question",
and a signed value is a net mempool delta that is legitimately negative. The column has no
non-negative CHECK for that reason, where `confirmed` does.

Every CHECK constraint is named and its text is duplicated into the migration, carrying the
same hazard `_ASSET_KIND_CHECK` documents and covered the same way: a reflection test
compares the constraint off a migrated database against the model's constant.

## Configuration

| Setting | Default | Why |
|---|---|---|
| `PORTFOLIO_BALANCE_SYNC_ENABLED` | `true` | an operator debugging a vendor needs an off switch that is not a code edit |
| `PORTFOLIO_BALANCE_SYNC_INTERVAL_MINUTES` | `15` | the issue's default |
| `PORTFOLIO_BALANCE_SYNC_SHUTDOWN_GRACE_SECONDS` | `10` | how long shutdown waits for a run in flight before recording it interrupted |
| `PORTFOLIO_PRICE_REFRESH_ENABLED` | `true` | the price timer's own switch, so the two can be turned off and tested apart |
| `PORTFOLIO_PRICE_REFRESH_INTERVAL_MINUTES` | `60` | paired with `STALE_AFTER`; changing one alone marks every price stale most of the time |

**`PORTFOLIO_BALANCE_SYNC_ENABLED=false` switches off the loop and nothing else.**
`POST /api/balances/sync` still works: the manual trigger is what an operator debugging a
vendor reaches for, and one switch taking both away would defeat its purpose.

The interval is validated `>= 1` in `_refuse_unsafe_configuration`: zero or a negative value
is a loop with no sleep against a public API that documents a ban as the consequence.

**The first run happens at startup only if the last *attempt* is older than one interval,
and otherwise the first sleep is only the time remaining.** Sleeping first leaves a fresh
deployment blank for fifteen minutes; running unconditionally lets a crash-looping container
hit two public APIs on every restart.

Review corrected both halves of the draft:

- **Attempts, not successes.** The draft counted only *finished* runs. A container that dies
  faster than one sync takes -- thirty BTC wallets are at least thirty seconds at one request
  a second -- leaves `interrupted` rows with no `finished_at`, so every restart synced again.
  The balance timer now reads the `started_at` of the latest run of any status, which makes
  the property "at most one sync per interval across restarts".
- **The remaining time, not a whole interval.** A deploy fifty minutes after an hourly price
  refresh slept another full hour, so every price read stale for fifty minutes after every
  deploy. The first sleep is now `interval - elapsed`, in whole seconds, rounded up.

Two residuals, accepted and documented rather than engineered away:

- **Prices have no attempt record** (a price-refresh history is out of scope). When every
  source fails, a crash loop costs one price request per restart; any success writes rows and
  suppresses the next one.
- **Prices read stale for a few seconds each hour.** The interval equals `STALE_AFTER` and
  `as_of` is stamped when a refresh begins, so between the hour and the new rows' commit the
  old ones are over an hour old. Bounded by how long a refresh takes, and it errs toward
  "stale" -- the direction #9's `as_of` argument already chose, because it is the only one
  that cannot make an old price look fresh.

## Acceptance criteria

Verbatim from #10, numbered:

1. A scheduled job runs on a configurable interval (default 15 minutes) and persists
   snapshots
2. `POST /api/balances/sync` triggers a manual refresh and returns a run summary
3. One provider failing is recorded while the other still succeeds, proven by a test
4. Every run writes a `sync_runs` row with status, counts and timing
5. The scheduler starts and stops cleanly with the application lifespan
6. A concurrent manual and scheduled run do not duplicate work
7. `GET /api/balances/current` and `GET /api/wallets/{id}/balances` expose current and
   historical values
8. Snapshot history is queryable for charting

Two readings the issue leaves open, resolved here rather than silently:

- **Criterion 6, "do not duplicate work"** is read as *join*, not *refuse*: the second
  caller gets the in-flight run's summary with `joined: true`. A 409 also satisfies the
  words and makes every client poll.
- **Criterion 8, "queryable for charting"** is read as the wallet history endpoint with
  `since` and `limit`, oldest first. No aggregation, no bucketing and no portfolio-wide time
  series: those need a decision about what a chart shows when one wallet has a gap, and #11
  is where that decision has a consumer.

## Test plan

| # | Criterion | Test |
|---|---|---|
| 1 | interval, default, persistence | `tests/services/test_scheduler.py::test_the_loop_runs_once_per_interval_and_persists_snapshots`, `::test_the_interval_comes_from_settings`, `tests/test_config.py::test_a_zero_interval_is_refused_at_construction` |
| 1 | first run condition | `tests/services/test_scheduler.py::test_a_fresh_database_syncs_at_startup`, `::test_a_recent_run_is_not_repeated_on_restart` |
| 2 | manual endpoint | `tests/api/test_balances.py::test_a_manual_sync_returns_the_run_summary`, `::test_the_sync_endpoint_requires_a_session` |
| 3 | **isolation** | `tests/services/test_balance_sync.py::test_one_chain_failing_leaves_the_other_chains_snapshots_written`, `::test_a_provider_error_is_recorded_with_its_kind`, `::test_an_internal_error_is_recorded_as_internal_and_not_as_a_vendor_outage` |
| 4 | the run row | `tests/services/test_balance_sync.py::test_a_run_row_exists_before_any_provider_is_called`, `::test_counts_and_timing_are_written_when_the_run_ends`, `::test_duration_comes_from_the_monotonic_clock_not_the_wall_clock` |
| 4 | orphans | `tests/db/test_sync_runs_repository.py::test_a_running_row_from_a_dead_process_is_swept_to_interrupted`, `tests/db/test_lifespan.py::test_a_running_row_from_a_dead_process_is_swept_at_startup` (the scheduler never touches the table, so the first draft's placement in `test_scheduler.py` named a module that cannot own it) |
| 5 | lifespan | `tests/db/test_lifespan.py::test_the_scheduler_starts_and_stops_with_the_application`, `::test_the_http_client_is_closed_on_shutdown`, `::test_shutdown_waits_for_a_run_in_flight`, `::test_a_run_that_outlasts_the_grace_is_recorded_as_interrupted` (one run cannot both finish within the grace and be recorded interrupted, so the draft's single name was two tests), `::test_the_scheduler_is_not_started_when_disabled` |
| 6 | one run at a time | `tests/services/test_sync_coordinator.py::test_a_second_caller_joins_rather_than_starting_a_second_run`, `::test_the_joined_caller_sees_the_running_trigger_not_its_own`, `::test_a_cancelled_request_does_not_cancel_the_run` |
| 7 | current | `tests/api/test_balances.py::test_current_balances_are_valued_against_the_price_cache`, `::test_an_unpriced_asset_makes_the_total_incomplete`, `::test_a_wallet_with_no_snapshot_reports_null_rather_than_zero` |
| 7 | history | `tests/api/test_balances.py::test_wallet_history_is_oldest_first`, `::test_another_users_wallet_is_a_404`, `::test_an_archived_wallet_still_answers_with_its_history` |
| 8 | charting query | `tests/api/test_balances.py::test_since_filters_the_history`, `::test_limit_is_bounded`, `tests/db/test_balances_repository.py::test_ordering_is_chronological_across_a_digit_boundary` |
| — | wire format | `tests/api/test_balances.py::test_base_units_cross_the_wire_as_strings`, `::test_a_kaspa_balance_past_the_javascript_safe_integer_survives_the_round_trip` |
| — | layering | `tests/test_import_contracts.py::test_the_shipped_contract_reports_a_router_reaching_prices_through_a_service (already present; asserted still red on a planted edge and green on the real tree)` |
| — | price refresh | `tests/db/test_lifespan.py::test_the_price_refresh_is_scheduled_and_actually_fills_the_cache` (rows in `prices`, not a scheduler object), `::test_a_failing_price_refresh_does_not_stop_the_balance_sync` |
| — | no network | `tests/test_no_network.py` (DNS and outbound connections blocked around a real lifespan, with a control proving the block fires) |
| — | symbol duplication | `tests/domain/test_chains.py::test_every_chain_symbol_matches_the_price_packages_constant` |
| — | schema drift | `tests/db/test_migrations.py::test_the_new_check_constraints_match_the_models` |
| — | auth by default | `tests/auth/test_route_contract.py` walks every registered route and covers all four new paths with no edit |

Failure cases carried explicitly: both providers failing (`status='failed'`, no snapshots,
still one run row), a wallet whose chain has no registered provider (`unknown_chain`,
isolated), a sync with no active wallets at all (a `success` run with zero counts rather
than a crash), and a snapshot write failing after a successful read.

## File ownership

Disjoint. Nobody else edits a file on another row.

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/**`, `openapi.json`, `frontend/src/api/generated/schema.ts`, `docs/providers.md`, `docs/operations.md` |
| tester | `backend/tests/**` |
| tech-lead | `docs/specs/010-*.md`, `backend/.importlinter`, `backend/pyproject.toml` |

`frontend/src/api/generated/schema.ts` is generated, not written: `dump_openapi.py` then
`npm run gen:api`. The OpenAPI drift job fails the build if it is not regenerated, so it is
listed as owned rather than left to be discovered in CI.

## What the plan got wrong

Filled in at the end, before the pull request opens.
