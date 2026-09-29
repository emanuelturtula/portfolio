# 021 — Position snapshots and the invested-per-asset endpoint

Issue: #19
Status: implementing

## Problem

The engine from #17 (spec 019) computes cost basis in memory, but nothing runs it on the
stored fills, keeps the result, or shows it. The owner still cannot see how much is invested
in each asset, what it is worth now, or what it has gained.

## Scope

- **An accounting service.** It loads the owner's fills, converts each to a `Trade`, calls
  `replay`, and persists the result as a snapshot: positions, lots and warnings, with the
  `input_fingerprint`, `method` and `engine_version`. It skips the write when the fingerprint
  is unchanged.
- **Recompute triggers**:
  - after an exchange sync run that inserted fills;
  - once at startup, which covers the first deploy over existing fills and an engine upgrade.

  Recomputes are serialised, and a failure never fails the sync or the startup.
- **`GET /api/accounting/positions`.** Per asset: quantity, average cost, total invested,
  current price, market value, unrealized P&L and percentage return. It also carries
  portfolio totals, flags and warnings, and the snapshot's age. Valuation is in USD.
- **A pure valuation function in `domain/accounting/`.** It combines a position with a
  price, so that the arithmetic stays under the engine's exactness rules and its coverage
  floor.

## Non-goals

- **Manual adjustments** are #18. The service's event loader is written so that #18 adds a
  second source of events, not a second pipeline.
- **The dashboard** is #20. This spec gives it every figure and flag it needs, so #20 sums
  nothing.
- **Snapshot history or a value-over-time chart.** One current snapshot per owner and method
  is kept.
- **EUR valuation.** The unit of account is USDT/USDC, pinned at 1 (spec 019), so it is
  USD-equivalent. Valuing it in EUR needs a historical FX rate at every acquisition, which does
  not exist. A cross rate at today's price would be the mistake `QuoteCurrency`'s docstring
  refuses.
- **Comparing replay quantity with the balances actually held**, which spec 019's *Risks*
  recommends considering. It is worth its own issue, because venue balances are not read
  today. It is filed as #104.
- **Total return mixing realized and unrealized P&L.** Both are returned separately.
  *Absolute return* is the unrealized P&L; see *Acceptance criteria*.

## Design

### Layers

```
api/routers/accounting.py        GET /api/accounting/positions            (new)
api/schemas/accounting.py        response models, money as MoneyStr       (new)
services/accounting.py           AccountingService: recompute, positions  (new)
domain/accounting/valuation.py   value_position(), value_portfolio()      (new, pure)
repositories/accounting.py       snapshot header, positions, lots, warnings (new)
repositories/exchanges.py        + list_fills_for_accounting(user_id)     (read, no raw_payload)
db/models.py + migration v0008_accounting                                  (new tables)
main.py                          recompute lock, startup recompute, post-sync trigger
api/dependencies.py              get_accounting_service
```

The router imports only the service and schemas (rule 4). The service imports
`services.prices` for lookups, which is provider-free, and never `providers.*`. That keeps
`prices-are-never-fetched-in-a-request` and `api-never-reaches-an-exchange-provider` green
without any edit to `.importlinter`.

### Loading events

`ExchangeFillRepository.list_fills_for_accounting(user_id)`:

- It returns every fill of every exchange account the user owns, as plain records.
- Each record carries the fill's fields minus `raw_payload`, plus the account's
  `exchange_key` and `id`.
- It selects the columns explicitly, so `raw_payload` is never loaded (rule 3). Nothing is
  compared or ordered in SQL except by integer `id`, and `replay` sorts anyway.

The service maps each record to a `Trade`:

- `EventKey(executed_at, source=exchange_key, external_id=external_trade_id)`;
- the trade fields copied as they are.

