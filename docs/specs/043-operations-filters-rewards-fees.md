# 043 — Operations: filters, page size, and rewards and network fees by hand

Issue: none; the owner's request of 2026-10-10
Status: in progress

## Problem

The Operations table (spec 042) holds every operation of every coin, about a thousand rows,
newest first, fifty at a time. Finding one swap or one venue's withdrawals means paging.

The consistency check also left two gaps open that no report can close:

- **A miner paid KAS straight to the wallet**, in coinbase transactions: block rewards, not
  dust, and in no exchange's report.
- **BingX's Fund Account export leaves out the network fee of a KAS withdrawal.** The coins
  arrive short of the amount listed by a fee the file never names.

A manual entry could only be a buy or a sell, so neither had anywhere to go.

## Scope / Non-goals

- **Filters**: `GET /api/exchange-operations` takes `asset`, `venue`, `since` and `until`.
  The table filters by a range of days, an asset and a venue.
- **Page size**: the table shows 25, 50, 100 or 200 rows, and says which it shows,
  "Showing 51–100 of 912".
- **Manual kinds**: `reward` and `fee` beside `buy` and `sell`.
- Not in scope:
  - A parser rule for BingX's missing fee. The file does not carry it.
  - Buenbit's BTC "costo de red", which the BTC did not pay: a few satoshis the check shows
    as held beyond what is explained. It is explained, not corrected.

## Rulings

- **R1. Filters narrow, the choices do not.** The response carries `assets` and `venues`:
  every value stored, filtered or not, so a filter that matches nothing can still be changed.
  `count` is how many the filters keep.
- **R2. A day is the browser's day.** The table turns a `From` date into `since` at local
  midnight, and a `To` date into `until` at the next local midnight. `since` is inclusive,
  `until` exclusive, and both must carry an offset.
  - A window that ends at or before it starts is refused with 422.
  - The form never sends one: a start after the end clears the end, and the reverse clears
    the start.
- **R3. A filter that keeps nothing says so beside the filters.** "No operations match these
  filters" is never the empty state "No operations yet", which means nothing was uploaded.
- **R4. A changed filter or page size starts again from the first page.**
- **R5. `reward`** entered by hand is what a miner or a staking program paid. It adds to the
  explained quantity and costs nothing (spec 042, R7).
- **R6. `fee`** is a network fee that no report lists. It is entered by hand and no parser
  produces it. It subtracts from the explained quantity and is not invested.
- **R7. A reward or a fee has no counterpart.** A `quote_currency`, a `quote_amount` or a fee
  of its own is refused with 422. A buy or a sell without both quote fields is refused too.

## Data model

Migration `0017_manual_rewards_and_fees` rebuilds `exchange_operations` with two wider
`CHECK`s:

- `kind` admits `fee`.
- A manual row may be `buy`, `sell`, `reward` or `fee`.

The downgrade deletes manual rewards and fees before restoring the older `CHECK`s.

## Acceptance criteria

1. Each filter, and any combination of them, narrows the list and its `count`. `assets` and
   `venues` stay whole.
2. A naive or inverted window, an empty asset, and an over-long venue are refused with 422.
3. The table filters by days in the browser's zone, by asset and by venue. It clears the
   filters, says when nothing matches, and pages at the chosen size with "Showing a–b of n".
4. A reward and a fee can be entered by hand without a counterpart, are listed as "Reward"
   and "Network fee", and move the explained quantity and not the invested figure.
5. The migration reverses on its own and keeps every row the older `CHECK`s accept.
6. Every gate holds.
