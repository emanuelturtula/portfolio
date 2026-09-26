# 015 — Resumable windowed exchange sync with checkpoints and idempotent writes

Issue: #15
Status: implementing

## Problem

#12 defined the exchange seam and #13 put Bitget behind it, but nothing calls a provider:
no fill has ever reached `exchange_fills`, and M4 has nothing to compute a cost basis from.
The engine that drives a provider has to survive the Raspberry Pi losing power in the middle
of a sync, re-read overlapping history without counting a fill twice, and tell a key the owner
must fix apart from a venue that is only busy.

## Scope

- **Sync state and checkpoints**, migration `0007_exchange_sync`:
  - sync state on `exchange_accounts`;
  - a queue of pending windows, each carrying its cursor;
  - a run log of its own, with one outcome per account per run;
  - triggers that make `exchange_fills` append-only.
- **`services/exchange_sync.py`**, the write side and the only module in `services/` that
  imports an exchange provider. It:
  - clamps the start to retention;
  - splits the range into windows, newest first, with a five-minute overlap;
  - pages with the venue's cursor, committing the cursor in the same transaction as the fills;
  - retries a rate limit;
  - marks `auth_failed` on an auth error and stops;
  - isolates one account's failure from the others.
- **`INSERT ... ON CONFLICT DO NOTHING`** on the existing `uq_exchange_fills_account_trade`, with
  `seen` and `inserted` counts. A conflicting row that **differs** is a collision, and it raises
  (spec 014's hand-on).
- **A scheduler and a manual trigger** through a coordinator that joins a run in flight, the
  pattern #10 built for balances.
- **Endpoints:** `GET /api/exchanges`, `POST /api/exchanges/sync`, `GET /api/exchanges/runs`.
  None of them returns a credential; each reports only `configured: bool` and a status.
- **Four settings:** a history start, an on/off switch, an interval and a shutdown grace.
- **Documentation:**
  - a new `docs/operations.md` section 13;
  - the `docs/providers.md` "Not done yet" entries this closes;
  - the regenerated `frontend/src/api/generated/schema.ts`.

### Reading of "Depends on: both exchange providers"

The sync is written against #12's protocol and tested with the fake provider. Bitget is the
one venue wired in the registry today. BingX (#14) plugs in with a line in `registry.py` and
nothing here changes, except as noted under Risks for a venue that `requires_symbol`. Building
it now means Bitget fills land before BingX exists, which is the order the owner chose.

## Non-goals

| Not here | Where |
|---|---|
| The Exchanges page, status badges, the truncation banner | #16 |
| BingX, and deciding its symbol discovery | #14 |
| Bitget UTA (v3) | #76 |
| **Storing a corrected fill as "a new row plus an adjustment"** | M4. See the interpretation of criterion 2 below: this issue guarantees the "never a mutation" half, and refuses a correction loudly rather than inventing an adjustment model the accounting engine has not designed |
| A credential health-check endpoint | not planned; a sync is the check |
| Per-account manual sync, deleting an account, forcing a full re-read | not planned |
| Cost basis, balances held on an exchange | M4 |

## Design

### The shape of a run

```
sweep interrupted exchange runs -> open run (committed)
-> find the owner -> ensure one exchange_accounts row per configured venue (committed)
-> for each account, sorted by exchange_key, sequentially:
     skip if auth_failed and the trigger is not manual
     clamp -> normalise pending windows -> plan new windows (committed)
     for each pending window, newest first:
         page loop: fetch -> insert fills + advance or delete the window (one commit per page)
     status ok + last_synced_at (committed)
-> close run (committed)
```

Accounts run **sequentially over one session**. There are at most two venues, an
`AsyncSession` is not safe for concurrent use, and a sequential loop needs none of the
gather/write split that `balance_sync.py` needed. Each account is wrapped so that its failure
is recorded as its outcome and the next account still runs.

### The owner

Exchange accounts belong to a user, and this application has one. The sync reads
`UserRepository.list_all()`:

- **No user:** the run finishes `success` with zero accounts and logs a warning. Nothing can
  be attached to anyone.
- **More than one user:** the run finishes `failed` with zero accounts and logs an error. No
  venue is called, because attaching fills to a guessed owner is worse than not importing
  them. `create-user` refuses a second account (#69), so this needs a hand-edited database.

For each configured venue (a key of the registry mapping), the account row is created if it
does not exist. A venue with an account row but no credentials any more is not synced. It
still appears in `GET /api/exchanges` with `configured: false`.

### Planning: clamp, newest first, overlap

Pure functions in `services/exchange_sync_plan.py`, with no I/O and no clock, so the
arithmetic is testable to the millisecond:

| Name | Value / contract |
|---|---|
| `HISTORY_GENESIS` | `2009-01-03T00:00:00Z`: what "all of it" means when no history start is configured |
| `OVERLAP` | `timedelta(minutes=5)`: how far each new top window reaches back into planned history |
| `RETENTION_STEP` | `timedelta(days=1)`: how far `since` moves after the venue refuses a window as too old. **A guess** |
| `MAX_RETENTION_STEPS` | `3`, per window per run |
| `RATE_LIMIT_RETRIES` | `3` retries per page (4 attempts) |
| `MAX_RATE_LIMIT_WAIT_SECONDS` | `60` |
| `split_newest_first(since, until, *, max_window)` | `FillWindow`s end to end, each at most `max_window`, **newest first**; the oldest may be shorter; `()` when `since >= until` |
| `plan_account(*, effective_since, planned_until, clamp, now)` | an `AccountPlan(windows, effective_since, planned_until)`: see below |
| `normalise_pending(windows, *, floor, max_window)` | re-clamps pending windows to the current retention floor, drops any that fall wholly below it, re-splits any longer than `max_window`, and reports whether anything was dropped or shrunk |
| `split_in_half(window)` | `(newer, older)`, the midpoint floored to a whole millisecond; `ValueError` below 2 ms |
| `seconds_to_wait(retry_after_ms, *, attempt)` | whole seconds, rounded **up**. The venue's `retry_after_ms` if given, otherwise `2 ** attempt`. Returns `None` when the wait exceeds `MAX_RATE_LIMIT_WAIT_SECONDS`, meaning "do not wait, stop this account for this run" |

`now` is floored with `floor_to_millisecond`, and every bound is built with it (spec 012's
handed-on rule). `plan_account`:

- **First sync** (`planned_until is None`): plans `[clamp.effective_since, now)`. That sets
  `effective_since = clamp.effective_since` and `planned_until = now`.
- **Later syncs:**
  - The **top** is `[max(planned_until - OVERLAP, clamp.effective_since), now)`, when
    `now > planned_until`. A clock stepped backwards plans nothing.
  - The **bottom** is `[clamp.effective_since, effective_since)`, when the clamp reaches earlier
    than the recorded floor. That only happens when the owner moves the history start earlier
    and retention still allows it.
  - The new values are `effective_since = min(...)` and `planned_until = max(...)`.
- **Order:** the windows come back newest first, the top's before the bottom's.
- **No floor movement from retention:** `effective_since` never moves forward in this function.
  History already held stays held when the rolling retention passes it.

The requested start is the `PORTFOLIO_EXCHANGE_HISTORY_START` date at 00:00 UTC, or
`HISTORY_GENESIS`. It is passed to `clamp_to_retention` as `min(requested, now)`: a clock
stepped back behind a configured date must not raise. **The configured value is what gets
recorded.**

The plan is **persisted before any fetch**: new window rows, and the account's
`requested_since`, `effective_since` and `planned_until`, in one commit. A crash after
planning resumes the plan rather than re-planning over it.

**Contiguity holds by construction.** Every planned range touches the existing
`[effective_since, planned_until)`: the top overlaps it, the bottom abuts it. So the union of
done and pending windows is always one interval, and the done part is complete once the
pending queue is empty.

### The pending-window queue is the checkpoint

`exchange_sync_windows` holds **only work not yet finished**. One row is one window, plus a
symbol for a venue that `requires_symbol`, plus the cursor of the next page to request (`NULL`
means the window's first page).

Each page is one transaction:

1. insert the fills;
2. set the window's `cursor` to the page's `next_cursor`, **or delete the window row** when
   `next_cursor` is `None`;
3. commit.

Criterion 1 follows from that. A crash before the commit loses the page and nothing else, and
the window is re-read from the last committed cursor. A crash after it has already advanced
the cursor. The re-read page's fills are deduplicated by the constraint.

Pending windows are processed **newest first across the whole queue**: new top windows and
windows left over by an interrupted earlier run, sorted in Python by `(until, since, id)`
descending. Datetimes are never ordered in SQL; see the `sync_runs` docstring.

**Before fetching, the queue is normalised** against the current clamp and capabilities:

- a window wholly older than the retention floor is dropped, since its history has aged out
  while the account was stalled;
- a window partly older has its `since` moved up;
- a window longer than `max_query_window` is re-split (a code change can shrink it).

If anything was dropped or shrunk, the account's `effective_since` rises to the clamp's
`effective_since`. The history held really does start there now.

**Cursor kinds.** `TRADE_ID_BEFORE`, `TRADE_ID_AFTER` and `TIME` are one case to the sync: pass
the cursor back and follow `next_cursor` to `None`. `NONE` differs. A page holding
`page_size` fills means the window was truncated. It is **not** inserted; the window row is
replaced by the two halves from `split_in_half`, committed, and the newer half is read first.
A window under 2 ms that is still full is an `ExchangeSchemaError`.

**Cycle detection** (spec 012's hand-on): within one window in one run, the sync keeps the set
of cursors it has sent. A `next_cursor` already in the set raises `ExchangeSchemaError` ("the
venue's cursor returned to one already visited"), catching A -> B -> A.
`require_cursor_advanced` already catches A -> A.

**Retention refusal** (`ExchangeRetentionWindowError`, "#15 clamps further"):

- The sync moves the window's `since` forward by `RETENTION_STEP` and drops every pending
  window lying wholly below the new `since`.
- It raises the account's `effective_since` to the new `since` and commits.
- It retries at most `MAX_RETENTION_STEPS` times per window per run. A window left empty is
  deleted and the sync moves on.
- When the steps run out, the account fails with `retention_window`. The moved `since` is
  persisted, so the next run continues from there.

**Requires symbol:** `candidate_symbols()` is called once per account per run, at planning
time. Each planned window becomes one row per symbol. No candidates means no rows. The call
gets the same rate-limit retry as a page, and the error table below applies to it.

### Idempotent, append-only writes

`ExchangeFillRepository.insert_page(account_id, fills, *, ingested_at) -> FillInsertResult(seen,
inserted)`:

- The insert is `sqlite.insert(ExchangeFill).values(rows).on_conflict_do_nothing(index_elements=
  [exchange_account_id, external_trade_id]).returning(external_trade_id)`.
- **Idempotency is the constraint**, not a pre-read.
- `seen` is `len(fills)`, and `inserted` is the number of returned ids.

**Collision check.** For every id the constraint skipped, the stored row is read and compared
on the accounting fields:

`external_order_id`, `symbol`, `base_asset`, `quote_asset`, `side`, `quantity`, `price`,
`quote_quantity`, `quote_quantity_derived`, `fee_amount`, `fee_asset`, `executed_at`.

- The amounts are compared as `Decimal`s, so the column's trailing zeros are not a difference.
- **`raw_payload` is not compared.** A venue adding a field to its response would otherwise make
  every overlap re-read a false collision and stall the account. Spec 014's hand-on said
  "raw_payload"; this narrows it, deliberately.
- Any difference raises `FillConflictError`. The caller rolls the page back, so the cursor does
  not advance, and the account fails with `conflict`.
- The message states a count and never an id.

**Append-only is a database property.** The migration creates two triggers:

- `exchange_fills_no_update`, `BEFORE UPDATE ON exchange_fills`;
- `exchange_fills_no_delete`, `BEFORE DELETE ON exchange_fills`.

Each does `SELECT RAISE(ABORT, 'exchange_fills is append-only')`.

The SQL of each is a module constant in the migration. A reflection test compares
`sqlite_master.sql` on a migrated database against it. **Alembic batch mode does not recreate
triggers**, so a future migration that rebuilds `exchange_fills` must recreate them. The
reflection test is what fails if it does not. The model docstring says so too.

### Error handling, per account

| Raised | Account status | Outcome `error_kind` | Behaviour |
|---|---|---|---|
| `ExchangeInsufficientScopeError` | `auth_failed` | `insufficient_scope` | stop, no retry |
| `ExchangeAuthError` | `auth_failed` | `auth` | stop, no retry |
| `ExchangeRateLimitedError` | (unchanged while retrying) | `rate_limited` if exhausted | wait `seconds_to_wait`, retry the same page; stop when exhausted or the wait is over the cap. Status `error` then |
| `ExchangeRetentionWindowError` | `error` if the steps run out | `retention_window` | step `since`, as above |
| `ExchangeUnavailableError` | `error` | `unavailable` | stop; the transport already retried |
| `ExchangeInvalidRequestError` | `error` | `invalid_request` | stop |
| `ExchangeSchemaError` (a cursor cycle included) | `error` | `schema` | stop |
| `FillConflictError` | `error` | `conflict` | stop, page rolled back |
| anything else | `error` | `internal` | stop, traceback logged, `detail` is the type name only |

- **Checkpoints survive every row.** A failure rolls back only the page in flight. What was
  committed before it stays, and the next run resumes from it.
- **Order matters.** Subclasses are matched before their parents, the way
  `_PROVIDER_ERROR_KINDS` does it: scope before auth, retention before invalid request,
  rate-limited before unavailable.
- **`detail`:**
  - For an `ExchangeError` it is `str(error)`, safe by construction: a class summary, a status
    and a digits-only venue code.
  - For `FillConflictError` it is its fixed, count-only message.
  - For anything else it is the type name.

**`auth_failed` is terminal until the owner acts** (criterion 4). A scheduled or startup run
**skips** an `auth_failed` account without calling the venue, and records a `skipped` outcome.
**A manual sync retries it.** The owner fixes the key, restarts the container (credentials are
read at startup) and presses sync. `docs/operations.md` section 13 says exactly that.
Retrying on a timer would ask a venue to refuse the same key every fifteen minutes, which some
venues answer with an IP ban.

**Success.** An account whose pending queue is empty at the end of the run gets
`sync_status = ok` and `last_synced_at = clock()`.

### Run log, coordinator, scheduler

- **`exchange_sync_runs` mirrors `sync_runs`:**
  - `SyncTrigger` and `SyncRunStatus` are reused, and their `CHECK` texts are the same model
    constants.
  - The row is written at `running` and committed before any venue is called.
  - The lifespan sweeps `running` rows to `interrupted` at startup and shutdown, and so does the
    service before it opens a run.
  - `finished_at`/`duration_ms` stay `NULL` for an interrupted run.
  - The duration comes from `monotonic_ms`.
- **Run status:**
  - it is computed over the **attempted** accounts, so `skipped` is not attempted;
  - none attempted gives `success`;
  - all attempted failed gives `failed`;
  - some failed gives `partial`;
  - otherwise it is `success`.
- **`SyncCoordinator` becomes generic** over the summary type (PEP 695), with the task name and
  the log-event prefix as constructor arguments:
  - **the balance coordinator's task name and log events do not change;**
  - the exchange coordinator uses `exchange-sync` / `exchange_sync`.
- **The runner closure lives in `main.py`**, like `balance_sync_runner`. It holds one session
  per run. The provider mapping comes from `exchange_providers(client, settings=settings)`,
  built **once** in the lifespan, as `price_sources` is.
- **What the lifespan publishes on `app.state`:**
  - `exchange_sync_coordinator`, always, so that a manual sync works with the timer off;
  - `configured_exchanges`, a `frozenset[ExchangeKey]`. That is the only thing the read side
    learns about credentials.
- **The `exchange-sync` `IntervalScheduler` is built only when:**
  - `PORTFOLIO_EXCHANGE_SYNC_ENABLED` is true, **and**
  - at least one venue is configured.

  An unconfigured install writes no empty runs every fifteen minutes. `last_run_at` is the
  newest exchange run's `started_at`, counting attempts, for the crash-loop reason
  `latest_sync_attempt` gives.
- **Shutdown** stops all three timers, drains both coordinators, and sweeps both run tables.
  Each sweep is in its own `try`.

### Read side, and what can never be served

`services/exchanges.py` (`ExchangeService`, read-only) builds the account list and the run log
from repositories.

- **It imports no provider.** A new import-linter contract makes `portfolio.api` unable to
  import `portfolio.providers.exchanges`, directly or indirectly:
  - no request path can reach the module that holds `Credentials`, except through the
    coordinator `main.py` wired;
  - a planted-violation test proves the contract is not vacuous, the treatment the prices
    contract has.
- **`configured` is `key in configured_exchanges`.** That is the complete disclosure about
  credentials. No response model has a field whose name contains `key` (other than
  `exchange_key`), `secret`, `passphrase`, `credential`, `token` or `signature`. A test walks
  the OpenAPI document to prove it.

### Files

| Path | Change |
|---|---|
| `backend/src/portfolio/db/migrations/versions/v0007_exchange_sync.py` | new |
| `backend/src/portfolio/db/models.py` | new columns, three tables, check constants |
| `backend/src/portfolio/domain/exchanges.py` | `AccountSyncStatus` |
| `backend/src/portfolio/repositories/exchanges.py` | new: accounts, pending windows, fills |
| `backend/src/portfolio/repositories/exchange_sync_runs.py` | new: run log and its vocabulary |
| `backend/src/portfolio/services/exchange_sync_plan.py` | new, pure |
| `backend/src/portfolio/services/exchange_sync.py` | new, the write side |
| `backend/src/portfolio/services/exchanges.py` | new, the read side |
| `backend/src/portfolio/services/sync_coordinator.py` | generic over the summary |
| `backend/src/portfolio/api/routers/exchanges.py`, `api/schemas/exchanges.py` | new |
| `backend/src/portfolio/api/dependencies.py`, `main.py`, `config.py` | wiring, settings |
| `backend/.importlinter` | the new contract |
| `frontend/src/api/generated/schema.ts` | regenerated, never hand-edited |
| `docs/operations.md`, `docs/providers.md` | section 13; "Not done yet" |

## API contract

All three paths require a session (deny-by-default; nothing is added to `PUBLIC_API_PATHS`).
Datetimes are ISO 8601 with an offset. No monetary field crosses this API.

### `GET /api/exchanges` → 200 `ExchangeListResponse`

```json
{"exchanges": [{
  "exchange_key": "bitget",
  "configured": true,
  "status": "ok",
  "syncing": false,
  "requested_since": "2009-01-03T00:00:00Z",
  "effective_since": "2026-06-27T12:05:00Z",
  "history_truncated": true,
  "last_synced_at": "2026-09-25T12:00:04Z",
  "fills_stored": 412,
  "pending_windows": 0,
  "last_error": null
}]}
```

- **Which venues are listed:** every configured venue, plus every venue with an account row for
  the owner, sorted by `exchange_key`. An empty list is #16's empty state.
- **`status`** is one of `never_synced`, `ok`, `error`, `auth_failed`, the stored
  `AccountSyncStatus`. A configured venue with no account row yet is `never_synced`.
- **`syncing`** is the exchange coordinator's `in_flight` and `configured`.
- **`history_truncated`** is `effective_since > requested_since`, and `false` when either is
  null. #16's banner names `effective_since`.
- **`last_error`** is `{"error_kind": ..., "detail": ...}` taken from the account's latest
  **non-skipped** run outcome, when that outcome failed. It is `null` otherwise.

### `POST /api/exchanges/sync` → 200 `ExchangeSyncTriggeredResponse`

Runs a manual sync of every configured venue, or joins the one in flight. The body is
`ExchangeSyncRunResponse` plus `joined: bool`. It is always 200, whatever the run's own status,
for the reason `POST /api/balances/sync` gives. A manual sync is the one that retries an
`auth_failed` account.

### `GET /api/exchanges/runs?limit=20` → 200 `ExchangeSyncRunListResponse`

`limit` runs from 1 to 100 (default 20); anything else is a 422. The runs come newest first,
by `id`.

```json
{"runs": [{
  "run_id": 7, "trigger": "scheduled", "status": "partial",
  "started_at": "...", "finished_at": "...", "duration_ms": 1840,
  "accounts_total": 2, "accounts_succeeded": 1, "accounts_failed": 1, "accounts_skipped": 0,
  "fills_seen": 3, "fills_inserted": 1,
  "accounts": [{
    "exchange_key": "bitget", "status": "success",
    "windows_completed": 1, "pages": 1, "fills_seen": 3, "fills_inserted": 1,
    "error_kind": null, "detail": null
  }]
}]}
```

`fills_seen`/`fills_inserted` on a run are the sums of its accounts' counts, computed in
Python. The accounts come sorted by `exchange_key`.

## Data model

Migration `0007_exchange_sync` (revises `0006_exchanges`). Every `CHECK` text is a constant in
`db/models.py`, repeated verbatim in the migration and covered by the existing reflection-test
pattern.

**`exchange_accounts`**, via `batch_alter_table`:

| Column | Type | Notes |
|---|---|---|
| `sync_status` | `TEXT NOT NULL DEFAULT 'never_synced'` | `CHECK (sync_status IN ('auth_failed', 'error', 'never_synced', 'ok'))`, the `AccountSyncStatus` members |
| `requested_since` | `UtcDateTime NULL` | what the owner asked for, as of the last plan |
| `effective_since` | `UtcDateTime NULL` | the floor of the planned history: the earliest instant held once no window is pending |
| `planned_until` | `UtcDateTime NULL` | the ceiling of the planned history |
| `last_synced_at` | `UtcDateTime NULL` | when a run last left the account with nothing pending |

**`exchange_sync_windows`**: the pending queue.

- `id`
- `exchange_account_id` (FK `CASCADE`, indexed)
- `since` and `until`, both `UtcDateTime NOT NULL`
- `symbol TEXT NULL`
- `cursor TEXT NULL`

There is no `CHECK` comparing `since` and `until`, because that would compare `TEXT` datetimes
in SQL. `FillWindow` refuses an inverted one when the row is read.

**`exchange_sync_runs`:**

- `id`
- `trigger` and `status`, with the same `CHECK` constants as `sync_runs`
- `started_at`, indexed
- `finished_at`, `duration_ms`
- `accounts_total`, `accounts_succeeded`, `accounts_failed`, `accounts_skipped`: integers, the
  last three defaulting to `0`

**`exchange_sync_run_accounts`:**

- `id`
- `exchange_sync_run_id` (FK `CASCADE`)
- `exchange_account_id` (FK `CASCADE`)
- `status`, `CHECK IN ('failed', 'skipped', 'success')`
- `windows_completed`, `pages`, `fills_seen`, `fills_inserted`: integers
- `error_kind`, `NULL` or one of `auth`, `conflict`, `insufficient_scope`, `internal`,
  `invalid_request`, `rate_limited`, `retention_window`, `schema`, `unavailable`
- `detail TEXT NULL`
- `UNIQUE (exchange_sync_run_id, exchange_account_id)`

**Triggers:** `exchange_fills_no_update` and `exchange_fills_no_delete`, as above.

**Reversible, with loss of bookkeeping only.** The downgrade drops the triggers, the three
tables and the five columns. The fills are untouched. What is lost is the run log and the
checkpoints. The next sync after a re-upgrade plans from scratch, and the constraint makes the
re-read free. `exchange_sync_runs` has no fills to lose, unlike `0006`.

## Acceptance criteria

Verbatim from the issue, with the interpretation where one was needed.

1. A crash mid-window resumes with no duplicates and no gaps, tested with a fake provider
   that raises on the third page.
   *"Crash" is tested two ways:*
   - *an exception out of page 3, where the run records the failure;*
   - *a `BaseException` that is not an `Exception` out of page 3, standing in for the process
     dying: no outcome is written and the run row stays `running` for the sweep.*

   *Either way, a fresh service over the same database file resumes with page 2's
   `next_cursor`, not from the start of the window.*
2. Re-running a completed sync inserts zero rows.
   *And "resync is append-only": no code path updates or deletes a fill, and the database
   refuses both. A same-id fill that differs is refused as a conflict. Recording it as an
   adjustment is M4's.*
3. The retention clamp records both `requested_since` and `effective_since`.
   *Recorded on the account and served by `GET /api/exchanges`, with `history_truncated`.*
4. An auth error sets the account to `auth_failed` and stops **without retrying**: it is
   terminal and needs the user, not a backoff.
   *This includes the scope subclass. Later scheduled runs skip the account; a manual sync
   retries it.*
5. A rate-limit error retries and the run still completes.
6. Scheduled and manual sync, plus `GET /api/exchanges` and a run-history endpoint.
7. **No endpoint returns a key, secret or passphrase**, only `configured: bool` and a status.
8. A sentinel-secret test proves absence from every response body and every log line.

## Test plan

| # | Criterion | Test |
|---|---|---|
| 1 | crash on page 3, exception | `backend/tests/services/test_exchange_sync.py::test_a_failure_on_the_third_page_resumes_at_the_committed_cursor` |
| 1 | crash on page 3, process death | `...::test_a_crash_on_the_third_page_resumes_with_no_duplicates_and_no_gaps` (a `BaseException` subclass, then a fresh service on the same file; every fill stored exactly once; the fake's call log shows page 3 requested with page 2's cursor) |
| 1 | the page is one transaction | `...::test_a_commit_that_fails_leaves_neither_the_fills_nor_the_cursor` |
| 2 | re-run inserts zero | `...::test_rerunning_a_completed_sync_inserts_zero_rows` (`seen > 0` from the overlap, `inserted == 0`) |
| 2 | constraint, not logic | `backend/tests/db/test_exchange_sync_repository.py::test_inserting_a_page_twice_inserts_nothing_the_second_time` |
| 2 | collision | `...::test_a_same_id_fill_that_differs_raises_and_writes_nothing`; `...::test_a_same_id_fill_whose_payload_alone_differs_is_not_a_conflict`; `test_exchange_sync.py::test_a_conflict_fails_the_account_without_advancing_the_cursor` |
| 2 | append-only | `backend/tests/db/test_exchange_sync_migration.py::test_updating_a_fill_is_refused`, `::test_deleting_a_fill_is_refused`, `::test_the_triggers_match_the_migration_text` |
| 3 | clamp recorded | `test_exchange_sync.py::test_the_retention_clamp_records_requested_and_effective_since`; `backend/tests/api/test_exchanges.py::test_the_account_list_reports_a_truncated_history` |
| 4 | auth stops | `test_exchange_sync.py::test_an_auth_error_marks_the_account_and_stops_without_retrying` (one provider call); `::test_insufficient_scope_is_auth_failed_too`; `::test_a_scheduled_run_skips_an_auth_failed_account`; `::test_a_manual_run_retries_an_auth_failed_account` |
| 5 | rate limit | `...::test_a_rate_limit_is_retried_and_the_run_completes` (the sleeper sees whole seconds, rounded up); `...::test_exhausted_retries_fail_the_account_and_keep_the_checkpoint`; `...::test_a_wait_beyond_the_cap_is_not_slept` |
| 6 | endpoints | `test_exchanges.py`: list, empty list, `configured: false` for an account without credentials, `syncing`, `last_error` (skipped outcomes ignored), manual sync with `joined`, runs newest first, `limit` bounds 422, 401 without a session (also by `tests/auth/test_route_contract.py`) |
| 6 | scheduler and lifespan | `backend/tests/db/test_lifespan.py`: built when enabled and configured, not built when disabled or when nothing is configured, both run tables swept, both coordinators drained; `tests/services/test_sync_coordinator.py` unchanged in meaning for balances plus the exchange instance |
| 7 | no credential fields | `backend/tests/security/test_exchange_sync_secrets.py::test_no_response_model_has_a_credential_field` (walks the OpenAPI document) |
| 8 | sentinel | `...::test_sentinel_credentials_reach_no_response_and_no_log_line`: the real application, Bitget configured with sentinel credentials, `bitget_harness`'s fake venue on the transport, driven through success, 401, 429-then-200 and 5xx. Every response body of all three endpoints and every captured log record (structlog and stdlib) is searched for **every 5-character window** of each sentinel (spec 014's lesson) |
| — | planning | `backend/tests/services/test_exchange_sync_plan.py`: newest first, whole milliseconds, the oldest window shorter, overlap exactly `OVERLAP`, a clock stepped back, the bottom range, `normalise_pending` drop/shrink/re-split, `split_in_half`, `seconds_to_wait` rounding and cap |
| — | loop behaviour | `test_exchange_sync.py`: newest-first call order across pending and new windows; a cursor cycle A→B→A; `NONE` split; `requires_symbol` windows × symbols; retention step and its exhaustion; one venue failing and the other succeeding gives `partial`; an internal error gives `internal` with the type name and a traceback in the log; no owner, two owners |
| — | schema | `test_exchange_sync_migration.py`: upgrade and downgrade, `CHECK` reflection for every new constraint, the fills untouched by a downgrade |
| — | layering | `backend/tests/test_import_contracts.py`: the new contract, with a planted violation |
| — | settings | `backend/tests/test_config.py`: the four settings, interval below 1 refused, a history start in the future refused |

Mutations the tester must see killed:

- `OVERLAP` set to zero;
- newest-first reversed;
- the checkpoint committed separately from the fills;
- `DO NOTHING` made a plain insert;
- a comparison field dropped from the collision check;
- the skip condition applied to manual runs;
- the retry count off by one;
- the wait rounded down;
- cycle detection removed;
- the window row not deleted on a `None` cursor;
- `normalise_pending`'s `<=` made `<`;
- `history_truncated`'s `>` made `>=`;
- the skipped-outcome filter in `last_error` removed.

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/**`, `backend/.importlinter`, `frontend/src/api/generated/schema.ts` (regenerated with `scripts/dump_openapi.py` + `npm run gen:api`, never hand-edited), `docs/operations.md`, `docs/providers.md` |
| tester | `backend/tests/**` |
| reviewer | nothing (reads, reports) |
| tech lead | this spec, `backend/pyproject.toml` (the coverage floor) |

## Risks

- **A cursor must stay valid across a restart.** A Bitget `tradeId` does. A venue whose cursor
  is session-bound or expires would break resume. #14 must check its venue, and a stale
  cursor would surface as that venue's invalid-request or schema error.
- **The collision policy can stall an account.** A venue that revises a settled fill under the
  same id stops that account at that page until someone acts. That is deliberate, since
  keeping the first version silently is worse. The adjustment model belongs to M4.
- **`RETENTION_STEP` of one day is a guess.** So is `RETENTION_MARGIN` before it. The owner's
  first real sync is the first measurement.
- **`requires_symbol` is planned with the candidates known at plan time.** A symbol first
  traded later is read only from the windows planned after it appears. No wired venue
  requires a symbol; #14 decides whether BingX does and whether that is enough.
- **Recovering from `auth_failed` needs a manual sync after the restart.** Documented in
  `docs/operations.md`. #16 gives it a button.
- **One run can hold the coordinator for a long first backfill.** 90 days at one request a
  second (the host limiter's default) is seconds for a personal account, but a bot trader's
  history is not. A manual click joins rather than piling up, and each page is durable, so a
  shutdown mid-backfill loses at most one page of work.
