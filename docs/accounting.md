# Accounting

How the product turns trade executions into **quantity held, cost basis, average cost and
realized P&L** per asset. The engine is `portfolio.domain.accounting.replay`. It is a pure
function: the same events and the same configuration always give the same answer, with no
database, clock or network involved. Why the method is weighted average, and why these figures
are not a tax computation, is in `docs/adr/0001-weighted-average-cost-basis.md`. The exact
contract is spec `docs/specs/019-weighted-average-cost-basis.md`.

Every example below is also a test (`backend/tests/domain/accounting/test_worked_examples.py`),
so this document cannot drift from the engine without a build failing.

## The model in one page

**Cash is the unit of account.** The configured cash assets (by default `USDT` and `USDC`) are
pinned at a unit cost of exactly 1. Every cost, basis, proceeds and P&L figure is in these
"cash units", which in practice means US dollars. Cash is never inventoried: it has no position
and never runs short.

**Each other asset has one pool**, across every venue and wallet. A pool holds:

| Field | Meaning |
|---|---|
| `quantity` | What the events say is held. |
| `cost_basis` | What the known-cost part of that quantity cost. |
| `average_cost` | `cost_basis ÷ known quantity`, rounded for display. Never fed back into the basis. Absent when no known-cost quantity is held, and when the quotient would be 10²⁰ or more per unit, since it is a display figure and not a reason for replay to fail. |
| `unknown_basis_quantity` | Units held whose cost nobody recorded, such as an opening balance entered without a cost. They are kept out of the average rather than valued at zero, because zero would report a fictitious profit on the next sale. |
| `realized_pnl` | Proceeds minus basis, over every sale of known-cost units for cash. |
| `unmatched_proceeds` | Proceeds from units whose cost is unknown: unknown-basis units, or units sold beyond what the history holds. Kept out of realized P&L for the same reason. |

**The events are replayed in one total order**: time, then source, then id, then kind (trade,
adjustment or transfer) for the rare tie between kinds. Same-millisecond fills therefore
always come out the same way, whatever order the database returned them in. A fill read twice
counts once.

### A trade, whatever its pair

A trade gives one asset and receives another. The fee folds in according to where it is paid:

| Fee paid in | Effect |
|---|---|
| the asset received | Less is received. The cost is unchanged, so the unit cost rises. |
| the asset given | More is given. |
| a third asset | That asset's pool pays it, at its carried cost. |

Then one of four things happens:

| Gives | Receives | Called | Effect |
|---|---|---|---|
| cash | an asset | a buy | The asset's basis rises by the cash given, fees included. |
| an asset | cash | a sale | The asset's pool gives up the proportional basis. Realized P&L is the proceeds, net of fees, minus that basis. |
| an asset | another asset | a swap | The given asset's basis, plus the fee, moves to the received asset. Nothing is realized, because no price exists to realize it at. If some of the units given had unknown cost, the received units split in the same proportion, and so does the fee: the unknown share of the fee goes to `unallocated_costs`, not onto the few units of known cost. |
| cash | cash | a conversion | Nothing changes, since both sides are pinned at 1. A fee is recorded in `unallocated_costs`. |

**When a short history shows, the engine says so rather than guessing:**

- **A sale larger than the pool.** It takes everything the pool holds and emits a
  `NegativeInventory` warning naming the asset, the moment and the shortfall. The pool never
  goes below zero.
- **A fee in a third asset whose cost is unknown.** It emits an `UnattributedFee` warning,
  and the trade's non-cash asset is flagged. On a buy or a swap, that asset's basis leaves
  the fee out. On a sale, its proceeds are overstated by the fee. A conversion has no such
  asset, so only the warning remains.

**A short history does not always show.** Suppose a buy is older than the venue's retention
and the coins are still held. Every later sale then fits inside the recorded pool, so nothing
warns, and the average and realized P&L leave that buy out. No method can see such a gap in
the events alone. Comparing the replayed quantity with the balances actually held is what
reveals it: that comparison is the holdings check, described under "Checking the history
against the balances held" below. An opening balance (example 9) is how to repair it.

**Rounding happens in one place**, a division rounded once, half to even, to 18 decimals. The
other side of every split is computed by subtraction, so nothing leaks. When a pool is emptied,
it gives up all of its remaining basis. The rounding residue of every earlier partial sale
therefore lands in the realized P&L of the sale that empties it.

## Worked examples

