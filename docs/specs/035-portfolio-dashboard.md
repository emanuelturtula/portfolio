# 035 — A dashboard that reads like a portfolio

Issue: #154
Status: done

## Problem

The dashboard was every table the application has, one above the other: the per-asset value,
the per-wallet value, the positions, the holdings check. Each was right, and together they
answered "what is my portfolio worth, and am I up?" only for a reader willing to add things
up across three sections, in two currencies.

## Scope / Non-goals

- **Three figures at the top, in USDT:** total value, net invested, profit or loss (with the
  return as a percentage inside the P/L card).
- **Holdings:** one row per asset with amount, price, value and share, and a donut of the
  value split.
- **The old dashboard moves to `/details`, unchanged.** Its sections already own their loading,
  error and empty states and their tests; moving them keeps every one of those.
- Not in scope: the value and quantity history charts (#156) and the daily price history they
  need (#155).

## Design

`GET /api/portfolio/summary` computes everything; the page formats strings and sums nothing.

**What is held.** Every tracked asset, summed over each active wallet's latest snapshot and
each exchange account's stored spot balances, whatever their age. A reading that is out of
date still counts and is named in `missing`, by the rules the holdings check uses
(`services.reconciliation`), so the two pages agree on which reading is stale. Cash
(`DEFAULT_CASH_ASSETS`) is not a holding: it was never invested. An untracked asset is named
in `untracked` and counted nowhere.

**What it is worth.** The cached USD price of each holding, read as USDT one for one. The
owner asked for USDT throughout, and a second price source to measure a spread of a fraction
of a percent is not worth a second set of failures.

**What went in.** `domain.portfolio.net_invested` over every stored fill (Bitget and BingX):
a buy adds its cash cost, a sell subtracts its proceeds, and a fee paid in cash adds on
either side. A trade between two cash assets moves nothing, though its fee still counts. A
fill quoted in something that is not cash cannot be valued without a price at its time; it
is left out and named (`fill_not_in_cash`). Manual adjustments are not trades and are not
counted.

So **P/L = value − net invested** is realized and unrealized together: the cash a sale
returned is out of the value (cash is not a holding) and out of the invested figure (the
sale subtracted it), so neither side counts it twice.

**Honest partial state.** Each gap in `missing` touches a figure: a wallet or venue unread or
stale, an unpriced asset or a stale price touch the value; a non-cash fill touches the
invested figure; both touch the P/L. The page puts a "Partial" chip on each figure touched
and lists the gaps in one line under the figures. When holdings exist and none could be
valued, the value and the P/L show a dash, not `0.00`.

**Colour.** Each asset's colour is fixed by the asset (`lib/assetColors.ts`): BTC orange, KAS
aqua, one spare blue, then grey. The three hues are the dataviz reference palette's slots
that clear the all-pairs colour-vision floors in both modes; a donut puts any two side by
side, so a fourth hue would not. Aqua is under 3:1 on the light surface, so every share is
also text, in the legend and the table. The P/L carries a sign and an arrow, never colour
alone.

**Charts and floats.** Recharts positions arcs with numbers. `toChartNumber` is the one
place allowed to turn a money string into one, and the ESLint money rule refuses `toNumber()`
everywhere else. The number places a mark; every label shows the original string.

## API contract

`GET /api/portfolio/summary` (`readPortfolioSummary`), authenticated:

```json
{
  "total_value": "30770.000000000000000000",
  "invested": "30701.300000000000000000",
  "pnl": "68.700000000000000000",
  "pnl_pct": "0.2238",
  "holdings": [
    {"asset": "BTC", "quantity": "0.4995", "price": "60000", "value": "29970.000000000000000000", "share_pct": "97.4001"}
  ],
  "missing": [{"kind": "unpriced", "subject": "KAS"}],
  "untracked": ["ZZDUST"]
}
```

`pnl_pct` is null when nothing was invested; a holding's `price`, `value` and `share_pct` are
null when it is unpriced. Holdings are ordered by value, largest first, unpriced last.

## Data model

None. The endpoint reads what the syncs and the price refresh already store.

## Acceptance criteria

1. The dashboard shows total value, invested and P/L in USDT, each from the summary's strings.
2. A figure that a gap touches says "Partial", and the gaps are named in one line.
3. Holdings list amount, price, value and share; an unpriced one shows dashes, not zeros.
4. The donut's every share is in its legend as text, and each asset keeps its colour whatever
   its rank.
5. The old sections are at `/details`, reachable from the header, and behave as before.
6. Loading, error, empty and refetch-failed states each render distinctly.

## Test plan

- `backend/tests/domain/test_portfolio.py`: net invested per side, fees, cash-to-cash,
  non-cash quotes, ordering, percentages; property tests for the sums.
- `backend/tests/api/test_portfolio_summary.py`: the end-to-end scenario, stale and unread
  sources, unpriced assets, non-cash fills, `401`, the allowlist, JSON strings, empty.
- `frontend/src/pages/DashboardPage.test.tsx`: every state, partial chips, signs, the legend,
  colour by entity, refresh.
- The old dashboard's tests run unchanged against `/details`.

## Risks

- **USD read as USDT.** A depeg would show as a wrong value. Accepted by the owner; the
  history work (#155) can add a USDT price if it ever matters.
- **A fill quoted in BTC is not invested money.** Named, not hidden; none exists today.
