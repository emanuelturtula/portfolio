# 023 — Manual adjustments for opening balances and off-exchange acquisitions

Issue: #18
Status: in progress

## Problem

The venues keep only a window of history (Bitget: 90 days), so the owner holds coins that were
bought before the imported history begins. A sale of those coins exceeds what the history
holds. The engine then empties the pool, warns `negative_inventory` and flags the asset
`history_incomplete` for good (spec 019). The dashboard (#20) says so, and nothing lets the
owner fix it.

The engine already has the event that fixes it: `Adjustment`, an inflow of `quantity` of an
asset at a `unit_cost`, or at unknown cost when `unit_cost` is `None` (spec 019). What is
missing is somewhere to keep adjustments, a way to enter them, and the recompute reading them.

## Scope

- **Storage**: a `manual_adjustments` table and its migration.
- **Code**: a repository, a service, and CRUD endpoints under `/api/accounting/adjustments`.
- **Recompute**: it replays the adjustments together with the fills, in one event list, in
  the engine's order.
- **Triggers**: creating, editing or deleting an adjustment recomputes the snapshot before the
  response returns.
- **Validation at entry**: an adjustment the engine could not replay is refused when it is
  entered, never stored (the lesson of #99).

## Non-goals

- **A UI.** The owner enters adjustments through the authenticated API. `/api/docs` works for
  that while signed in. A dashboard form is filed as #111.
- **Outflows**, such as a gift sent, a loss, or a withdrawal to a wallet not tracked. The
  engine's `Adjustment` is an inflow, and quantity must be above zero. Outflows are a
  separate question about what a disposal with no proceeds means for realized P&L. They are
  not needed to fix an opening balance.
- **Transfers.** The engine's `Transfer` event exists, and nothing records one yet (#104
  territory).
- **Currency.** `unit_cost` is in the unit of account, USD (USDT/USDC pinned at 1), like every
  basis the engine holds.

## Design

### Data model (migration `v0009_manual_adjustments`, reversible)

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK, **AUTOINCREMENT** | An id is never reused after a delete, so it names one adjustment forever. It is also the event's identity (below). |
| `user_id` | INTEGER FK `users.id` **ON DELETE CASCADE**, indexed | Owner data, like `wallets`. |
| `asset` | TEXT NOT NULL | The symbol as the venues spell it, for example `BTC` (see Validation). |
| `quantity` | NumericText(18) NOT NULL | Above zero. |
| `unit_cost` | NumericText(18) NULL | USD per unit. **NULL is unknown cost, never zero.** A zero is accepted and means a known cost of nothing. |
| `occurred_at` | UtcDateTime NOT NULL | When the coins were acquired. It is what orders the adjustment among the fills. |
| `note` | TEXT NOT NULL | Why, in the owner's words. `CHECK (trim(note) <> '')`, named. |
| `created_at`, `updated_at` | UtcDateTime NOT NULL | |

- No `CHECK` constrains a money column. A comparison on a TEXT money column coerces to float.
- No SQL `ORDER BY` on `occurred_at` or on money. The list is ordered in Python.

### The event

Each row becomes the engine's `Adjustment`:

- `key`: `EventKey(occurred_at, source="manual", external_id=f"{id:020d}")`.
  - `"manual"` is the source `lot_kinds_of` already expects for an adjustment.
  - Zero-padding the id to 20 digits makes the text order of `external_id` equal the numeric
    order of `id`, so two adjustments at one instant replay in the order they were entered.
- `asset`, `quantity` and `unit_cost` are copied as stored.

**The order among the fills** is the engine's: `(occurred_at, source, external_id, kind)`
(spec 019). An adjustment at the same instant as a fill sorts after it, because `"manual"`
sorts after every venue key. An owner who records an opening balance dates it before the
first sale it must cover. The API documentation says so.

### Loading and recompute

`AccountingService.recompute` loads the owner's fills and adjustments. Both go to the worker
thread, which builds **one** event list; `_replay_fills` becomes `_replay_events`.

A stored adjustment that does not convert to an `Adjustment` fails the recompute with
`UnconvertibleAdjustmentError`:

- It carries `adjustment_id` as an attribute. Its message names nothing.
- It pickles, like `UnconvertibleFillError`.
- It is unreachable in practice, because validation refuses the same shapes at entry. It is
  kept for the same reason the fill version is: never skip silently.

`RecomputeReason` gains `ADJUSTMENT = "adjustment"`.

### Triggering the recompute from a request

The trigger `run_accounting_recompute(app, reason)` lives in `main.py`. A router may not import
`main`, and business logic may not live in a router. So:

- `install_accounting_runtime` also publishes `app.state.accounting_recompute`, a callable
  `(RecomputeReason) -> Awaitable[AccountingStatus]` bound to the app.
- The dependency `get_adjustment_service` builds `AdjustmentService` with an `after_change`
  callable that awaits it with `RecomputeReason.ADJUSTMENT`.
- The service commits its own write, then awaits `after_change`, then returns. The router
  calls one service method and serialises.
- **The recompute is awaited before the response.** A client that re-reads
  `/api/accounting/positions` after the response sees the new snapshot. Criterion 8 of spec
  021 measured 53 ms on the Pi.
- The recompute never raises. If it fails, the adjustment stays saved and the previous
  snapshot stays served. `last_recompute` on the positions endpoint says `failed`, as for a
  sync.
- The trigger's lock serialises it with a sync's recompute.

### Validation (service, before anything is stored)

- **`asset`**: `^[A-Z0-9]{1,20}$`, the symbol exactly as the exchanges spell it.
  - A lower-case symbol is **refused, not upper-cased**. Silently changing what the owner
    typed is the wrong fix.
  - The error says to use the exchange's symbol.
  - A cash asset (`DEFAULT_CASH_ASSETS`: USDT, USDC) is refused. It is the unit of account, an
    adjustment of it changes nothing, and entering one is a mistake.
- **`quantity`**: above zero. `unit_cost`, when given, is zero or more. Both are **JSON
  strings**; a JSON number is refused. They must satisfy the engine's amount rule: at most 18
  fractional digits, value-based as in spec 019 R9, and within the integer-digit limit. The
  total cost must fit (spec 019, R2).
- **The engine is the authority on the rules.** The service builds the domain `Adjustment`
  from the input and maps its `ValueError` to a 422. Both the entry path and the recompute
  path therefore apply one rule, and cannot drift, as in #99.
  - The 422 names the field and the rule, never the value.
  - A property test holds that every adjustment the API accepts converts.
- **`occurred_at`**: a timezone-aware ISO 8601 datetime; a naive one is refused. It must not
  be later than now, on the service's injected clock. It must be convertible to UTC (spec 020,
  R1).
