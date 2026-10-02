# 028 — Leave a wallet out of the holdings check when its chain failed

Issue: #116
Status: in progress

## Problem

The holdings check (spec 025) compares a reading only while it is current. A venue has three
rules: its last balance read succeeded, its last fill sync succeeded, and the reading is at
most 24 hours old. A wallet has the age rule alone.

So when a chain's provider fails for hours, the wallet's last reading stays in the
comparison until it is 24 hours old. Coins sent from that wallet to a venue in the meantime
are read at the venue by its next sync and are still counted in the wallet. The asset shows
as "Held exceeds history" for units that do not exist, and nothing names the wallet.

`sync_run_chains` already records, per balance run, which chains failed.

## Scope

- A wallet whose chain failed in the latest finished balance run is left out of the
  comparison, counted, and named by its chain in the block.
- The documentation states the rule and what still ages out at 24 hours.
- The regenerated OpenAPI types, and tests.

## Non-goals

- No change to a venue's rules, to the age limit, or to the Value section.
- No per-address isolation of a failure. A chain fails as a whole (#54).
- No migration. Everything is read from what the balance sync already stores.
- No reason for the failure in this block. The Value section's wallet rows already say why
  a chain could not be read, and a venue's `sync_failed` notice does not say why either.

## Design: backend

### The rule

**The latest finished balance run** is the newest `sync_runs` row, by `id`, whose status is
`success`, `partial` or `failed`. A `running` or `interrupted` run has no chain rows
(`finish_run` writes them with the final status), so it cannot say which chains failed, and
the run before it still stands.

**A wallet is left out as `chain_failed`** when both hold:

1. that run has a `sync_run_chains` row for the wallet's `chain_key` with status `failed`;
2. the wallet has no reading, or its latest reading was written by that run or an earlier
   one (`BalanceSnapshot.sync_run_id <= run.id`).

Condition 2 keeps a reading a **later** run has already written. Snapshots are committed per
chain before a run closes, so a run that is still in flight, or one that was interrupted
after it read the chain, leaves a newer reading while the latest finished run still says
`failed`. That reading is the newest there is, and the Value section calls it up to date.
The comparison is of two `INTEGER` ids in Python.

The reasons are tested in this order, and the first that applies is the answer, as for a
venue:

1. `chain_failed`, as above. A wallet with no reading whose chain failed is `chain_failed`,
   not `unread`: the failure is the fact the owner can act on.
2. `unread`: no reading.
3. `stale`: the reading is more than `MAX_READING_AGE` old.
4. Otherwise the wallet is compared.

A wallet that is left out contributes **nothing**, for whichever reason.

A chain with no row in the latest finished run (no wallet was active on it then) and a
database with no finished run are both "did not fail": the other rules decide.

### `repositories/sync_runs.py`

`SyncRunRepository.latest_finished() -> SyncRunSummary | None`: the newest run whose status
is one of the three finished ones, with its chains, or `None`. Ordered by `id`, like every
read in that module. The status filter is on a `TEXT` column of enum values, which is
neither money nor a datetime.

### `services/reconciliation.py`

- The service takes the `SyncRunRepository` and reads the latest finished run once per
  request.
- The rule is one pure, exported function, as `not_compared_reason` is for a venue.
- `WalletSources` gains:
  - `chain_failed: int`, the number of wallets left out by this rule. The four counts
    (`compared`, `stale`, `unread`, `chain_failed`) add up to the active wallets.
  - `failed_chains: tuple[FailedChain, ...]`, with `FailedChain(chain_key: str, wallets: int)`,
    sorted by `chain_key`. **Only chains with at least one wallet left out are listed**, so
    `wallets` is never zero, and the entries' `wallets` add up to `chain_failed`.
- `oldest_observed_at` stays the oldest reading among the compared wallets.
- The module docstring and `MAX_READING_AGE`'s say what the rule now is. The sentence
  "#116 will leave it out" goes.

### `api/schemas/accounting.py`

`WalletsReadResponse` gains `chain_failed: int` and
`failed_chains: list[FailedChainResponse]` (`chain_key: str`, `wallets: int`), placed before
`oldest_observed_at`. `chain_key` is a string, as `WalletResponse.chain_key` is.

```json
"wallets": {
  "compared": 1, "stale": 0, "unread": 0, "chain_failed": 2,
  "failed_chains": [{"chain_key": "bitcoin", "wallets": 2}],
  "oldest_observed_at": "2026-10-01T09:45:00Z"
}
```

`frontend/src/api/generated/schema.ts` is regenerated, and the drift job must pass.

### Documentation

`docs/accounting.md` (the holdings check, where the windows are listed) and
`docs/operations.md` (the reconciliation endpoint):

- State the wallet rule, and that the block names the chain.
- Replace "#116 will leave such a wallet out".
- State what still ages out at 24 hours with nothing naming the source until then, as
  **residuals**:
  - a wallet when the balance timer is switched off, or when no balance run finishes;
  - a venue whose credentials were removed after a read;
  - a venue when the exchange timer is switched off;
  - a balance read whose failure could not be recorded (already stated).
- State the one window the rule leaves: a chain that fails **between** two runs is not known
  to have failed until the next run finishes, so the gap is one balance interval.
- Document the two new fields of `wallets`.

## Design: frontend

### `lib/accounting.ts`

- `MissingSource` gains
  `{ kind: 'wallets_chain_failed'; chain: string; count: number }`.
- `missingSources` emits one per entry of `wallets.failed_chains`, in the endpoint's order,
  **after the venues and before `wallets_stale`**.
- `describeChainFailed(chain: string, count: number): string`, with the chain's display
  name from `chainDisplayName`:
  - one wallet: "The last balance sync could not read Bitcoin, so the coins in 1 wallet on
    it are left out of the comparison."
  - several: "The last balance sync could not read Bitcoin, so the coins in 2 wallets on it
    are left out of the comparison."

No count is summed in the frontend. `chain_failed` is not rendered: the entries carry it.

### `pages/dashboard/MissingSources.tsx`

A `role="alert"` paragraph per `wallets_chain_failed`, like the other notices. The notice
key includes the chain, because there can be one per chain.

Nothing else in the block changes. A wallet left out this way is not in
`oldest_observed_at`, which the backend already answers.

## Acceptance criteria

1. A wallet whose chain failed in the latest finished balance run, and whose latest reading
   is from that run or an earlier one, or is absent, contributes nothing to the comparison
   and is counted in `chain_failed`.
2. A wallet whose chain succeeded in the latest finished run is compared as today. So is one
   whose chain has no row in it, and every wallet when no run has finished.
3. A wallet whose latest reading was written by a run later than the latest finished one is
   not `chain_failed`.
4. A `running` or `interrupted` newest run does not hide the latest finished one.
5. `failed_chains` lists exactly the chains with a wallet left out, sorted by `chain_key`,
   and its counts add up to `chain_failed`. The four counts add up to the active wallets.
6. `oldest_observed_at` ignores wallets left out.
7. The scenario of the issue holds end to end: with the wallet's chain failed, the asset is
   not `history_short` for the coins the venue now holds.
8. No SQL aggregation, ordering or comparison on a money or datetime column is added. The
   layering contracts hold. Nothing is added to `PUBLIC_API_PATHS`.
9. The OpenAPI document and `schema.ts` carry the two fields, with no drift.
10. The block shows one alert per failed chain, with the chain's display name and the
    number of wallets, singular and plural, between the venues' notices and the stale one.
11. With `failed_chains` empty the block renders exactly what it renders today.
12. The documentation states the rule, the residuals and the remaining window, and no
    longer says #116 will do it.
13. The full gate passes with the coverage floors unchanged: backend 99.7% total, domain
    100% lines and branches as measured, frontend 100% on all four metrics.

## File ownership

| Agent | Files |
|---|---|
| `backend-dev-116` | `backend/src/portfolio/services/reconciliation.py`, `backend/src/portfolio/repositories/sync_runs.py`, `backend/src/portfolio/api/schemas/accounting.py`, `docs/accounting.md`, `docs/operations.md`, and the regenerated `frontend/src/api/generated/schema.ts` |
| `frontend-dev-116` | `frontend/src/lib/accounting.ts`, `frontend/src/pages/dashboard/MissingSources.tsx` |
| `tester-116` | every test file on both sides, `frontend/src/test/**`, and the gate. Sole gate owner |

The tech lead owns this spec and does the browser check at 1280 px and 375 px.