Times are UTC on 2026-01-01. Every trade is on one venue. Amounts are exact unless shown to
18 decimals.

### 1. Two buys and a partial sale

| Time | Event |
|---|---|
| 10:00 | Buy 1 BTC for 30,000 USDT |
| 11:00 | Buy 1 BTC for 40,000 USDT |
| 12:00 | Sell 0.5 BTC for 25,000 USDT |

After the buys, 2 BTC cost 70,000, an average of 35,000. The sale gives up
70,000 × 0.5 ÷ 2 = 17,500 of basis, against 25,000 of proceeds.

| BTC | |
|---|---|
| quantity | 1.5 |
| cost basis | 52,500 |
| average cost | 35,000 |
| realized P&L | 7,500 |

The sale leaves the average unchanged. That is what weighted average means.

### 2. A fee in the quote, and a fee in the base

| Time | Event |
|---|---|
| 10:00 | Buy 1 BTC for 30,000 USDT, fee 30 USDT |
| 11:00 | Buy 1,000 KAS for 100 USDT, fee 1 KAS |
| 12:00 | Sell 1 BTC for 33,000 USDT, fee 33 USDT |

- **The BTC buy.** The fee is in the asset given, so 30,030 USDT goes out and the basis is
  30,030.
- **The KAS buy.** The fee is in the asset received, so 999 KAS arrive for a basis of 100.
  The unit cost rises to 100 ÷ 999.
- **The BTC sale.** The fee is in the asset received, so the proceeds are 32,967. The pool is
  emptied, giving up all 30,030.

| | BTC | KAS |
|---|---|---|
| quantity | 0 | 999 |
| cost basis | 0 | 100 |
| average cost | — | 0.100100100100100100 |
| realized P&L | 2,937 | 0 |

### 3. A fee in a third asset, carried at cost

| Time | Event |
|---|---|
| 10:00 | Buy 10 BGB for 10 USDT |
| 11:00 | Buy 1 BTC for 30,000 USDT, fee 2 BGB |

The fee is neither BTC nor USDT, so the BGB pool pays it:

- 2 of its 10 units leave, taking 10 × 2 ÷ 10 = 2 of basis with them.
- That 2 is the fee's value, and it is added to the BTC basis.
- No price was needed, and BGB realizes nothing. Its cost moved into the thing it was spent
  on.

| | BTC | BGB |
|---|---|---|
| quantity | 1 | 8 |
| cost basis | 30,002 | 8 |
| average cost | 30,002 | 1 |
| realized P&L | 0 | 0 |

### 4. A fee in a third asset nobody bought

| Time | Event |
|---|---|
| 11:00 | Buy 1 BTC for 30,000 USDT, fee 2 BGB |

No BGB was ever recorded, so the BGB pool is empty. Two warnings follow, each about a
different fact:

- **`NegativeInventory` (BGB, 11:00, shortfall 2).** BGB's history is incomplete, and the
  position carries `HISTORY_INCOMPLETE`.
- **`UnattributedFee` (BGB, 2, charged to BTC).** BTC's basis leaves out a fee whose cost is
  unknown, and the position carries `UNATTRIBUTED_FEE`.

| | BTC | BGB |
|---|---|---|
| quantity | 1 | 0 |
| cost basis | 30,000 | 0 |
| flags | `UNATTRIBUTED_FEE` | `HISTORY_INCOMPLETE` |

### 5. Selling more than the history holds

| Time | Event |
|---|---|
| 10:00 | Buy 1 BTC for 30,000 USDT |
| 12:00 | Sell 1.5 BTC for 60,000 USDT |

The pool holds 1 BTC, so the sale takes all of it, and the other 0.5 is a shortfall. The
proceeds split in the same proportion as the quantity:

- 60,000 × 1 ÷ 1.5 = 40,000 is matched against the 30,000 basis.
- The other 20,000 has no known cost, and it is not counted as profit.
- `NegativeInventory` (BTC, 12:00, shortfall 0.5) is emitted, and the position carries
  `HISTORY_INCOMPLETE`.

| BTC | |
|---|---|
| quantity | 0 |
| cost basis | 0 |
| realized P&L | 10,000 |
| unmatched proceeds | 20,000 |

### 6. Emptying a pool takes the rounding residue with it

| Time | Event |
|---|---|
| 10:00 | Buy 3 KAS for 1 USDT |
| 11:00 | Sell 1 KAS for 0.5 USDT |
| 12:00 | Sell 1 KAS for 0.5 USDT |
| 13:00 | Sell 1 KAS for 0.5 USDT |