- **`note`**: required. It must not be blank after trimming, is at most 500 characters, and
  must be UTF-8-encodable. It is stored as given.

### Endpoints

All are authenticated by the middleware. Nothing is added to `PUBLIC_API_PATHS`.

| Method | Path | Answer |
|---|---|---|
| `GET` | `/api/accounting/adjustments` | `200 {"adjustments": [...]}`, ordered by `occurred_at` then `id`, in Python |
| `POST` | `/api/accounting/adjustments` | `201` with the adjustment, after the recompute |
| `PUT` | `/api/accounting/adjustments/{id}` | `200` with the adjustment, after the recompute. A **full replacement** of the five editable fields. |
| `DELETE` | `/api/accounting/adjustments/{id}` | `204`, after the recompute |

- **`PUT`, not `PATCH`.** `unit_cost: null` is a meaningful value (unknown cost), and PATCH
  would make it indistinguishable from "not sent".
- **Another owner's id and a missing id are the same `404`.** The problem detail does not
  say which.
- **The adjustment on the wire**:

  ```json
  {"id": 7, "asset": "BTC", "quantity": "0.5", "unit_cost": "30000" | null,
   "occurred_at": "...", "note": "...", "created_at": "...", "updated_at": "..."}
  ```

  - Money is sent as strings at 18 places, like the positions endpoint.
  - `unit_cost: null` is unknown cost. The schema description says it is **not zero**, and
    that the asset then shows `unknown_basis` on the positions endpoint.
- **The request body** has `asset`, `quantity`, `unit_cost` (nullable, required to be present
  on PUT and optional on POST, defaulting to null), `occurred_at` and `note`.

### Logging

- Log `adjustment_created`, `adjustment_updated` and `adjustment_deleted` with the id only.
- Never log the asset, amounts, dates or the note. The note is free text the owner wrote.

### Documentation

- `docs/accounting.md` gains a section "Recording what the history does not show":
  - what an adjustment is;
  - dating an opening balance before the first sale;
  - unknown cost versus zero;
  - an example that resolves a `negative_inventory` warning.
- `docs/operations.md` names `UnconvertibleAdjustmentError`.

## API contract

Above. The OpenAPI document gains the four operations. `frontend/src/api/generated/schema.ts`
is regenerated, because the drift job checks it.

