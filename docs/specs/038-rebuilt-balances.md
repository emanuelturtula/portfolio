# 038 — Past wallet balances, rebuilt from on-chain transaction history

Issue: none; the owner's plan of 2026-10-08 (PR 4 of 5)
Status: in progress

## Problem

The value history (spec 037) starts at each wallet's first balance snapshot, which is the day
the wallet was added here. A wallet that has held coins for three years charts three days. The
chain knows every transaction the address ever made; the current balance minus those
transactions, newest first, is the balance on every earlier day.

Prices have the same edge: Kraken keeps 720 daily candles, so BTC before 2024-10-18 has no
price to value a rebuilt balance with.

## Scope / Non-goals

- **A transaction-history read per address** on both chains: the net effect of every confirmed
  transaction, with its time.
- **`domain/balance_history.py`**, pure: walk back from the current balance, one closing
  balance per UTC day from the first transaction to today. **A complete history ends at exactly
  0 before its first transaction**; one that does not is incomplete and is not stored.
- **`reconstructed_balances`**: one row per wallet and UTC day (migration
  `0014_reconstructed_balances`), replaced whole by every successful rebuild of the wallet.
- **A daily timer** (`balance-rebuild`) and **`python -m portfolio rebuild-balances`**.
- **The value history uses rebuilt days before a wallet's first snapshot.** From the first
  snapshot on, the snapshots stay the source.
- **Coinbase daily candles** price BTC/USD before Kraken's window, back to 2015-07-20.
- Not in scope: KAS before 2024-11-19 (no source lists it; it stays a gap), pending
  transactions (a balance change happens when it confirms), any UI beyond the timer's label.

## Verified vendor facts

Read from each vendor's own documentation and measured live on 2026-10-08; recorded in
`docs/providers.md`.

**Esplora** (mempool.space primary, blockstream.info fallback).
`GET /address/:a/txs/chain[/:last_seen_txid]`: "Returns 25 transactions per page", newest
first. Measured on both hosts: paging by the last txid of the previous page reaches the oldest
transaction, the total equals `chain_stats.tx_count`, and the walk back from
`funded_txo_sum − spent_txo_sum` ends at exactly 0. **An unknown or reorged-out cursor also
answers `200 []`**, so an empty page does not prove the end. `?after_txid=` is ignored on
`/txs/chain` by both hosts. Amounts are integer satoshis; `block_time` is Unix seconds; a
coinbase input has no `prevout`.

**Kaspa REST** (api.kaspa.org). `GET /addresses/{a}/full-transactions-page` with `limit` ≤ 500,
`resolve_previous_outpoints=light`, paged with `before=<X-Next-Page-Before>` while the header
is present. Measured: no duplicates or gaps, the row count equals `/transactions-count`, and
outputs minus resolved inputs equals `/balance`. A page can exceed `limit` (the server completes
the boundary millisecond). Amounts are integer sompi; **`block_time` is epoch milliseconds**.
Every row read was `is_accepted: true`; the resolved input fields are optional in the schema.

**Coinbase Exchange** `GET /products/BTC-USD/candles?granularity=86400&start&end`, public,
10 requests/s per IP documented. Measured: at most 300 *intervals* per request (both ends
inclusive), newest first, every candle at 00:00 UTC, data from 2015-07-20 with no missing day.
**Prices are JSON numbers, not strings**, despite the documentation's general rule; the close
is index 4 (`[time, low, high, open, close, volume]`). No KAS product exists.

## Rulings

- **R1. A history is complete only if it proves it.** For each address: the transactions
  collected (unique ids) number exactly what the vendor's count says; their net effects sum to
  the current confirmed balance; and the count and balance read before the paging equal those
  read after it. Anything else is `incomplete`, with a reason, and stores nothing.
  - **Amended on 2026-10-09 for Kaspa.** Its count is the rows of its address index, which
    holds unaccepted transactions and, measured, ids its pages cannot serve. So on Kaspa the
    count is checked against every id served, accepted or not, and may run ahead of it by
    two or one in a hundred, whichever is more, but never fall behind. The sum check is
    unchanged and still exact. Esplora's `tx_count` is still matched exactly.
- **R2. A transaction's net effect** on an address is the sum of its outputs to the address
  minus the sum of its resolved inputs from it. Confirmed (Bitcoin) or accepted (Kaspa) only;
  an input whose source cannot be resolved makes the history incomplete.
- **R3. A transaction's day** is the UTC date of its block time.
- **R4. A day's rebuilt balance** is the current balance minus every effect dated after that
  day. The rebuilt history must never be negative and must be 0 before the first transaction.
- **R5. An extended-key wallet** sums the effects of every address it has derived and used; a
  transfer between two of its own addresses nets to zero on its day.
- **R6. A rebuild replaces the wallet's rows only when it is complete.** An incomplete or failed
  rebuild leaves the previous rows in place.
- **R7. The value history** takes each wallet's rebuilt days strictly before its first snapshot
  day, then its snapshots. A wallet with no rebuilt rows behaves as in spec 037.
- **R8. Old BTC prices** come from Coinbase, only for days before the earliest stored close and
  not before 2015-07-20, written as `close` with source `coinbase`. Once filled, a run asks
  for nothing.

## Data model

`reconstructed_balances(id, wallet_id → wallets ON DELETE CASCADE, day DATE, confirmed BIGINT
(BaseUnits), decimals INTEGER, rebuilt_at UtcDateTime)`, `UNIQUE (wallet_id, day)`,
`CHECK confirmed >= 0`.

## Acceptance criteria

1. Both providers page to the oldest transaction and report a complete history only per R1.
2. The domain rebuild is exact, never negative, and refuses a history that does not reach 0.
3. A rebuild stores one row per day from the first transaction to today, and an incomplete one
   stores nothing and keeps the old rows.
4. The value history extends back to the first transaction.
5. The backfill fills BTC/USD closes from Coinbase before Kraken's window, idempotently.
6. Every gate holds: backend 99.7, domain 95/90, frontend 100 %, diff coverage 90 %.

## Risks

- **Large wallets cost many calls**: ⌈N/25⌉ Esplora pages per address. The timer runs daily
  and every read goes through the shared one-request-per-second-per-host floor.
- **A transaction that confirms during the paging** fails R1's before/after check; the next
  day's run retries.
- **An unaccepted Kaspa transaction** was never observed; R2 excludes it, and if it were
  counted by `/transactions-count` the history would be reported incomplete rather than wrong.
