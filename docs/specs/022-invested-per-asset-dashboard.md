# 022 — Invested per asset and unrealized P&L on the dashboard

Issue: #20
Status: in progress

## Problem

`GET /api/accounting/positions` (spec 021) serves what each asset cost, what it is worth and
what it has made. Nothing shows it. The dashboard shows what the wallets hold and what that
is worth, and nothing about what went in.

## Scope

- A new **Invested** section on the dashboard (`/`), below the value section. It has its own
  loading, error and empty states, independent of the balances query.
- A per-asset table: quantity, average cost, total invested, price with its age, market value,
  unrealized P&L and return percentage.
- A portfolio summary: invested against value, unrealized P&L with its return, realized P&L,
  and the positions left out of the totals with the reason.
- The three data-quality flags, marked on their rows and explained.
- An empty state that tells "no trades imported yet" apart from "the sync failed".
- Readable at 1280 px and on a phone.

## Non-goals

- **Anything on the backend.** The endpoint is spec 021's, and the page sums nothing: every
  total it shows is in `totals`.
- **A per-row realized P&L column, lots, and FIFO.** Realized P&L appears once, in the
  summary.
- **Currency choice.** The endpoint is USD-only. The page shows `quote_currency`, which may
  differ from the value section's currency.
- **Manual adjustments (#18) and the exchanges transactions view (#93).**
- **Reconciling quantities with the balances held (#104).**

## Design

### Where it lives

`DashboardPage` today returns early, as a whole page, for the balances query's loading, error
and "no wallets" states. An owner with trades and no wallets would never see the invested
section. So:

- The current body of `DashboardPage` moves, **unchanged in behaviour**, to
  `pages/dashboard/ValueSection.tsx`. Its early returns become that section's own states.
- `DashboardPage` renders `<ValueSection />` and then `<InvestedSection />`.
- `InvestedSection` lives in `pages/dashboard/InvestedSection.tsx`, with its pieces beside it:
  `InvestedSummary.tsx`, `PositionTable.tsx`, and optionally `HistoryWarnings.tsx`.

### Queries

- `api/accounting.ts` exports `usePositions()`:
  - query key `['accounting', 'positions']`;
  - `GET /api/accounting/positions`;
  - polls every 60 s, since the endpoint is a database read that reaches no vendor.
  - Types come only from the generated schema: `PositionsResponse`,
    `AccountingPositionResponse`, `AccountingTotalsResponse`, `ExclusionResponse`,
    `AccountingWarningResponse`, `LastRecomputeResponse`.
- `InvestedSection` also reads `useExchanges(false)`. It reuses the Exchanges page's query and
  cache. Only the empty state and the sync-failure notice consult it.
- `useSyncExchanges` invalidates `['accounting']` as well as `['exchanges']` when it settles.
  A sync that stored a fill has recomputed before it answers (spec 021).

### States, in order

1. **Positions pending**: `<Skeleton label="Loading invested per asset…" />`.
2. **Positions failed with no data** (`isLoadingError`): `ErrorState`, titled
   "Could not load invested per asset", with retry.
3. **Positions refetch failed with data** (`isRefetchError`): a notice that the figures could
   not be refreshed, and the last data stays on screen. This is the pattern `DashboardPage`
   already uses.
4. **`positions` is empty**: the empty state below.
5. **Otherwise**: the section.

### Empty state (`positions.length === 0`)

While the exchanges query is pending, the section shows the skeleton. It cannot yet tell the
cases apart.

The first case that holds wins. The decision is a pure function in `lib/accounting.ts`, for
example `describeEmptyPositions(positions, exchanges)`, returning a discriminated union.

| # | Condition | Title | Says |
|---|---|---|---|
| 1 | `last_recompute?.outcome === 'failed'` | "Positions could not be computed" | when (`RelativeTime` of `last_recompute.at`), the error class name, and that it is retried after the next exchange sync. Links to `/exchanges`. |
| 2 | the exchanges query succeeded, and some venue has `status` `error` or `auth_failed` | "The exchange sync failed" | which venue or venues, by display name from `EXCHANGES`, and that no trades have been imported. Links to `/exchanges`. |
| 3 | the exchanges query succeeded, and `fills_stored` is 0 on every venue | "No trades imported yet" | with no configured venue, that an exchange must be configured on the server; otherwise, that syncing the exchanges imports trades. Links to `/exchanges`. |
| 4 | `computed_at === null` | "Positions have not been computed yet" | that they are computed at startup and after each exchange sync that stores a trade. |
| 5 | otherwise | "No positions" | that every trade imported so far is between stablecoins, which are held at cost. |

If the exchanges query failed, rows 2 and 3 are skipped. A notice then says exchange status is
unavailable, using `describeApiError`.

This table is how the page meets #20's criterion "no trades imported yet" versus "the sync
failed": row 3 versus row 2, and row 1 when the failure was ours.

### The section, when there are positions

- **A status line**:
  "Weighted average cost in {quote_currency}, computed {RelativeTime computed_at}. Not a tax
  figure." ADR 0001 is the reason for the last sentence.
- **Notices**, each `role="alert"` like the dashboard's others:
  - `last_recompute.outcome === 'failed'`: the last recompute failed, when, and the error class
    name, so these figures are the ones computed at `computed_at`.
  - A venue with `status` `error` or `auth_failed`: its last sync failed, so these figures may
    miss its latest trades. Links to `/exchanges`.
- **Held and closed positions.** A position is **held** when `quantity` is not zero, and
  **closed** otherwise. Compare with decimal.js in `lib/`, never with string equality: the wire
  sends `"0.000000000000000000"`.
  - The table lists held positions only, in the endpoint's order.
  - Closed positions are summarised in one line:
    "{n} fully sold asset(s) ({symbols}) are not listed; their realized P&L is in the total."
  - When nothing is held, the table is replaced by "Nothing is held right now."

### Summary (`InvestedSummary`)

A `<dl>` with the following entries:

| Term | Value |
|---|---|
| Invested | `totals.total_invested` |
| Market value | `totals.market_value` |
| Unrealized P&L | `totals.unrealized_pnl`, signed, and `totals.unrealized_return_pct`, signed with `%`, or "—" when null |
| Realized P&L | `totals.realized_pnl`, signed |

It then shows the following lines:

- **When `totals.excluded` is not empty**: "Left out of these totals:", followed by each asset
  and its reason sentence.
  - `unknown_basis`: part of the holding has no known cost, so its value and its cost describe
    different quantities.
  - `unpriced`: there is no price for it, so it has a cost and no value.

  Realized P&L covers every position. The sentence says so.
- **When every held position is excluded**: Invested, Market value and Unrealized P&L show "—"
  instead of an empty sum's `0.00`. This is the rule `TotalSummary` follows: a total over
  nothing is not a zero.
- **When `unallocated_costs` is not zero**: "Fees not assigned to any asset: {amount}
  {currency} (conversions between stablecoins)."

### Per-asset table (`PositionTable`)

The columns are Asset, Quantity, Average cost ({cur}), Invested ({cur}), Price ({cur}),
Market value ({cur}), Unrealized P&L ({cur}) and Return. The currency goes in the header
rather than on every cell, which is what keeps the table narrow.

| Column | Content |
|---|---|
| Asset | `<th scope="row">` holding the symbol, then a text badge per flag, then a "Not in totals" badge when the asset is in `totals.excluded` |
| Quantity | `quantity`. When `unknown_basis_quantity` is not zero, add "({x} with no known cost)" |
| Average cost | `average_cost`, or "—" when null |
| Invested | `total_invested` |
| Price | `price.amount`. When `price.stale`, add "(stale, as of {RelativeTime price.as_of})". "—" when null |
| Market value | `market_value`. When null, show the sentence for `market_value_unavailable_reason` |
| Unrealized P&L | `unrealized_pnl`, signed, or "—" when null |
| Return | `unrealized_return_pct`, signed, 2 places, with `%`, or "—" when null |

#### Flags

Each flag has a badge text and an explanation. They live in `lib/accounting.ts` as
`Record<PositionFlag, string>`, so a flag added on the backend fails `tsc` until it has words.
The explanation is printed below the table, as a legend, for each flag that appears in it.

| Flag | Badge | Explanation (gist; final wording is the developer's) |
|---|---|---|
| `unknown_basis` | "Unknown cost" | Part of the holding has no known cost: it was deposited, or bought before the imported history begins. Its average cost, invested and unrealized P&L cover only the part with a known cost, and it is left out of the portfolio totals. |
| `history_incomplete` | "History incomplete" | A sale was larger than everything the imported history held, so a buy or a deposit is missing. The realized P&L of that sale is unreliable, and the flag stays. |
| `unattributed_fee` | "Fee not valued" | A fee on this asset's trades was paid in another asset whose cost is unknown, so this asset's figures leave that fee out. |

`market_value_unavailable_reason` reuses `PRICE_UNAVAILABLE_MESSAGES` and adds
`value_out_of_range`. The table is a `Record` over `PriceUnavailable | ValueUnavailable`.

#### Formats

Every amount renders through `<Money>`, so the exact wire string sits in `<data value>`.

- Quantities use the default options, up to 8 places.
- Average cost and price use 2 to 8 places, like `AssetTable`'s `PRICE_OPTIONS`.
- Invested, market value and P&L use 2 places.
- The return uses 2 places.

Numeric columns are right-aligned with tabular figures.

### The sign of P&L

The sign is a **symbol in the text**, not colour. `FormatMoneyOptions` gains
`signDisplay?: 'auto' | 'exceptZero'`, named after `Intl.NumberFormat`'s option:

- `'auto'`, the default, is today's behaviour. No existing output changes.
- `'exceptZero'` prefixes `+` to a positive amount and leaves zero unsigned. A negative amount
  keeps its `-`.
- The rule-1 boundary keeps its sign: a tiny gain renders `< +0.01`, and a tiny loss renders
  `> -0.01` as today.

The sign follows the exact value, not the rounded one. Colour classes for gain and loss are
allowed, but only on top of the symbol.

### History warnings (optional within this issue)

`warnings` may be listed in a collapsed `<details>` titled
"What the imported history could not account for ({n})". Each item gives:

- the date, from `formatAbsoluteTime`;
- the venue, by its display name from `EXCHANGES`, or the raw `source` when it is not a known
  key;
- the asset and the quantity, in one sentence per kind;
- for `unattributed_fee`, whose figures leave the fee out: `charged_to`, or "a conversion
  between stablecoins" when it is null.

This is what makes `history_incomplete` actionable: it says where to look. If it risks the
issue's size, the developer may leave it out and say so. The tech lead then files it.

### Layout

- At 1280 px the table fits the `.app` column without horizontal scrolling. Padding may be
  tightened for this table only.
- On a phone (375 px wide), the table sits in a scroll container:
  - The container is a `role="region"` labelled by the table's heading, and keyboard-reachable
    (`tabIndex={0}`).
  - The Asset column is sticky, with a background.
  - The page itself never scrolls horizontally.
  - If `jsx-a11y/no-noninteractive-tabindex` refuses the `tabIndex`, allowing `region` in that
    rule's `roles` option is acceptable, with a comment saying why.
- The summary `<dl>` stacks on narrow screens.

### Accessibility

- Headings: `h2` "Invested" for the section, and `h3` for "Per asset".
- The flag badges are text, never icons alone.
- `RelativeTime` is never inside a live region, per spec 016's lesson that a ticking region
  re-announces.

## API contract

Consumed, unchanged: `GET /api/accounting/positions` (spec 021) and `GET /api/exchanges`
(spec 016). No generated-schema change.

## Data model

None.

## Acceptance criteria

1. The table shows, per held asset, quantity, average cost, total invested, market value,
   unrealized P&L and return percentage, plus the price, with its age when it is stale
   (#20: "a stale price shows its age"; review N7).
2. The summary shows invested against market value, unrealized P&L and return, and realized
   P&L. It never shows a sum over nothing as zero.
3. Every amount renders from its wire string. The `<data value>` of each amount is exactly the
   string the endpoint sent, including 18-place strings.
4. The sign of P&L and of return is a `+` or `-` in the text. Zero is unsigned.
5. An `unknown_basis` asset carries a visible text marker and "Not in totals". The summary
   names it as left out and says why, and the legend explains the flag. The same holds for an
   `unpriced` exclusion, with its own reason.
6. A stale price shows "stale, as of" with a `<time>` carrying its `as_of`.
7. The empty state renders each of the five rows of the empty-state table, and the fallback
   when the exchanges query fails. "No trades imported yet" and "The exchange sync failed"
   are distinct, tested states.
8. Tests cover loading, the positions error with retry, the refetch error with data kept,
   empty, and each of the three flags: badge, legend, and exclusion for `unknown_basis`.
9. The value section behaves exactly as before, and its existing tests pass.
10. It is readable at 1280 px and at 375 px, and the page never scrolls horizontally. The tech
    lead checks this in the browser before the pull request, against a local backend seeded
    with synthetic fills.
11. Frontend coverage stays at 100 % on every metric, and the gate passes.

## Test plan

| # | Criterion | Test |
|---|---|---|
| 1, 3 | table content and exact strings | `pages/DashboardPage.test.tsx` (or a new `pages/dashboard/InvestedSection.test.tsx`): a fixture with 18-place strings, asserting `<data value>` per cell |
| 2 | summary; "—" when every held position is excluded; unallocated costs | same |
| 4 | the sign | `lib/money.test.ts`: `signDisplay: 'exceptZero'` for positive, negative, zero, `-0`, tiny positive and tiny negative, and `'auto'` unchanged. Plus rendered `+`/`-` in the table and summary |
| 5, 8 | the flags and exclusions | one fixture per flag. `unknown_basis` with `unknown_basis_quantity`, and an unpriced exclusion; a position with both is excluded once, as `unknown_basis` |
| 6 | a stale price | fixture with `stale: true`, `<time dateTime>` equal to `as_of` |
| 7 | empty states | `lib/accounting.test.ts` for the decision function, row by row, including precedence. Page tests for rows 2 and 3 and for the exchanges-failure fallback |
| 8 | loading, error and refetch | page tests |
| 9 | the value section unchanged | the existing `DashboardPage.test.tsx`, passing with only fixture additions (default handlers for the two new requests) |
| — | invalidation | `useSyncExchanges` settling invalidates `['accounting']` |
| — | closed positions | a zero-quantity position is not a table row and appears in the closed line |

## File ownership

| Agent | Owns |
|---|---|
| frontend-dev | `frontend/src/**` except test files and `frontend/src/test/**`. That includes `pages/DashboardPage.tsx`, `pages/dashboard/*.tsx`, `api/accounting.ts`, `api/exchanges.ts`, `lib/accounting.ts`, `lib/money.ts`, `lib/prices.ts`, `index.css`, and `frontend/eslint.config.js` only for the rule above |
| tester | `frontend/src/**/*.test.ts`, `frontend/src/**/*.test.tsx`, `frontend/src/test/**` |
| reviewer | nothing |
| tech lead | this spec, and the browser check |

## Risks

- **Most positions will be unpriced.** Only chain assets have prices (spec 021). The totals
  will cover few assets, and "Left out of these totals" will be long. That is the honest
  answer. The sentence must read well with many entries.
- **Moving the value section can break its tests in ways a diff hides.** Criterion 9 holds
  them unchanged, apart from new default handlers.
- **Two currencies on one page.** The value section may be in EUR while this one is in USD.
  Every amount carries its currency, in the header or the suffix.

## Rulings during implementation

- **R1. No branch for a response the backend cannot write (tester, before the gate).** Three
  branches were type-legal, but no backend response reaches them:
  - positions without a snapshot (`computed_at` null while `positions` is not empty);
  - a null `market_value` with a null reason;
  - a failed recompute with a null `error`.

  The 100 % branch floor would have needed a contradictory fixture for each. The page
  instead narrows once, where the backend's contract guarantees the shape:
  - The section's empty-state gate is `computed_at === null || positions.length === 0`.
    `computed_at` null is the endpoint's own definition of "no snapshot".
  - The unavailable-value sentence and the error class name render only when present,
    with no invented fallback.

  The rule for the rest of the issue is the same: never keep a branch only a contradictory
  fixture can reach, and never invent a value to fill it.
- **R2. Empty rows 1 and 2 are failures, rendered as `ErrorState` with `role="alert"` (developer).**
  Every time inside an alert is an `AbsoluteTime`, never a `RelativeTime`: a ticking phrase
  inside a live region re-announces. This settles the conflict between row 1's "`RelativeTime`
  of `last_recompute.at`" and the accessibility rule, in the rule's favour. The status line and
  the stale-price age are not live regions, so they keep `RelativeTime`.
- **R3. Row 2 does not claim that no trades were imported (developer).** A venue can hold
  fills, all between stablecoins, and still have a failed last sync. The row says that
  venue's trades may be missing because its last sync failed.
- **R4. Exclusions are grouped by reason, one line per reason, each listing its assets
  (developer).** Most positions are unpriced (see Risks), so one line per asset would be a
  long repetition of one sentence.
- **R5. "Exchange status is unavailable" shows whenever the exchanges query is in error,** in
  the empty and the non-empty case. A list kept across a failed poll is treated as unknown, as
  `ValueSection` treats its runs.
- **R6. The browser check (criterion 10) found the app header overflowing a phone.** It was
  run against a local backend seeded with synthetic fills. At 375 px, `.app-header` (title,
  nav, owner and "Sign out" in one flex row that did not wrap) made the document 513 px wide
  on every page. It predates this issue. The criterion forbids horizontal page scroll, so the
  header, the nav and the account controls now wrap.
  - After the fix, the document is exactly the viewport's width at 320 and 375 px. At 1280 px
    the header stays on one row.
  - The same check changed three sentences, because a fee paid in an asset never held is a
    disposal, not a sale:
    - the closed line says "no longer held";
    - the `negative_inventory` sentence says "A sale of, or a fee paid in, …";
    - the `history_incomplete` explanation says the same.
  - The figures shown were checked by hand against the seeded trades. Dark mode was checked
    at 1280 px.
- **R7. Nothing held at all is a genuine zero (tester).** When every position is closed, the
  summary shows `0.00` invested and `0.00` market value, and the return shows "—". Holding
  nothing is a real answer, not a total over nothing. The value section renders an empty
  portfolio the same way. The "—" rule applies only when something is held and all of it is
  excluded.
- **R8. Review findings (reviewer, before the pull request).**
  - **M1. A flag on an asset no longer held must stay visible.** `dispose` empties the pool
    when a disposal exceeds it, and `history_incomplete` and `unattributed_fee` are sticky, so
    a flag often sits on a closed position. The fix has three parts:
    - The closed line names each closed asset, with its flag labels beside the flagged ones.
    - The legend explains every flag shown anywhere, whether on a row or in the closed line.
    - Realized P&L carries a caveat naming the assets when any position, held or closed,
      carries `history_incomplete` or `unattributed_fee`.
  - **S1. Empty-state rows follow what the positions response proves.** `event_count === 0`
    on a snapshot proves no trades have been replayed. The new order is:
    1. The recompute failed. Unchanged.
    2. A venue's sync failed. Unchanged.
    3. `no_trades`, in either of two cases:
       - The exchanges list is known and `fills_stored` is 0 on every venue. `anyConfigured`
         is a boolean.
       - The list is unknown, `computed_at` is set and `event_count === 0`. `anyConfigured`
         is `undefined`, and the description stays neutral.
    4. `not_computed`: `computed_at === null` or `event_count === 0`. Rows 2 and 3 have
       already ruled out "no trades", so this is fills whose snapshot predates them.
    5. `no_positions`: only when `event_count > 0`.
  - **S2.** The summary says it includes at least one stale price when a held, non-excluded
    position has `price.stale`, as `TotalSummary` does.
  - **S3. `unallocated_costs` has three sources** (`results.py`):
    - a conversion's fee;
    - the value given in a swap whose received side has no known cost;
    - R11's share of a swap fee.

    The line reads as costs not assigned to any asset and names both origins, stablecoin
    conversions and swaps into units with no known cost. The earlier "(conversions between
    stablecoins)" in *Summary* is superseded.
  - **S4.** Fixtures must be shapes the engine writes. Every warning's asset has a position.
    A `negative_inventory` implies `history_incomplete` on its asset, and an
    `unattributed_fee` with `charged_to` implies that flag on `charged_to`. The fixture guard
    checks all three.
  - **S5. `unmatched_proceeds` is shown nowhere.** A portfolio figure needs a backend total,
    because the page sums nothing, so it is filed as #108 rather than done here.
  - **N1.** A held position with no known-cost units (`quantity` equal to
    `unknown_basis_quantity`) shows "—" for Invested and Unrealized P&L, not a `0.00` that
    reads as break-even.
  - **N2, N3. Explanations corrected.**
    - Unknown basis comes from units that arrived without a cost: a swap paid with
      unknown-cost units, a fee rebate in a non-cash asset, and, from #18, an adjustment
      without a cost. It does not come from buys before the history begins; selling those
      units is `history_incomplete`. The explanation also says that market value covers
      every unit.
    - The `unpriced` exclusion says there is no market value, which is true for
      `value_out_of_range` as well.
  - **Kept as they are.** Two "Try again" buttons when both sections fail. Each sits under
    its own titled alert.
- **R9. Delta review of R8 (reviewer). Nothing must be fixed; three small edits:**
  - **The unallocated line says "or", not "and".** It reads "from stablecoin conversions or
    from swaps into units with no known cost". The page cannot tell which one applied.
  - **Row 3 on the known-list path also requires `computed_at === null || event_count === 0`.**
    A list whose poll lags a stablecoin-only sync must not claim "No trades imported yet"
    over a snapshot that replayed trades. That case falls through to `no_positions`.
  - **The stale-price check reads `price?.stale` before the exclusion check.** The logic is
    the same, and the null-price path is then reached by an unpriced, excluded row, which is
    a writable shape (R1).
  - **The N5 trade-off stands.** While the exchanges list has not answered and the
    positions are empty, a failed positions refresh shows no alert. It needs a hung first
    `/api/exchanges` request, and the section shows no figures meanwhile.