**A record that does not convert** raises `UnconvertibleFillError`. It carries
`exchange_account_id` and `external_trade_id` **as attributes, never in its message**, and
it pickles and copies the way `ConflictingEventError` does. The recompute then fails
loudly, and the previous snapshot stays (spec 020, *For #19*). After #99 no such row can be
written, and the one-time backfills hold zero fees. No stored row is expected to hit this;
the first recompute on the Pi is what confirms it.

### Recompute

`AccountingService.recompute(user_id) -> RecomputeOutcome`:

1. Load and convert the events.
2. Compute the fingerprint, with `replay` run **off the event loop** through
   `anyio.to_thread.run_sync`. It is pure and CPU-bound, and on the Pi a large history
   would otherwise stall every request for its duration.
3. Read the stored header for `(user_id, METHOD)`. If its `input_fingerprint` equals the
   new one, return `UNCHANGED` and write nothing. `ENGINE_VERSION` is in the fingerprint,
   so an engine upgrade always recomputes.
4. Otherwise, in **one transaction**: delete the old header (the cascade removes its
   positions, lots and warnings), insert the new header and its children, and commit.
   Return `WRITTEN`.

A value that `NumericText(18)` refuses (past 10²⁰, spec 019 *Risks*) makes the write raise.
The transaction rolls back and the old snapshot stays.

**The trigger** is `run_accounting_recompute(app, reason)` in `main.py`:

- It takes an `asyncio.Lock` on `app.state.accounting_lock`, so two recomputes never
  interleave their writes.
- It opens its own session and recomputes for every user. There is one today, and the loop
  is the honest shape for "every owner".
- It logs `accounting_recompute_finished` (reason, duration_ms, event_count, outcome) or
  `accounting_recompute_failed` (reason, error **class name only**).
- It records the last outcome in memory on `app.state.accounting_status` (at, outcome,
  error class), for the endpoint.
- It never raises.

It is called in two places:

- **After an exchange sync run with `fills_inserted > 0`**, inside `exchange_sync_runner`,
  after `service.sync(trigger)` returns and before the summary is handed back. A manual
  sync's response therefore reflects the new snapshot. A recompute failure is logged; the
  sync's own result is untouched.
- **Once at startup**, as a background task the lifespan starts after migrations and
  cancels on shutdown. It does not delay readiness, so the deploy health check never waits
  on it.

### Valuation (pure, `domain/accounting/valuation.py`)

`value_position(position, price: Decimal | None, price_reason: str | None) -> PositionValue`,
with `Pk = price × known quantity` and `C = cost_basis`:

| Field | Rule |
|---|---|
| `market_value` | `quantize(multiply(quantity, price), 18)`. `quantity` is the **total**, including unknown-basis units: it is what the holding is worth. `None` when the price is missing and `quantity > 0`. `"0"` when `quantity == 0`, whatever the price. |
| `unrealized_pnl` | `quantize(multiply(Qk, price), 18) − C`, over the **known** part only, since the unknown part has no cost to compare against. `None` when the price is missing and `Qk > 0`. `0` when `Qk == 0`. |
| `unrealized_return_pct` | `divide(multiply(unrealized_pnl, 100), C, 4)`. `None` when `unrealized_pnl` is `None` or `C <= 0`. A zero or negative basis has no meaningful percentage. |
| `market_value_unavailable_reason` | the price reason when `market_value` is `None`. |

`value_portfolio(values) -> PortfolioTotals`, summed with `money.add`:

- **`total_invested`**, **`market_value`** and **`unrealized_pnl`** cover only the positions
  that are **fully comparable**: priced (or holding nothing), with no `UNKNOWN_BASIS` flag.
  Mixing in a value with no cost, or a cost with no value, makes the percentage a fiction
  (#20: "excluded from the aggregate return, with an explanation").
- **`unrealized_return_pct`** is computed over those same positions.
- **`realized_pnl`** is summed over all positions, because realized figures are known
  whatever the current price is.
- **`excluded`**: the positions left out, each with the reason (`unknown_basis`,
  `unpriced`). #20 renders them as the explanation.

The valuation uses the price as it is, stale or not. Staleness is shown, never hidden (#20
shows the price's age).

### Price lookup

The service values in USD and looks up each position's asset:

- An asset that is not a chain's `asset_symbol` (`ChainKey`, in `domain`) is
  `unsupported_pair`, without a lookup. Only chain assets are priced (`SUPPORTED_PAIRS` in
  `providers/prices/base.py`, which a request path may not import).
- Otherwise the service calls `PriceService.lookup_price(asset, "USD")`, which returns a
  `Price` or `never_fetched`.

A test holds the set of chain asset symbols equal to the set of assets in `SUPPORTED_PAIRS`,
the way `domain/currencies.py`'s copies are held together.

### Data model (migration `v0008_accounting`, reversible)

| Table | Columns | Notes |
|---|---|---|
| `accounting_snapshots` | `id`, `user_id` FK users **ON DELETE CASCADE**, `method`, `engine_version` INTEGER, `input_fingerprint`, `event_count` INTEGER, `unallocated_costs` NumericText(18), `computed_at` UtcDateTime | `UNIQUE (user_id, method)`: one current snapshot. Derived data, so the cascade on the owner is correct: it is recomputable. |
| `accounting_positions` | `id`, `snapshot_id` FK **ON DELETE CASCADE**, `asset`, `quantity`, `unknown_basis_quantity`, `cost_basis`, `average_cost` (nullable), `realized_pnl`, `unmatched_proceeds`, all NumericText(18); `flags` TEXT | `UNIQUE (snapshot_id, asset)`. `flags` is the sorted, comma-joined `PositionFlag` values, or empty. |
| `accounting_lots` | `id`, `snapshot_id` FK CASCADE, `seq` INTEGER (event order), `asset`, `occurred_at`, `source`, `external_id`, `kind`, `quantity`, `cost_basis`, `unknown_basis_quantity` (NumericText(18)) | The issue's lots table: written and never read, until a FIFO pass exists. The method is on the header. |
| `accounting_warnings` | `id`, `snapshot_id` FK CASCADE, `seq` INTEGER, `kind` (`negative_inventory` / `unattributed_fee`), `occurred_at`, `source`, `asset`, `quantity` NumericText(18), `charged_to` (nullable) | `CHECK` on `kind`. |

`user_id` has the index its lookup needs through the unique constraint. Nothing is aggregated,
compared or ordered in SQL on a money column. The rows are read back and ordered by `asset`
or by `seq`, which are text and integer.

### Endpoint

`GET /api/accounting/positions`, authenticated by the middleware (not added to
`PUBLIC_API_PATHS`):

```jsonc
{
  "method": "weighted_average",
  "quote_currency": "USD",
  "computed_at": "2026-09-29T10:00:00Z",      // null: no snapshot yet
  "event_count": 312,
  "last_recompute": {"at": "...", "outcome": "unchanged|written|failed", "error": null | "UnconvertibleFillError"},  // null before the first attempt since start
  "positions": [{
    "asset": "BTC",
    "quantity": "1.5",                         // money: strings
    "unknown_basis_quantity": "0",
    "average_cost": "35000" | null,
    "total_invested": "52500",                 // = cost_basis of the known part
    "realized_pnl": "7500",
    "unmatched_proceeds": "0",
    "flags": ["history_incomplete"],
    "price": {"amount": "60000", "as_of": "...", "stale": false, "source": "..."} | null,
    "market_value": "90000" | null,
    "market_value_unavailable_reason": null | "never_fetched" | "unsupported_pair",
    "unrealized_pnl": "37500" | null,
    "unrealized_return_pct": "71.4286" | null
  }],
  "totals": {
    "total_invested": "...", "market_value": "...", "unrealized_pnl": "...",
    "unrealized_return_pct": "..." | null, "realized_pnl": "...",
    "excluded": [{"asset": "KAS", "reason": "unknown_basis"}]
  },
  "unallocated_costs": "0.1",
  "warnings": [{"kind": "negative_inventory", "occurred_at": "...", "source": "bitget", "asset": "BTC", "quantity": "0.5", "charged_to": null}]
}
```

- **Every money and quantity field is a JSON string** (`MoneyStr`). The percentage is a
  string too.
- **No snapshot** returns `200` with `computed_at: null`, empty `positions` and
  `warnings`, zero totals, and `last_recompute` as it stands.
- **Warnings carry no `external_id`.** The venue and the moment identify the fill for the
  owner, and trade ids stay out of anything that tends to end up in a log.
- The router reads `last_recompute` through a dependency over `app.state.accounting_status`,
  never `app.state` directly.

`api/schemas/accounting.py` has a module docstring. It is the first schema module whose
money fields are the owner's holdings and returns.

## Acceptance criteria

1. **`accounting_service` loads events, calls `replay`, and persists `position_snapshots`
   with `input_fingerprint` and `method`.** The table is `accounting_positions` under
   `accounting_snapshots`, which carries the fingerprint, method and engine version. Lots
   and warnings are persisted beside them.
2. **Recompute is idempotent: an unchanged fingerprint yields an unchanged snapshot.** No
   row is written, and `computed_at` does not move.
3. **Recompute triggers automatically after a sync that inserted rows.** It also runs once
   at startup, which the first deploy over existing fills needs.
4. **`GET /api/accounting/positions` returns per asset: quantity, average cost, total
   invested, current price, market value, unrealized P&L, and absolute and percentage
   return.** *Interpretation*: the absolute return is `unrealized_pnl`, the percentage
   return is `unrealized_return_pct`, and `realized_pnl` is beside them, never mixed in.
5. **Every money field serializes as a JSON string.**
6. **Unknown-basis assets are flagged rather than zeroed.** The asset gets
   `flags: ["unknown_basis"]` and a non-zero `unknown_basis_quantity`. Its market value
   counts every unit, and its P&L counts only the known part. It is excluded from the
   totals, with its reason.
7. **A missing price yields a null market value with a reason**: `never_fetched` or
   `unsupported_pair`.
8. **Recompute over the golden scenario completes in under two seconds on the Pi.**
   *Interpretation*: a test runs the service recompute over spec 019's golden scenario
   through a real SQLite file. It asserts under **0.5 s** on the development machine, a
   4× margin for the Pi. It also runs a scale case of 5,000 synthetic fills and records the
   measured time. After the deploy, the startup recompute's `duration_ms` in the Pi's log
   is the measurement on the real hardware. The tech lead reads it, with the owner's
   permission, and records it in this spec.
9. **The endpoint requires authentication**, proved by the contract test that walks every
   route, plus a direct `401` test. `PUBLIC_API_PATHS` is unchanged.

## Test plan

| # | Criterion | Test |
|---|---|---|
| 1 | persist | `tests/services/test_accounting.py::test_recompute_persists_*` (header fields, one position per asset, lots and warnings in `seq` order, flags text); `tests/db/` migration test: upgrade/downgrade, constraints reflected, cascade from users and from header |
| 1 | `raw_payload` never loaded | the repository test asserts the compiled SELECT names no `raw_payload`; a fill with a sentinel payload is recomputed and the sentinel appears nowhere in the rows or the logs |
| 1 | unconvertible row | a row inserted bypassing `NormalizedFill` (direct SQL) makes the recompute fail with `UnconvertibleFillError` carrying both ids as attributes, not in `str()`; the old snapshot is intact |
| 2 | idempotent | a second recompute is `UNCHANGED`; row ids and `computed_at` are unchanged; a new fill leads to `WRITTEN` |
| 3 | triggers | `exchange_sync_runner` with a fake sync: inserted > 0 recomputes, 0 does not; a failing recompute leaves the sync summary intact and logs the class name only; the startup task runs once, is cancelled cleanly on shutdown, and never blocks health; concurrent triggers are serialised by the lock |
| 4–7 | endpoint + valuation | `tests/domain/accounting/test_valuation.py` (every row of the valuation table, zero quantities, a missing price, `C <= 0`, totals exclusion, exactness under a hostile decimal context); `tests/api/test_accounting.py` (shape, strings via the OpenAPI schema and a real response, no snapshot, stale price, `unsupported_pair` vs `never_fetched`, warnings without `external_id`) |
| 7 | chain assets equal priced assets | `tests/services/test_accounting.py::test_priced_assets_are_the_chain_assets` |
| 8 | performance | `tests/services/test_accounting_performance.py` (golden < 0.5 s; the 5,000-fill case records its time) |
| 9 | auth | the existing route-walking contract test covers the new route; plus `tests/api/test_accounting.py::test_positions_require_a_session` |

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/**` (new `api/routers/accounting.py`, `api/schemas/accounting.py`, `services/accounting.py`, `domain/accounting/valuation.py`, `repositories/accounting.py`, `db/migrations/versions/v0008_accounting.py`; edits to `db/models.py`, `repositories/exchanges.py`, `main.py`, `api/dependencies.py`, `domain/accounting/__init__.py`), `frontend/src/api/generated/schema.ts` (regenerated, never hand-edited), `docs/operations.md` (the recompute log lines and what a failed recompute means), `docs/accounting.md` (a short "Where the figures are stored and served" section) |
| tester | `backend/tests/**` |
| reviewer | nothing |
| tech lead | this spec, `backend/pyproject.toml` |

## Risks

- **The startup recompute on the Pi is the first real run over the owner's fills.** It will
  show whether any stored row fails to convert. A failure is logged and visible in
  `last_recompute`, and the endpoint serves no snapshot until it is fixed. That is the
  loud outcome, not a silent one.
- **Holding the recompute inside the exchange runner lengthens the sync run** by the
  recompute time: seconds at most for a personal history. A manual sync's `POST` waits for
  it, which is what makes the dashboard current once the call returns.
- **`last_recompute` lives in memory.** A restart clears it, and the startup recompute sets
  it again.
- **A symbol case mismatch** between a venue's `base_asset` (e.g. `BTC`) and a chain's
  `asset_symbol` would leave an asset unpriced. Both are upper case today, and a test pins
  it.