| Sale | Basis given up | Basis left |
|---|---|---|
| 11:00 | 1 × 1 ÷ 3 = 0.333333333333333333 | 0.666666666666666667 |
| 12:00 | 0.666666666666666667 × 1 ÷ 2 = 0.3333333333333333335, a tie that rounds to even: 0.333333333333333334 | 0.333333333333333333 |
| 13:00 | everything left: 0.333333333333333333 | 0 |

The three amounts given up add up to exactly 1. Realized P&L is
0.166666666666666667 + 0.166666666666666666 + 0.166666666666666667 = 0.5 exactly, which is
1.5 of proceeds minus 1 of cost. The basis is exactly 0 at quantity 0.

### 7. A crypto-to-crypto swap carries the cost over

| Time | Event |
|---|---|
| 10:00 | Buy 1 BTC for 30,000 USDT |
| 11:00 | Buy 100,000 KAS for 0.5 BTC (pair KAS/BTC) |

Half the BTC pool leaves, taking 15,000 of basis. That 15,000 becomes the KAS cost, and
nothing is realized.

| | BTC | KAS |
|---|---|---|
| quantity | 0.5 | 100,000 |
| cost basis | 15,000 | 15,000 |
| average cost | 30,000 | 0.15 |
| realized P&L | 0 | 0 |

A tax return would treat 11:00 as a sale of BTC at market value. This dashboard does not. See
the ADR.

### 8. An opening balance without a cost

| Time | Event |
|---|---|
| 09:00 | Adjustment: 2 BTC held, cost unknown |
| 10:00 | Buy 1 BTC for 30,000 USDT |
| 12:00 | Sell 1.5 BTC for 60,000 USDT |

**Before the sale.** The pool holds 3 BTC, of which 2 have unknown cost. The average covers
only the known BTC, so it is 30,000 and not 10,000. The position carries `UNKNOWN_BASIS`.

**The sale** takes a proportional share of each part:

- Known: 1.5 × 1 ÷ 3 = 0.5 BTC, with 15,000 of basis.
- Unknown: 1 BTC.

The proceeds split the same way: 60,000 × 0.5 ÷ 1.5 = 20,000 is matched against the 15,000
basis, and the other 40,000 is unmatched.

| BTC | |
|---|---|
| quantity | 1.5 |
| unknown-basis quantity | 1 |
| cost basis | 15,000 |
| average cost | 30,000 |
| realized P&L | 5,000 |
| unmatched proceeds | 40,000 |
| flags | `UNKNOWN_BASIS` |

### 9. An opening balance with a cost resolves the shortfall

The same events as example 5, with one adjustment before them:

| Time | Event |
|---|---|
| 09:00 | Adjustment: 0.5 BTC held, unit cost 25,000 |
| 10:00 | Buy 1 BTC for 30,000 USDT |
| 12:00 | Sell 1.5 BTC for 60,000 USDT |

The pool holds 1.5 BTC at a cost of 12,500 + 30,000 = 42,500, and the sale empties it
exactly.

| BTC | |
|---|---|
| quantity | 0 |
| cost basis | 0 |
| realized P&L | 17,500 |

There is no warning and no flag.

### 10. A transfer changes nothing

Add "11:00, transfer 0.5 BTC from the venue to a wallet" to example 1. Every figure and every
warning is unchanged. Weighted average pools an asset across locations, so moving it is not an
accounting event. Only the input fingerprint and the event count change, because the input did.

### 11. A stablecoin conversion

| Time | Event |
|---|---|
| 10:00 | Buy 100 USDC for 100 USDT, fee 0.1 USDT |

No position exists, since both sides are cash. The fee has no asset to attach to, so it goes to
`unallocated_costs`, which is 0.1.

## What the result carries

| Field | Contents |
|---|---|
| `positions` | One per non-cash asset any trade or adjustment touched, sorted by symbol. Each has the pool fields above and its flags. |
| `warnings` | `NegativeInventory` and `UnattributedFee`, in event order. They are returned, never logged. |
| `lots` | One per acquisition, with its cost as this method attributed it. #19 persists them, so that a FIFO pass can later sit beside this one. |
| `unallocated_costs` | Known costs that belong to no position: conversion fees, value given in a swap whose received quantity has no known-cost part, and the share of a swap's fee that belongs to received units of unknown cost. |
| `input_fingerprint` | A SHA-256 over the method, the engine version, the cash assets and every event. Equal fingerprints mean an equal answer, so #19 can skip a recompute. |

