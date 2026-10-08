# 036 — Remove exchange sync, accounting, adjustments and the holdings check

Issue: none; the owner's request of 2026-10-08
Status: in progress. Pull request "feat!: remove exchange sync, accounting, adjustments and the
holdings check"

## Problem

The application set out to answer three questions: what the portfolio is worth, what each
wallet is worth, and how much has been invested in each asset. The third pulled in most of
the code and most of the operating burden:

- two exchange integrations (Bitget, specs 012, 014 and 015; BingX, spec 017), each signed
  with the owner's key, each with its own undocumented behaviour, retention window and
  failure modes;
- a weighted-average cost-basis engine with position snapshots and history warnings (specs
  019 to 021), and the invested figure and profit and loss built on it (specs 022 and 035);
- manual adjustments to patch what the imported history could not show (specs 023 and 027);
- a holdings check comparing that history with the balances actually held (specs 025 and
  026).

The owner no longer wants the third answer. What stays is what the application is for now:
wallet management -- Bitcoin addresses and extended public keys, Kaspa addresses, read
on-chain -- their current value in USDT from cached prices, and, in later pull requests,
charts of that value over time. Everything else is code to keep correct, credentials to
keep on the host, and vendors to keep up with, for an answer nobody reads.

## Scope

Removed, backend and frontend together:

- **Exchange integrations.** The Bitget and BingX providers (`providers/exchanges/`): spot
  fill import, exchange balance reads and request signing. The `exchange-sync` timer and its
  coordinator. The `PORTFOLIO_BITGET_API_KEY`, `PORTFOLIO_BITGET_API_SECRET`,
  `PORTFOLIO_BITGET_API_PASSPHRASE`, `PORTFOLIO_BINGX_API_KEY` and
  `PORTFOLIO_BINGX_API_SECRET` settings, and `PORTFOLIO_EXCHANGE_HISTORY_START`,
  `PORTFOLIO_EXCHANGE_SYNC_ENABLED`, `PORTFOLIO_EXCHANGE_SYNC_INTERVAL_MINUTES` and
  `PORTFOLIO_EXCHANGE_SYNC_SHUTDOWN_GRACE_SECONDS`. `/api/exchanges/*`. The Exchanges page.
- **Accounting.** The weighted-average cost-basis engine, position snapshots, the invested
  figure, profit and loss, `/api/accounting/*`, and the Invested section of the dashboard.
- **Manual adjustments.** `/api/accounting/adjustments/*` and the Adjustments page.
- **The holdings check.** The reconciliation service, its dashboard section and its health
  section.
- **The data.** Migration `0012_drop_exchanges_accounting` drops the eleven tables and two
  triggers those features wrote (see *Migration and rollback*).
- **The documentation** that described them: `docs/accounting.md`, the ADR on
  weighted-average cost basis, the exchange half of `docs/providers.md`, and sections 12 to
  16 of `docs/operations.md`, which now holds one section on the removal in their place.

Changed:

- **`GET /api/portfolio/summary` is wallets only**: `{total_value, holdings, missing}`. Each
  entry of `missing` has a `kind` of `wallet_unread`, `wallet_stale`, `unpriced` or
  `stale_price`, and a `subject` naming the chain or the asset. `invested`, `pnl`, `pnl_pct`
  and `untracked` are gone.
- **`GET /api/health/detail` loses its `exchanges` and `reconciliation` sections**, and its
  scheduler list loses `exchange-sync`: the timers are `balance-sync`, `price-refresh` and
  `backup`.

## Non-goals

- **Value-over-time charts.** They are the next pull requests, built on the balance
  snapshots and prices that stay.
- **Rewriting older specs.** Specs 012 to 028 and 035 describe what was built at the time.
  They stay as records and are not edited to match.
- **Exporting the deleted history.** The pre-deployment backup holds it (see below); no
  export command is added for data the owner has said is not needed.
- **Squashing the migration history.** Revisions 0006 to 0010 stay unchanged, because the
  database on the Pi is stamped `0011_extended_keys`, and a history that no longer contained
  a stamped revision could not be upgraded from.

## Rulings

- **R1. The data is deleted, not kept dormant.** Tables nobody reads still have to be
  migrated, backed up and reasoned about. The deployment backup and the scheduled copies
  hold the rows for as long as the owner keeps them.
- **R2. The downgrade restores the schema, not the data.** It runs `upgrade()` of migrations
  0006 to 0010 in order, so the schema below `0012` is exactly what those revisions built and
  their own tests keep holding, but every table comes back empty.
- **R3. The credentials are revoked at the venues and removed from the host.** Nothing reads
  them after this change. Leftover `PORTFOLIO_BITGET_*` and `PORTFOLIO_BINGX_*` variables are
  ignored by the settings model rather than refused, so a forgotten line does not stop the
  deployment -- but a key that is no longer used is only a liability, so the operator removes
  it.
- **R4. The query-string rule stays.** The shared HTTP client logs URLs with the query
  removed, and the redaction pipeline replaces a URL's query in every record. Both were
  written for an exchange that signed in the query string; both stay, because a query string
  can carry a key or a signature for any future vendor.

