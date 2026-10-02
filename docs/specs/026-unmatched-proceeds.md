# 026 — Show the proceeds of sales of units with no known cost

Issue: #108
Status: done

## Problem

When units with no known cost are sold, the engine books what the sale brought in as
`unmatched_proceeds` and not as `realized_pnl` (spec 019). That is correct: there is no cost
to compare the proceeds with. Nothing shows the figure:

- `GET /api/accounting/positions` returns it per position, and `totals` has no portfolio
  figure for it.
- The dashboard shows it nowhere.

The sharpest case is an asset whose units all had unknown cost and were all sold.
`unknown_basis` is not sticky, so the closed position carries no flag. Its realized P&L is
`0`, and the line for assets no longer held says its realized P&L is in the total. The owner
sees nothing for money that did come in.

## Scope

- `totals.unmatched_proceeds` on `GET /api/accounting/positions`, summed in the domain.
- The dashboard's Invested summary shows the total, explains it, and names the assets it
  comes from with each one's amount.
- The line for assets no longer held marks the ones that carry it.
- The regenerated OpenAPI types, the documentation, and tests.

## Non-goals

- No change to the engine, to what it books as unmatched, or to the stored snapshot. The
  total is derived at read time from the positions the snapshot already holds, so there is
  no migration.
- No new column in the positions table and no badge on a held row. The list under the
  summary names every asset that carries the figure, held or not.
- No link to the manual adjustments page. That page is #111.

## Design: backend

- **`domain/accounting/valuation.py`.** `PortfolioTotals` gains `unmatched_proceeds: Decimal`.
  `value_portfolio` sums it with `money.add` over **every** position, exactly as it sums
  `realized_pnl`: before the branch that decides whether a position is excluded, so a
  position left out as `unknown_basis` or `unpriced` still counts, and so does one no longer
  held. Over no positions it is zero.
- **`api/schemas/accounting.py`.** `AccountingTotalsResponse` gains
  `unmatched_proceeds: MoneyStr`, placed after `realized_pnl`. Its docstring says the figure
  covers every position, like `realized_pnl`.
- **Nothing else changes.** The service already hands every position's value to
  `value_portfolio`. No router, repository, model or migration is touched.
- **The sign.** A sale's proceeds are net of every fee, so a fee paid in a third asset whose
  carried cost exceeds what the sale brought in makes them negative. The total is therefore a
  signed sum and is not clamped.
- **OpenAPI.** `frontend/src/api/generated/schema.ts` is regenerated
  (`uv run python ../scripts/dump_openapi.py` from `backend/`, then `npm run gen:api` from
  `frontend/`). The drift job must pass.

### Documentation

- `docs/accounting.md`: where `unmatched_proceeds` is defined, say that the positions
  endpoint also serves its total over every position and that the dashboard shows it beside
  realized P&L.
- `docs/operations.md`, the section that describes the positions endpoint's totals: say that
  `unmatched_proceeds` covers every position, as `realized_pnl` does.

## Design: frontend

The page still sums nothing. The total is `totals.unmatched_proceeds` as sent. The amounts
in the list are each position's own `unmatched_proceeds` as sent.

### When it shows

**When at least one position carries a non-zero `unmatched_proceeds`.** This is the issue's
"when it is not zero", stated on the positions rather than on the total, for one reason: the
figure is signed, so two positions can cancel to a total of exactly zero. A rule on the total
would then mark a closed position in the line below while hiding the figure and the
explanation the mark refers to. With the rule on the positions, the three always appear
together. A non-zero total with no position carrying it is a response the backend cannot
write, because the total is the sum of those positions, so no branch handles it.

### `lib/accounting.ts`

- `UNMATCHED_PROCEEDS_LABEL = 'Unmatched proceeds'`. One constant, used as the summary's
  term and as the mark in the closed-positions line.
- `hasUnmatchedProceeds(position)`: true when `position.unmatched_proceeds` is not zero,
  decided with `isZeroMoney(money(...))`, never by comparing strings.
- `UNMATCHED_PROCEEDS_EXPLANATION`:
  > Unmatched proceeds are what sales brought in, net of fees, for units with no known cost:
  > units that arrived without one, or units sold beyond what the imported history held.
  > They are kept out of realized P&L, because there is no cost to compare them with. Like
  > realized P&L, the figure covers every position, held or not.

  Both origins are named (R2).
- `describeClosedPositions` adds `UNMATCHED_PROCEEDS_LABEL` to a closed position's bracket
  when `hasUnmatchedProceeds` is true: after the flags' labels and before "Held exceeds
  history", which stays last. Example:
  `1 asset no longer held is not listed: ETH (Unmatched proceeds). Its realized P&L is in the total.`
  The closing sentence is unchanged. The mark is the label only, never an amount: the line
  is a string, and an amount on this page is always a `<Money>` element.

### `pages/dashboard/InvestedSummary.tsx`

When it shows (see above):

- A fifth entry in the `dl.invested-summary`, after Realized P&L: `dt` is
  `UNMATCHED_PROCEEDS_LABEL`, `dd` is `totals.unmatched_proceeds` through `<Money>` with
  `AMOUNT_FORMAT`, followed by the quote currency. `AMOUNT_FORMAT` and not `SIGNED_FORMAT`:
  it is an amount of money that came in, not a gain or a loss, so it carries no `+`. A
  negative one shows its minus.