## Acceptance criteria

1. The `manual_adjustments` table, repository, service and CRUD endpoints exist. The migration
   upgrades and downgrades.
2. An opening balance dated before a sale that exceeded the history removes that sale's
   `negative_inventory` warning, and the `history_incomplete` flag the sale caused, after the
   recompute. The realized P&L of the sale is then computed against the adjustment's cost.
3. An adjustment without a unit cost counts toward quantity. While those units are held, the
   asset shows `unknown_basis` with that quantity in `unknown_basis_quantity` (R3).
   It is never valued at zero cost: the sale of those units realizes no profit, and the
   proceeds go to `unmatched_proceeds`.
4. Adjustments order deterministically alongside executions, in the engine's order. A replay
   of the same stored rows gives the same fingerprint.
5. Creating, editing or deleting an adjustment triggers a recompute before the response
   returns. A failed recompute leaves the change saved and shows `failed` in
   `last_recompute`.
6. Every adjustment carries a non-blank note.
7. Anything the engine would refuse is refused at entry with a 422 that names no value. Every
   accepted adjustment converts, and a property test proves it.
8. Endpoints are authenticated. The route-walking contract test covers them, and
   `PUBLIC_API_PATHS` is unchanged. Another owner's adjustment is a 404.
9. Nothing sensitive is logged: no amounts, notes, assets or dates. Money on the wire is
   strings, and no JSON number is accepted for money.
10. The gate passes with coverage floors unchanged or raised.

## Test plan