Taken together, the figures always reconcile. Current basis, minus realized P&L, minus
unmatched proceeds, plus unallocated costs, equals the net cash the trades put in plus the known
cost of the adjustments of non-cash assets. For a conversion, the net cash counts only its fee,
since both sides are pinned at 1. The property tests hold this exactly for random event
sequences, with no tolerance.

## Where the figures are stored and served

The engine computes; #19 keeps and serves the result (spec
`docs/specs/021-position-snapshots.md`).

- **Stored as one snapshot per owner and method.** `accounting_snapshots` holds the header:
  the method, the engine version, the input fingerprint, the event count, the unallocated costs
  and `computed_at`. `accounting_positions`, `accounting_lots` and `accounting_warnings` hold
  the result's three lists, every amount at eighteen places as the engine returns it. It is
  derived data: deleting it loses nothing that a recompute cannot rebuild from the fills and
  the manual adjustments.
- **Recomputed at startup, after every exchange sync that stored a fill, and after every
  change to a manual adjustment**, and skipped when the fingerprint is unchanged. A new engine
  version changes every fingerprint, so an upgrade always recomputes. A stored fill or
  adjustment that cannot become an event fails the recompute and leaves the previous snapshot
  in place, rather than being skipped. `docs/operations.md`, section 15, covers the log lines
  and what a failure means.
- **Served by `GET /api/accounting/positions`**, valued at the cached **USD** price of each
  asset. The unit of account is USDT/USDC pinned at 1, so USD is the currency the figures are
  already in.
  - `market_value` is the price times every unit held.
  - `unrealized_pnl` and `unrealized_return_pct` cover only the known-cost part, the only part
    with a cost to compare against.
  - `realized_pnl` is reported beside them, never added in.
  - The totals leave out any position with unknown-cost units or no price, and name it.
  - The arithmetic is `portfolio.domain.accounting.value_position`, pure and exact like the
    engine.

## Checking the history against the balances held

The engine can only report a gap it can see in the events. A buy older than a venue keeps,
whose coins are still held, leaves none: every later sale fits inside the recorded pool, and
nothing warns. The **holdings check** finds that gap from the other side, by comparing what
the replay says is held with what is actually held (spec
`docs/specs/025-holdings-reconciliation.md`). The comparison is
`portfolio.domain.accounting.reconcile`, pure and exact like the engine.

### What is compared

For every non-cash asset, two quantities:

| Side | What it is |
|---|---|
| `history_quantity` | The position's `quantity` in the stored snapshot: what the fills and the manual adjustments add up to. |
| `held_quantity` | `wallet_quantity + exchange_quantity`: what the current readings say is held. |

- **`wallet_quantity`** is the latest confirmed balance of every active wallet whose chain's
  native asset this is, summed over the wallets whose reading is current.
- **`exchange_quantity`** is the asset's total in each venue's **spot** account, summed over
  the venues whose reading is current. A venue's balances are read after each successful fill
  sync of that venue, and only the last reading is kept.
- **`difference`** is `held_quantity - history_quantity`. Positive means more is held than the
  history accounts for.

"Current" is defined below. A reading that is not current adds nothing to either sum.

The assets are every asset that appears on either side. The cash assets, USDT and USDC, are
left out: they are the unit of account, the engine keeps no quantity for them, and a venue's
USDT balance has nothing to be compared with. An asset whose two sides are both zero is left
out too.

### The held side is a lower bound

The balances read are never everything the owner holds. A wallet that is not registered here,
an Earn, futures, margin or funding account at a venue, a venue whose key was refused, a
wallet no sync has read yet: all of them hold coins the check does not see. Every other rule
follows from that one, and it is why the two directions of a difference do not mean the same
thing.

| `status` | When | What it means |
|---|---|---|
| `match` | The difference is within the tolerance. | The history accounts for what was read. |
| `history_short` | More is held than the history accounts for. | **A finding, and a prompt to look.** The owner holds at least what was read, and the history accounts for less. The usual cause is acquisitions missing from the history: a buy older than a venue keeps, or coins acquired elsewhere. The average cost and the profit of that asset then leave the missing units out. Coins in transit between two readings can produce it too, for a while; see below. |
| `history_over` | The history accounts for more than was read as held. | **Not a finding.** Coins held where this application does not read, a withdrawal, a network fee, a trading fee the import did not record, and a sale or a conversion the import did not see all produce it. The check cannot tell them apart, so it shows the difference and draws no conclusion from it. |