- It keeps showing when every held position is excluded (`nothingComparable`), as Realized
  P&L does, because it covers every position.
- Below the `dl`, after the sentence about unreliable realized P&L and before the exclusions:
  a `<p>` with `UNMATCHED_PROCEEDS_EXPLANATION`, then a `<ul className="excluded-list">` with
  one `<li>` per position that carries the figure, in the endpoint's order:
  `<strong>{asset}</strong>: <Money …AMOUNT_FORMAT /> {quoteCurrency}`. The list is shown for
  one asset as well as for several. With one, it is what says which asset the total is from.

When it does not show, the summary renders exactly what it renders today: no fifth entry,
no explanation, no list.

### The legend

`FlagLegend` is not changed. Its rule is that every marker on screen is explained. The
"Unmatched proceeds" mark in the closed-positions line is the summary figure's own label,
and the explanation under the summary is on screen whenever the mark is, because both follow
the one condition above.

## Acceptance criteria

1. `totals.unmatched_proceeds` is a JSON string, equal to the exact sum of every position's
   `unmatched_proceeds`: held and closed, comparable and excluded. It is zero, spelled as
   the endpoint spells every zero total (`"0.000000000000000000"`), when there is no snapshot
   or no position carries any.
2. `value_portfolio` computes it with `money.add`, and a property test checks it against an
   independent exact sum.
3. The OpenAPI document and `schema.ts` carry the field, and the drift check passes.
4. With no position carrying a non-zero figure, the dashboard shows no "Unmatched proceeds"
   entry, no explanation and no list, and the closed-positions line has no such mark.
5. With at least one, the summary shows the entry beside Realized P&L with the total as
   sent, the explanation, and one list item per carrying asset with its own amount as sent,
   in the endpoint's order.
6. A closed position that carries it has "Unmatched proceeds" in its bracket in the
   closed-positions line, after its flags and before "Held exceeds history". A closed
   position that does not carry it has no such mark.
7. A held position that carries it is in the list. No badge is added to its row.
8. A negative total and a negative per-asset amount render with a minus and without a plus.
   Two positions that cancel show the entry with a zero total, and both items.
9. The entry, the explanation and the list still show when every held position is excluded
   from the totals.
10. No amount is summed, compared or parsed as a number in the frontend. No `float` appears
    in the backend change.
11. `docs/accounting.md` and `docs/operations.md` say what the total covers.
12. The full gate passes with the coverage floors unchanged: backend 99.7% total, domain
    100% lines and branches as measured, frontend 100% on all four metrics.

## File ownership

| Agent | Files |
|---|---|
| `backend-dev-108` | `backend/src/portfolio/domain/accounting/valuation.py`, `backend/src/portfolio/api/schemas/accounting.py`, `docs/accounting.md`, `docs/operations.md`, and the regenerated `frontend/src/api/generated/schema.ts` |
| `frontend-dev-108` | `frontend/src/lib/accounting.ts`, `frontend/src/pages/dashboard/InvestedSummary.tsx`, and `frontend/src/index.css` (R3) |
| `tester-108` | every test file on both sides, `frontend/src/test/**`, and the gate. Sole gate owner |

The tech lead owns this spec.

## Rulings

From the review of the working tree (reviewer: no must-fix, three should-fix) and from the
implementers' reports.

- **R1. The documentation does not say the figure is the only place the sales show.** The
  Exchanges page lists those fills. `docs/accounting.md` says it is the only place **on the
  dashboard**.
- **R2. The explanation names both origins.** On this page "Unknown cost" is a badge of its
  own (`unknown_basis`), and spec 022 (R8, N2) ruled that units sold beyond the history are
  `history_incomplete`, not unknown cost. A venue that keeps a limited history makes the
  second origin the likely common one, and an asset listed under a sentence about "units
  with no known cost" while it carries "History incomplete" and no "Unknown cost" badge
  reads as a contradiction. The text in *Design: frontend* is the corrected one.
- **R3. The fifth entry sits beside Realized P&L at desktop width.** The summary grid's
  tracks were `minmax(11rem, 1fr)`, which fits four in the 960 px column, so the fifth entry
  wrapped alone onto a second row. The minimum becomes `10rem`: five tracks at 960 px, still
  one column at 375 px. The stale "Four figures" comment in `index.css` goes with it.
- **R4. Criterion 1 names the zero as the endpoint spells it.** `"0.000000000000000000"`,
  not the literal `"0"`.
- **R5. The sign is confirmed against the engine, and the route is narrower than "carried
  cost".** Only `_book_sale` writes the figure. It goes negative only through a fee paid in
  a third asset, cash or not, that costs more than the sale brought in. A fee folded into
  the cash received cannot do it, because the trade is refused. A test drives `replay` to a
  negative figure and to a pair that cancels.
- **Accepted as they are.**
  - "Net of fees" has one exception that is already flagged: a third-asset fee of unknown
    cost is not deducted, and the position carries `unattributed_fee`, whose legend entry
    says its figures leave that fee out.
  - The closed-positions line still ends "Its realized P&L is in the total.", which is
    true. Naming unmatched proceeds there would need a second condition.
  - The list reuses the `excluded-list` class, which is spacing only.
  - The per-asset amounts are rounded to the cent and may differ from the rounded total by
    a cent. Each `<data value>` carries the exact string.
- **Found by the browser check, not caused by this change.** The Value section's Wallets
  table widens the page at 375 px when a cell is long. Filed as #118.