| # | Test |
|---|---|
| 1 | `tests/db/`: migration up and down, the columns, the cascade, `AUTOINCREMENT`, the note `CHECK`. Repository CRUD tests. |
| 2 | Service or API: fills with an oversell produce the warning and the flag; an adjustment dated before them removes both, and the realized P&L matches a hand computation. |
| 3 | `unit_cost: null`: `unknown_basis` flag and quantity on positions; a later sale gives zero realized P&L and non-zero `unmatched_proceeds`. |
| 4 | Two adjustments at one instant and a fill at the same instant replay in the documented order. The fingerprint is stable across two recomputes. `external_id` padding keeps numeric order (id 9 before id 10). |
| 5 | POST, PUT and DELETE each call the trigger once, after the commit. A failing trigger still returns success and the row is saved. An end-to-end test through the app shows positions changed after the response. |
| 6, 7 | 422 cases: blank note, a note over 500 characters, lower-case asset, cash asset, zero or negative quantity, negative unit cost, too many fractional digits, too many integer digits, total cost overflow, naive datetime, future datetime, a JSON number for money. No value is echoed. Hypothesis: every accepted body converts to `Adjustment`. |
| 8 | The contract tests (route walk and allowlist pin), and a 404 for another owner's id. |
| 9 | A log-capture test: no note, amount or asset appears in the logs of a create, update or delete. |

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/**`: `db/models.py`, `db/migrations/versions/v0009_manual_adjustments.py`, `repositories/adjustments.py`, `services/adjustments.py`, `services/accounting.py`, `api/routers/adjustments.py`, `api/schemas/adjustments.py`, `api/dependencies.py`, `main.py`. Also `frontend/src/api/generated/schema.ts` (regenerated only), `docs/accounting.md` and `docs/operations.md` |
| tester | `backend/tests/**` |
| reviewer | nothing |
| tech lead | this spec |

## Risks

- **An opening balance dated at the same instant as the sale it should cover** sorts after
  the sale and does not cover it. It is documented, and the owner picks the date. A later UI
  can default it to before the earliest fill.
- **An adjustment entered with the wrong asset spelling** makes a separate pool. The strict
  pattern catches case. It cannot catch `XBT` versus `BTC`.
- **The recompute runs inside the request.** At 53 ms on the Pi that is fine. If a history
  ever makes it slow, the trigger can move to a background task without an API change,
  because the response carries no recompute result.

## Rulings during implementation

- **R1. The conversion lives beside `trade_of`.** `ADJUSTMENT_SOURCE`, `external_id_of`,
  `adjustment_of` and `UnconvertibleAdjustmentError` are defined in `services/accounting.py`
  and re-exported from `services/adjustments.py`. Defining them in `adjustments` would make
  the two modules import each other. There is still one definition.
- **R2. `AUTOINCREMENT` costs the primary key its name.** SQLAlchemy writes the key inline for
  `sqlite_autoincrement`, so `pk_manual_adjustments` exists in the metadata and not in the
  DDL. A future Alembic batch rebuild of this table must pass
  `table_kwargs={"sqlite_autoincrement": True}` or it drops `AUTOINCREMENT`. The migration
  and the model say so.
- **R3. `unknown_basis` describes units still held.** The flag is not sticky (spec 019), so it
  clears once the unknown-cost units are sold. What is permanent is that their sale realizes
  nothing and its proceeds go to `unmatched_proceeds`. Criterion 3 is reworded to match.
- **R4. Validation order and trigger failures (developer).**
  - Only the first failure is reported, checked in the order asset, occurred_at, quantity,
    unit_cost, note.
  - A PUT validates its body before looking up the id, so a bad body on a missing id is a
    422. That reveals nothing about which ids exist.
  - If `after_change` raises, the service logs `adjustment_after_change_failed` with the id
    and the error class, and still answers success. The change is committed, and a 500 would
    say it was not. The real trigger never raises; the service does not rely on that promise.
    A cancellation is not caught.
- **R5. No JSON number where the spec asks for a string.**
  - Amounts refuse every JSON number, integers included. That is stricter than the shared
    money type elsewhere, and it is what "a JSON number is refused" means.
  - `occurred_at` refuses a JSON number too. Pydantic would otherwise read it as a Unix
    timestamp, and the contract is an ISO 8601 string with a timezone.
- **R6. The limits are machine-readable, with one authority.** The OpenAPI schema states
  `maxLength` for `note` and `pattern` for `asset` as schema metadata, taken from the
  service's own constants (`NOTE_MAX_LENGTH`, the asset pattern). The service remains the
  only validator. This keeps its field-specific messages, and gives #111's form limits it can
  read.
  openapi-typescript does not carry `pattern` or `maxLength` into `schema.ts`, so #111
  reads them from `/api/openapi.json` or mirrors the constants.
- **R7. No guard for a trigger that is always installed (tester).** `create_app` installs
  `app.state.accounting_recompute` through `install_accounting_runtime`, whether or not the
  lifespan runs. A `RuntimeError` for its absence could only be reached by a test that
  deletes the attribute, so the dependency reads it directly (#20's R1 rule). The
  coordinators' guards differ: an application whose lifespan never ran really has no
  coordinator.
- **R8. Review findings (reviewer, before the pull request).** The baseline gate passed at
  `0095233` before these fixes.
  - **Must-fix: `occurred_at` accepted Unix time spelled as a string.** Pydantic's lax
    datetime parser reads a string of digits as Unix time, and the result is aware:
    - `"1767225600"` was stored as 2026-01-01;
    - `"20260101"`, a valid ISO 8601 basic-format date, was stored as 1970-08-23. That
      silently replays an acquisition before the whole history.

    A string is now parsed with `datetime.fromisoformat`, and a failure is a fixed 422 that
    quotes nothing. `"20260101"` then parses as a naive date, which the service refuses as
    not timezone-aware.
  - **Should-fix: DELETE through `/api/docs` is refused.** The write guard requires
    `Content-Type: application/json` on every non-safe method, and Swagger UI sends none for
    a bodiless operation, so DELETE there answers 403. The docs keep `/api/docs` for list,
    create and replace. For delete, they give a `fetch` to run in the browser console on the
    signed-in app's page. The middleware is unchanged: relaxing it is a security decision,
    not this issue's.
  - **Nits taken:**
    - An id beyond 64 bits is a 422 (`Path(ge=1, le=2**63 - 1)`), not an `OverflowError` 500.
    - The schema descriptions are built from the engine's constants.
    - `accounting_recompute_failed` also carries `adjustment_id` when the error is
      `UnconvertibleAdjustmentError`, so an operator can find the row to fix. Adjustment ids
      are already logged; fill ids still are not.
    - A guard test holds every `ExchangeKey` below `"manual"`, so a future venue cannot
      silently reverse the documented same-instant order.
    - A test shows the recompute finishes before `http.response.start`, which a background
      task would not.
  - *After the fix:* the bound makes an id of zero or below a 422 as well, since no id can
    be one. A 404 remains the answer for any id in range that the owner does not have.
- **R9. Two behaviours the endpoint table left implicit (tester).**
  - **A repeated DELETE answers 404.** The adjustment no longer exists. This matches a
    missing id, not an idempotent 204.
  - **The table's note `CHECK` is weaker than the service.** SQLite's `trim()` removes spaces
    only, while the service strips all whitespace, so a note of tabs or no-break spaces is
    refused by the service. The `CHECK` is a backstop against an empty string written
    around the service, not the rule.
