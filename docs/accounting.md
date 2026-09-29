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
reveals it. An opening balance (example 9) is how to repair it.

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