## Migration and rollback

Migration `0012_drop_exchanges_accounting` drops, triggers first and children before
parents:

| Created by | Dropped |
|---|---|
| `0006_exchanges` | `exchange_accounts`, `exchange_fills` |
| `0007_exchange_sync` | `exchange_sync_windows`, `exchange_sync_runs`, `exchange_sync_run_accounts`; triggers `exchange_fills_no_update`, `exchange_fills_no_delete` |
| `0008_accounting` | `accounting_snapshots`, `accounting_positions`, `accounting_lots`, `accounting_warnings` |
| `0009_manual_adjustments` | `manual_adjustments` |
| `0010_exchange_balances` | `exchange_balances` |

No table that stays refers to any of them.

**Where the data survives.** Before the new container starts, `deploy.py` snapshots the live
database, as on every deployment. On success that snapshot becomes
`prod/backup/database.sqlite3`, which then holds every deleted row. The next successful
deployment replaces it. The scheduled copies in `/app/backups` taken before the deployment
hold the rows too, until the rotation removes them. An owner who may want the history copies
one off the host before the next deployment.

**Rollback.**

- If the deployment itself fails, the automatic rollback restores the snapshot it took (spec
  034), so nothing is lost.
- After a successful deployment, an older image does not start on a database at `0012`,
  because it does not know the revision, and a revert fails to deploy for the same reason.
  Going back with the data means restoring a copy taken before the deployment, by hand
  (`docs/operations.md`, section 17), which loses everything written since. The downgrade
  alone gives back empty tables.

**Operator steps**, in `docs/operations.md`, section 12:

1. Revoke the Bitget and BingX API keys at the venues.
2. Delete the `PORTFOLIO_BITGET_*`, `PORTFOLIO_BINGX_*` and `PORTFOLIO_EXCHANGE_*` lines from
   `~/portfolio-app/prod/secrets.env`, and recreate the container.
3. If the history may be wanted, copy the pre-deployment backup off the host before the next
   deployment.

## Acceptance criteria

1. No module under `backend/src/portfolio` imports or names `providers.exchanges`, the
   accounting engine, the adjustments service or the reconciliation service, and the import
   contracts no longer mention an exchange provider.
2. `/api/exchanges/*` and `/api/accounting/*` are not registered: the route contract test
   walks the remaining routes and none of them is under either prefix.
3. `GET /api/portfolio/summary` answers exactly `total_value`, `holdings` and `missing`, and
   every `missing[].kind` is one of `wallet_unread`, `wallet_stale`, `unpriced` and
   `stale_price`. A wallet that is unread or stale, and a price that is missing or stale, are
   named, never rendered as zero.
4. `GET /api/health/detail` has no `exchanges` or `reconciliation` key, and its schedulers are
   `balance-sync`, `price-refresh` and `backup`.
5. `Settings` has no Bitget, BingX or exchange-sync field, and a leftover variable of that
   name does not stop the application starting.
6. Migration `0012_drop_exchanges_accounting` upgrades a database at `0011_extended_keys` that
   holds rows in every dropped table, leaves every other table's rows unchanged, and leaves
   none of the eleven tables or two triggers. Its downgrade recreates the schema of `0011`
   with the eleven tables empty, and upgrading again succeeds.
7. The Exchanges and Adjustments pages, the Invested section and the holdings check are gone
   from the frontend; the dashboard renders loading, empty, error and success states for the
   wallets-only summary.
8. `docs/accounting.md` and the ADR are deleted, no kept document links to them, and
   `docs/providers.md`, `docs/operations.md`, `docs/architecture.md`, `docs/deployment.md`,
   `README.md` and `CLAUDE.md` describe the application as it now is.
9. `python scripts/check.py` passes, coverage thresholds included.

## Test plan

- The tests of every removed module are removed with it, including the documentation tests
  that read `docs/accounting.md` or the exchange sections of `docs/providers.md` and
  `docs/operations.md`.
- A migration test for `0012`: rows seeded in every dropped table, the upgrade, the
  surviving rows unchanged, then the downgrade and a second upgrade.
- The summary endpoint's tests: the response's exact keys, each `missing` kind, and that a
  removed key is absent.
- The health detail's tests: the exact sections and timer names.
- `tests/deploy/`: the assertion on the deleted Exchanges page's recreate command is removed;
  the rest still passes against the docs.

## Risks

- **The deleted history is gone once the last copy holding it rotates or is replaced.** The
  deployment backup lasts until the next successful deployment, the scheduled copies up to
  about four weeks. Mitigated by the operator step to copy one off the host first; accepted
  otherwise, since Bitget keeps 90 days of fills and the older history exists nowhere else.
- **A rollback past `0012` that runs only the old image fails to start.** That is the safe
  failure: it refuses rather than running on empty tables. The documentation says a restore
  is the way back.
- **Unrevoked keys.** The application no longer reads them, but a key left valid at a venue,
  or in `secrets.env`, is still a credential. The operator step names both places.
- **USD read as USDT** (spec 035) still applies to every value on the dashboard.
