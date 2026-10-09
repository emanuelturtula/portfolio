# 039 — Two pages, value history first, one style

Issue: none; the owner's plan of 2026-10-08 (PR 5 of 5)
Status: in progress

## Problem

The navigation has three pages for two jobs. The dashboard has the figures. Details explains
them wallet by wallet. Wallets adds and archives the wallets that Details then lists again.
Someone checking one wallet reads two pages for it, and its label, address and chain appear on
both.

Spec 038 gives the value history a past. That makes the chart the dashboard's subject, so it
belongs directly under the figure it extends.

## Scope / Non-goals

- **Two pages in the navigation: Dashboard and Wallets.**
  - Health stays reachable at `/health` and stays out of the navigation, as it already is.
  - The backup notice links to it.
- **Dashboard, in this order:**
  1. The total value in USDT, set as the page's hero figure.
  2. What it could not include.
  3. The value over time.
  4. Holdings: the donut beside the table.
  - The Refresh button sits in the hero, beside the figure it refreshes.
  - The "Incomplete" line links to the Wallets page, not to Details.
- **Wallets, in this order:** the balances, then the registry.
  - The balances are what Details showed: when they were read, the total, the bars, the asset
    and wallet tables, and one wallet over time.
  - The registry is the add form and the list, under a heading of their own.
- **`/details` redirects to `/wallets`**, replacing the history entry, so a bookmark still
  lands somewhere useful.
- **The style tokens of #162 are refined.**
  - A type scale and a spacing scale are added as tokens.
  - The hero figure, the section headings and the notes use them, so every page uses one
    set of sizes.
  - Light and dark modes are still two selected sets, not one inverted.
- **Not in scope:**
  - Any change to an endpoint, a figure or a state's wording other than the ones named here.
  - The y-axis labels, which are display-only coordinates.
  - The polling interval.

## Rulings

- **R1.** No figure, notice or state is removed. Every behaviour of the Details page holds on
  the Wallets page, under the same accessible names.
- **R2.** The two pages keep their four states each. On the Wallets page, the balances and the
  registry fail independently: a failed balances read leaves the add form working, as the
  registry already does when its list fails.
- **R3.** One "no wallets" message per page. When nothing is registered, the balances section
  says "No balances yet" and points to the form below it, and the registry says "No wallets
  yet". The balances section does not link to the page it is on.
- **R4.** Headings keep their levels.
  - The balances' sections stay `h2`, as on Details.
  - The registry's form and list stay `h3`, under one `h2`, "Manage wallets". That heading
    replaces the page's old "Wallets" title.
  - Neither page has a title apart from its sections: the navigation names the page, and the
    dashboard's first heading is its hero figure, "Total value".

## Acceptance criteria

1. The navigation lists Dashboard and Wallets, in that order, and nothing else.
2. `/details` lands on `/wallets`, and the back button does not return to `/details`.
3. The dashboard shows the hero total with its Refresh, then the incomplete line, then the
   chart, then the holdings.
4. The Wallets page shows every figure and state the Details page did, and the add form and
   the list.
5. Every gate holds: frontend 100 %, and the full `scripts/check.py`.