**A source that contributes nothing cannot produce a false `history_short`.** Leaving a
wallet or a venue out only lowers the held side. It can hide a real one, so the check names
every source it left out.

**A reading that is out of date could, so it is left out.** Coins moved after a reading was
taken are counted where they were, by the old reading, and where they are now, by a newer
one. A venue whose balance read has been failing for a week, summed at what it held a week
ago, beside the wallet those coins were withdrawn to since, would show as more held than the
history accounts for, for units that do not exist. So a reading is compared only while it is
**current**:

- **A venue's reading is current** when its last balance read succeeded, its last fill sync
  succeeded, and the reading is at most 24 hours old (`max_reading_age_hours`).
- **A wallet's reading is current** when it is at most 24 hours old.

A venue that is left out says why, in `not_compared_reason`. The first of these that applies:

| `not_compared_reason` | Means |
|---|---|
| `read_failed` | The last balance read failed; `balances_error` says how. Whatever was read before it is not used. |
| `never_read` | No balance read has succeeded yet. |
| `sync_failed` | The venue's fill sync is not `ok`. Balances are read only after a successful fill sync, so nothing is refreshing the reading. |
| `out_of_date` | The reading is more than 24 hours old and none of the above explains it: for example the exchange timer is off, the application was not running, or the venue's credentials were removed after the read. |

Wallets are counted: `compared`, `stale` (a reading more than 24 hours old) and `unread` (no
reading at all). Only the compared ones are in the sum.

**What remains is coins moved between two current readings.** A venue and a wallet are read
by two different syncs. Coins withdrawn from the venue after its reading and seen by the
wallet's are counted twice; coins that left one and have not reached the other are not counted
at all. Either lasts until both sources have been read again. How far apart two current
readings can be depends on whether both sources are still being read:

- **Minutes, while both syncs are running.** Each runs every fifteen minutes by default.
- **Up to 24 hours, when a source has stopped being read without a recorded failure.** Its
  last reading stays current until it reaches the age limit, and nothing names the source
  until then. That is a wallet whose chain is failing on every balance run (#116 will leave
  such a wallet out), a venue whose credentials were removed or whose exchange timer is off,
  and a venue whose balance read failed when the failure could not be recorded either: its
  previous reading then stays in the comparison until a read succeeds or it is 24 hours old.

The check shows the age of each reading, which is how to tell the two apart. No rule removes
the gap, which is why a `history_short` is a prompt to look and not a verdict.

**The history can be behind the balances too.** An exchange sync stores each venue's fills and
balances as it goes, and recomputes the snapshot once the whole run has finished. Between
those commits and the recompute, an asset bought in that sync can show as `history_short`;
with two venues that window spans the second venue's sync. If the recompute fails, the
previous snapshot stays, and every asset bought since shows as missing from the history. The
endpoint serves `last_recompute` so that this is visible, and the dashboard shows no
comparison while its outcome is `failed`.

### The one percent tolerance

A difference is a `match` when it is at most `tolerance_pct` percent of the larger side:

```
|difference| x 100 <= tolerance_pct x max(history_quantity, held_quantity)
```

The tolerance is **one percent**, for a reason on each side of it:

- **It must not be smaller.** The one-time historical imports hold zero fees, and a venue's
  fee is about a tenth of a percent of a trade. A complete history therefore sits a fraction
  of a percent above the balances, and without a tolerance every asset would be reported.
- **It need not be smaller.** A missing buy smaller than one percent of a holding moves the
  average cost by about as much, unless its price was far from the average. The shift is the
  missing share of the holding times how far that price sat from the average, relative to the
  average: one percent of a holding bought at twice the average moves it by one percent, and
  bought at ten times the average, by nine.

It is relative to the **quantity**, not to its value, because only two assets have a price. So
it cannot tell dust from a real gap: a remainder too small to trade, in an asset the history
no longer holds, is a difference of one hundred percent and is shown as it is.

The comparison is exact. Both sides of the inequality are exact products, with no division and
no rounding, so an asset exactly at the boundary is a `match` and one unit of the last decimal
place past it is not.

