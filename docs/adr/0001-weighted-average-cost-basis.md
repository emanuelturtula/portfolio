# ADR 0001 — Weighted-average cost basis

Status: accepted, 2026-09-28 (#17)

## Context

The figure the product exists to show is **how much has been invested in each asset**, and at
what average cost. The data it is computed from comes from exchange fills, and that history is
incomplete by construction:

- Bitget keeps 90 days.
- BingX's reach is uncertain beyond a year.
- Coins bought before either account existed never appear at all.

Any method will be computing from a history with a hole at the start.

## Decision

Cost basis is computed by **weighted average**, pooled per asset across every venue and wallet,
by a pure function over the event log (`portfolio.domain.accounting.replay`). The detailed
rules are in `docs/accounting.md`.

1. **Weighted average is the requested output.** "Amount invested in BTC" is what a
   weighted-average pool holds natively: the basis of what is still held. Other methods need a
   lot-matching step to arrive at the same kind of number.
2. **It degrades more gracefully under incomplete history, and one number repairs it.**
   - **What no method can see.** Suppose a buy is missing and no later sale exceeds what was
     recorded. The events hold no trace of that buy, so every method computes without it.
   - **What differs is the damage and the repair.** Under FIFO, the answer depends on *which*
     lots are missing. Every sale is matched against a specific wrong lot, and the error
     carries into what is left. Repairing it needs every missing lot with its date and
     price. Under weighted average, a missing buy shifts one average. An opening balance, a
     quantity plus a cost if known, repairs it.
   - **Where a sale does exceed the recorded holdings, this engine does not guess.** It
     empties the pool and emits a negative-inventory warning naming the asset and the moment.
     It also keeps the proceeds that no recorded cost stands behind out of realized P&L.
3. **Transfers are free.** Moving coins between a venue and a wallet relocates quantity, and
   the average is untouched. Lot methods have to carry lots across locations.
4. **Stablecoins are the unit of account.** The configured cash assets (USDT and USDC by
   default) are pinned at a unit cost of exactly 1. That makes "invested in BTC" a
   dollar-equivalent figure instead of a basis expressed in another basis.
5. **No price is invented.** No historical price source exists.
   - A crypto-to-crypto swap therefore carries the given asset's cost over to the received
     asset, and realizes nothing.
   - A fee paid in a third asset is valued at that asset's carried cost.
   - Where a cost is not known, the engine says so: through unknown-basis quantity, unmatched
     proceeds, and unattributed-fee flags. It never assumes zero.

## This is not a tax figure

**Spanish personal income tax (IRPF) does not use weighted average.**

- For homogeneous assets, the gain on a transfer is computed as if the units sold were the
  ones acquired first (first in, first out; Ley 35/2006 del IRPF, art. 37.2).
- The tax authority's binding consultations apply the same rule to cryptocurrencies.
- IRPF also treats a swap of one cryptocurrency for another as a transfer at market value
  (a *permuta*), which realizes a gain or a loss. This engine carries the cost over instead.

The realized P&L this product shows is therefore a dashboard figure. It will differ from the
figure on a tax return, and it must not be used to file one.

## Consequences

- **FIFO can be added later as a second pure function over the same events**, not as a
  migration. `replay` returns the acquisitions it saw as lots, and #19 persists them with the
  method that produced them. A FIFO pass writes its own lots under its own method into the
  same table.
- **Incomplete history shows as flags wherever replay can detect it.** That covers a sale
  beyond the recorded holdings, units of unknown cost, and a fee whose cost is unknown.
- **A gap that no sale exceeds cannot be detected from the events alone.** Only comparing
  replay's quantity with the balances actually held can show it, and #19 should consider
  doing that.
- **The designed way to fill the hole is the manual adjustments of #18**: an opening balance,
  with or without a known cost.
- **A depeg is invisible.** So is the spread of a USDC/USDT conversion, because both sides are
  pinned at 1.
- The result depends only on the events, the configuration and the engine version. Its
  fingerprint covers all three, so an unchanged fingerprint safely means an unchanged answer.

## Alternatives rejected

- **FIFO as the dashboard method** answers the question a tax computation asks, not the one
  the dashboard is asked. Under missing lots it concentrates the error on specific sales, and
  repairing that needs every missing lot (point 2 above).
- **LIFO and specific identification** also depend on which lots are missing. Specific
  identification also needs a per-lot choice from the owner that nothing records.
- **Market-value swaps and fees** would need a price on the day. Presenting today's price as
  the price on the day is the one mistake that is worse than admitting the number is unknown.