| History | Wallets | Exchanges | Held | Difference | Status |
|---|---|---|---|---|---|
| 0.5 | 0.7 | 0.3 | 1 | +0.5 | `history_short` |
| 2 | 0 | 1.99 | 1.99 | -0.01 | `match`: 0.01 x 100 = 1, at most 1 x 2 |
| 100 | 0 | 99 | 99 | -1 | `match`: 1 x 100 = 100, exactly 1 x 100 |
| 100 | 0 | 98.9 | 98.9 | -1.1 | `history_over` |
| 100 | 101 | 0.1 | 101.1 | +1.1 | `history_short`: 110 is more than 1 x 101.1 |
| 0 | 0.001 | 0 | 0.001 | +0.001 | `history_short`: no tolerance is left when one side is zero |

### Resolving a `history_short`

**First rule out coins moved between two readings.** Look at the age of each reading the
check shows, and at whether the asset was moved between a venue and a wallet, or traded,
since the older of them. Run both syncs again and look once more: a difference that came from
two readings taken at different times lasts until both sources have been read again, and is
gone once the transfer has arrived and both readings are fresh. A difference that is still
there is the one to act on.

Such a `history_short` says units are held that no event acquired. An **opening balance**
records them: a manual adjustment of the asset, for the `difference`, dated before the first
fill of that asset, with the cost if the owner knows it and without one if not. The next
section explains adjustments and works an example.

Take the first row above. The history accounts for 0.5 BTC and 1 BTC is held, so 0.5 BTC is
missing: it was bought before the venue's history begins. An adjustment of 0.5 BTC raises the
history to 1, the difference becomes zero, and the asset is a `match`. With its cost, the
average cost and the profit now include those units. Without it, they are held at unknown
cost: the asset carries `unknown_basis`, and the figures say they do not know rather than
leaving the units out in silence.

### Where it is served

`GET /api/accounting/reconciliation` returns the comparison and the state of every source:

```json
{
  "computed_at": "2026-10-01T10:00:00Z",
  "tolerance_pct": "1",
  "assets": [
    {
      "asset": "BTC",
      "history_quantity": "0.500000000000000000",
      "wallet_quantity": "0.700000000000000000",
      "exchange_quantity": "0.300000000000000000",
      "held_quantity": "1.000000000000000000",
      "difference": "0.500000000000000000",
      "status": "history_short"
    }
  ],
  "max_reading_age_hours": 24,
  "last_recompute": {"at": "2026-10-01T10:00:00Z", "outcome": "written", "error": null},
  "exchanges": [
    {"exchange_key": "bitget", "balances_read_at": "2026-10-01T09:59:00Z",
     "balances_error": null, "not_compared_reason": null}
  ],
  "wallets": {"compared": 2, "stale": 0, "unread": 0, "oldest_observed_at": "2026-10-01T09:45:00Z"}
}
```

- Every quantity is a JSON string at eighteen places, like every other amount.
- `computed_at` is the snapshot's. Before the first snapshot it is `null` and `assets` is
  empty: nothing has been compared, which is not the same as every balance being unaccounted
  for. `exchanges` and `wallets` are answered either way.
- `last_recompute` is the last recompute attempt since the process started, as
  `GET /api/accounting/positions` serves it. It is `null` after a restart until the startup
  recompute ends, and the stored snapshot is compared meanwhile. An outcome of `failed` means
  the snapshot compared is older than the balances beside it.
- `exchanges` lists every exchange account, compared or not. `balances_read_at` is when its
  balances were last read successfully. `balances_error` is the kind the last attempt failed
  with, in the vocabulary of a failed fill sync (`docs/operations.md`, section 13). A failed
  read keeps the rows of the last good reading in the database, and the comparison does not
  use them: `not_compared_reason` is then `read_failed`.
- `not_compared_reason` is `null` for a venue whose balances are in the comparison.
- `wallets.compared`, `wallets.stale` and `wallets.unread` add up to the active wallets.
  `oldest_observed_at` is the oldest reading among the compared ones, `null` when none is.

The request reads what is stored. It asks no chain and no venue, and it recomputes nothing.
The age of a reading is measured when the request is served.

## Recording what the history does not show

The venues keep only a window of history (Bitget: 90 days). Coins bought before that window
are held, but no event says so. Selling them then oversells the pool, and the engine reports
it: a `negative_inventory` warning, and the `history_incomplete` flag on the asset. While they
are still held nothing warns, and the holdings check above is what shows them, as a
`history_short`. A **manual adjustment** is how the owner fixes either (spec
`docs/specs/023-manual-adjustments.md`).

### What an adjustment is

An adjustment is an **inflow**: `quantity` of `asset` acquired at `occurred_at`, at a
`unit_cost` in USD or at an unknown cost. It is the engine's `Adjustment` event, stored in
`manual_adjustments` and replayed with the fills in one list. It covers an opening balance and
any acquisition off the exchanges, such as a purchase from a person. It records no outflow: a
gift sent or coins lost are not adjustments.

Every adjustment carries a note, in the owner's words, saying why it exists. The note is never
logged.

The owner enters adjustments through the authenticated API under
`/api/accounting/adjustments`. While signed in, `/api/docs` works for listing, creating and
replacing them. It cannot delete one: every write must carry `Content-Type: application/json`,
and Swagger UI sends no content type for a request without a body, so the delete gets a 403.
To delete one, run this in the browser console on a page of the signed-in application, with
the adjustment's id in place of `<id>`:

```js
await fetch('/api/accounting/adjustments/<id>', {method: 'DELETE', headers: {'Content-Type': 'application/json'}})
```

The browser adds the `Origin` header and the session cookie itself, and a `204` means it is
gone. Creating, replacing or deleting an adjustment recomputes the positions before the
response returns.

### Dating an opening balance

An adjustment takes its place among the fills by `occurred_at`. At the same instant as a fill,
it replays **after** the fill, because its source, `manual`, sorts after every venue's. So date
an opening balance **before the first sale it has to cover**, not at the moment of that sale.
The date of the first imported fill, minus a day, is a safe choice.

Two adjustments at the same instant replay in the order they were entered. An adjustment's id
is its identity in the replay, and ids are never reused.

### Unknown cost is not zero

- **Without a `unit_cost`** (omitted, or `null`), the quantity counts toward the position but
  not toward its cost. While those units are held, the asset shows the `unknown_basis` flag and
  the quantity in `unknown_basis_quantity`, and the totals leave the asset out and name it. A
  later sale of those units realizes nothing, and its proceeds go to `unmatched_proceeds`
  (example 8).
- **With a `unit_cost` of zero**, the cost is known to be nothing, as for an airdrop recorded
  that way. A later sale reports its whole proceeds as profit.

Enter zero only when the coins really cost nothing. When the cost is not known, leave it out:
the position then says that it does not know, rather than reporting a profit that did not
happen.

The API refuses what the engine could not replay, and stores nothing: a quantity that is not
above zero, a negative cost, more than 18 decimal places, a cost times a quantity too large to
represent, a date later than now or without a time zone, and a symbol that is not the venue's
own spelling (`BTC`, never `btc`). The cash assets USDC and USDT are refused too: they are the
unit of account, and an adjustment of one changes nothing. The error names the field and the
rule, never the value.

### Example: resolving a `negative_inventory` warning

The imported history is example 5: a buy of 1 BTC at 10:00 for 30,000 USDT, and a sale of
1.5 BTC at 12:00 for 60,000 USDT. Nothing records the other 0.5 BTC, which was bought before
the history begins, at 25,000. The positions say so:

```json
"warnings": [{"kind": "negative_inventory", "asset": "BTC", "quantity": "0.500000000000000000", ...}]
```

The BTC position carries `history_incomplete`, 10,000 of realized P&L and 20,000 of unmatched
proceeds. The owner records the opening balance, dated before the buy:

```bash
curl -s -b "$COOKIE" -H "Origin: https://<host>" -H "Content-Type: application/json" \
  -X POST https://<host>/api/accounting/adjustments \
  -d '{"asset": "BTC", "quantity": "0.5", "unit_cost": "25000",
       "occurred_at": "2026-01-01T09:00:00Z",
       "note": "Bought before the Bitget history begins"}'
```

The response arrives after the recompute. The events are now example 9's. The warning and the
flag are gone, the sale is matched against a basis of 42,500, and the realized P&L is 17,500
with no unmatched proceeds. Recorded without a `unit_cost`, the same adjustment would remove
the warning and the flag too. The realized P&L would stay at 10,000, and the 20,000 the
unknown-cost half brought in would stay in `unmatched_proceeds` rather than become profit.
While units of unknown cost are still held, the asset carries `unknown_basis`. This sale
emptied the pool, so none remain here.
