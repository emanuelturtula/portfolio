# 024 — Exchange transactions with filters and totals

Issue: #93
Status: in progress

## Problem

The Exchanges page shows how the sync went and how many fills are stored. No endpoint
serves a fill. The owner cares most about what was bought and sold. This spec settles the
details the issue leaves to it. The issue's *Behaviour*, *Constraints* and *Decisions taken*
sections stand as written, and this spec does not restate them.

## Scope

- A backend endpoint, `GET /api/exchanges/fills`, which returns filtered, paged rows and
  totals over the whole filtered set.
- A pure aggregation in `domain/`.
- A Transactions section on `/exchanges`, placed above Accounts, with filters held in the
  URL, the totals, completeness notices, three empty states and pagination.
- Sync history demoted into a disclosure below Accounts.

## Non-goals

These are the issue's *Out of scope*, plus one more:

- Cost basis and P&L (#17, #19 and #20).
- Editing fills.
- On-chain transactions.
- Converting non-USDT quotes.
- Filtering by asset or side, grouping by order, and CSV export.
- **Manual adjustments (#18) are not transactions here.** This view is the exchange fill
  log, as stored.

## Design: backend

### Endpoint

`GET /api/exchanges/fills`, with these query parameters:

| Param | Type | Rule |
|---|---|---|
| `exchange` | repeatable `ExchangeKey` | Omitted means all. An unknown value is a 422 (the enum). A repeated value counts once. |
| `from` | aware datetime | Inclusive. Optional. |
| `to` | aware datetime | Exclusive. Optional. |
| `limit` | int | `1..MAX_FILLS_LIMIT` (200), default 50. |
| `offset` | int | `0..`, default 0. An offset past the end is a `200` with no rows and the same totals. |

- **Datetimes.** A naive datetime is a 422. So is `from >= to` when both are given. A
  datetime sent as a JSON number cannot arrive in a query string, but a digit string can, so
  both bounds are parsed with `datetime.fromisoformat` (spec 023, R8).
- **Offset bound.** `offset` is bounded above by `2**63 - 1`, which is spec 023's lesson for
  integers that reach SQLite. Here it never does, but the bound keeps the answer a 422
  rather than a 500.

Response `200`:

```jsonc
{
  "fills": [{
    "id": 123,                      // our row id: a stable key, not the venue's trade id
    "executed_at": "…Z",
    "exchange_key": "bitget",
    "symbol": "BTCUSDT",
    "base_asset": "BTC",
    "quote_asset": "USDT",
    "side": "buy",
    "quantity": "0.5",              // base quantity
    "price": "60000",
    "quote_quantity": "30000",      // as stored, in quote_asset
    "quote_quantity_derived": false,
    "usdt_value": "30000" | null,   // quote_quantity when quote_asset == "USDT", else null
    "fee_amount": "0.0005",         // signed: negative is a rebate
    "fee_asset": "BTC" | null,
    "order_id": "…" | null          // external_order_id
  }],
  "total_count": 812,
  "totals": {
    "fill_count": 812,
    "by_asset": [{"asset": "BTC", "fill_count": 40, "bought": "…", "sold": "…", "net": "…",
                  "usdt_spent": "…", "usdt_received": "…", "usdt_net": "…",
                  "usdt_unvalued_fill_count": 0}],
    "usdt": {"spent": "…", "received": "…", "net": "…"},
    "not_valued_in_usdt": {"fill_count": 3,
                           "by_quote_asset": [{"quote_asset": "USDC", "fill_count": 3,
                                               "spent": "…", "received": "…", "net": "…"}]},
    "fees": [{"asset": "BNB", "amount": "…"}]
  }
}
```

- **Money.** Every money field is a JSON string, at the stored scale, normalised as the
  other endpoints do.
- **Order.**
  - `fills` are newest first by `executed_at`, with ties broken by `id` descending.
  - `by_asset` is sorted by asset, `by_quote_asset` by quote asset, and `fees` by asset.
- **Per-asset figures.**
  - `bought`, `sold` and `net` cover every fill of that base asset, whatever the quote.
  - `usdt_*` cover only its USDT-quoted fills.
  - `usdt_unvalued_fill_count` says how many of its fills those USDT figures leave out.
    Without it, a per-asset row mixing quotes would read as fully valued.
- **`not_valued_in_usdt.by_quote_asset`.** `spent` is the quote paid on buys, and
  `received` the quote received on sells. Each is in its own quote asset.
- **Every net is buys minus sells**, as the issue says. That applies to `net` (quantity),
  `usdt_net`, `usdt.net` and the per-quote `net`, which is `spent − received`. A positive
  net means net buying, and a filtered range may make any of them negative. The schema
  descriptions say so.
- **Fees.** A fee with `fee_asset` null (a zero fee) adds nothing. The fee sum per asset is
  signed, and a zero-sum entry is still listed when a fee in that asset occurred.

### Layers

- **`domain/fill_totals.py`** (pure) holds `FillLine` (the fields the totals need) and
  `total_fills(lines) -> FillTotals`.
  - Sums use `money.add` and `money.subtract`, which are exact at any length.
  - The ">28 digits" criterion holds by construction, and a test proves that plain `+`
    under the default context loses it.
- **`repositories/exchanges.py`** gains `list_fills_for_view(user_id, exchanges)`.
  - It returns plain records of every column except `raw_payload`, for the owner's accounts
    on the selected venues.
  - It is filtered by account in SQL (an integer and an enum column). It never compares or
    orders `executed_at` or money in SQL.
- **`services/exchanges.py`** gains `list_fills(user_id, *, exchanges, from_, to, limit,
  offset) -> FillsPage`.
  - It loads, filters by the half-open range in Python, sorts, totals the filtered set, and
    slices the page.
- **`api/routers/exchanges.py`** and **`api/schemas/exchanges.py`**.
  - The schemas' module docstring is rewritten: money now crosses this API, and so does the
    **order id**, which the owner needs in order to find a trade at the venue.
  - The venue's **trade id** (`external_trade_id`) is still never served.
  - Neither id is ever logged.

### Performance budget (issue: stated, and guarded)

Filtering and ordering run in Python because the datetime column is text.

- **The backend developer measures** the service call (load, filter, total and page)
  against synthetic fills: 5,000, 20,000 and 50,000 rows, on the development machine, with
  and without coverage. They record the figures under *Rulings*.
- **The budget is 20,000 fills in under 1 s on the Pi,** far beyond a personal history.
  The Pi is taken as 4× slower than the development machine, the margin spec 021 used.
- **A test guards the 20,000 case** under a bound the developer sets at 3× the measured
  time with coverage on. That catches a regression by an order of magnitude without being
  flaky in the gate.
- If the budget cannot be met, the fix is a deliberate, written exception that moves the
  account filter or the ordering into SQL. It is not taken silently.

## Design: frontend

### Structure of `/exchanges`

This is the order under the page heading:

1. The existing refetch alert.
2. The toolbar, with **Sync now**.
3. **One alert line per failing account** (`error` / `auth_failed`). It names the venue and
   links to `#exchange-<key>`. The account entries in `ExchangeList` gain that `id`.
4. **Transactions** (`h3`).
5. **Accounts** (`ExchangeList`), with the truncation banners kept as they are.
6. **Sync history.** It becomes a `<details>`:
   - Its `<summary>` always shows the newest run's outcome and age.
   - It is initially open when the newest run is `partial`, `failed` or `interrupted`, or an
     account is `error` or `auth_failed`, and closed otherwise.
   - Once the owner toggles it, their choice wins for the rest of the page's life.

**Failure isolation.**
- The exchanges list no longer blanks the page.
  - A failed first load is an error inside the Accounts section, with retry.
  - The toolbar is then hidden, because `configured` is unknown.
  - The completeness notice says completeness is unknown.
  - Transactions still render.
- A failed fills request is an error inside the Transactions section, with retry. Accounts
  still render.
- This is the same split the page already makes for the run log.
- The page-level **"No exchange connected"** empty state stays only when the list loaded
  and is empty. Transactions then does not render, because there is nothing to import from.

### Transactions

- **Filters live in the URL,** via `useSearchParams`.
  - The parameters are `exchange` (repeatable), `from` and `to` as local days
    (`YYYY-MM-DD`), and `page` (1-based).
  - Reload and back/forward restore them.
  - Changing a filter resets `page`.
  - **Clear filters** removes all four.
  - The venue choices are every `ExchangeKey` (`EXCHANGES` in `lib/exchanges.ts`), so they
    do not depend on the list loading.
- **Days to instants.**
  - `from` day → that local day's midnight.
  - `to` day → the **next** local day's midnight. The UI's end day is inclusive, and the API
    boundary is exclusive.
  - A `to` day before the `from` day is refused in the form, and never sent.
  - The page states the timezone used, from
    `Intl.DateTimeFormat().resolvedOptions().timeZone`: "Dates are days in
    Europe/Madrid."
- **The query.**
  - Key: `['exchanges', 'fills', filters, page]`.
  - It is invalidated by the existing `['exchanges']` invalidation after a sync settles.
  - It does not poll, and does not keep previous data across filter changes: it shows a
    skeleton instead of stale rows under new filters.
- **Scope sentence** above the totals, for example: "812 fills on Bitget and BingX from
  1 Mar 2026 to 31 Mar 2026 (inclusive)", or "…at any date".
- **Totals.**
  - A per-asset table with fills, bought, sold, net, USDT spent, USDT received and USDT net.
    A row with unvalued fills says "(N fills not in USDT)".
  - The USDT line.
  - A "Not valued in USDT" block per quote asset, shown when present.
  - Fees per asset.
  - All figures come from the server, formatted with `<Money>`; the client sums nothing.
  - **Signs are text:** `+` / `-` via `signDisplay: 'exceptZero'` for the nets.
  - On a phone the per-asset table scrolls inside a labelled region, as #20's does. Nothing
    is truncated.
- **Rows table.**
  - Columns: when (local, absolute), exchange, pair, side as a **word**, quantity, price,
    quote value, USDT value, fee with its asset, and order id.
  - A derived quote carries a "derived" marker.
  - A missing order id reads "none".
  - It scrolls in a labelled region on a phone.
- **Pagination.**
  - "Showing a to b of N".
  - Previous and Next use `aria-disabled` (spec 016 R10), and a no-op when not applicable.
  - The page size is 50, fixed.
- **Completeness notice** next to the totals, one sentence per selected venue and reason:
  - truncated → "Bitget history begins on {effective_since}; nothing before it is held."
    - When `from` is before `effective_since`, a stronger version says the selected range
      starts before what is held.
  - `pending_windows > 0` → "Bitget's import has N windows still to read."
  - `error` / `auth_failed` → "Bitget's last sync failed, so its latest trades may be
    missing."
  - When the list is unavailable → "Whether this history is complete is unknown: the
    exchange list could not be read."
- **Empty states**, when `total_count == 0`. The first that holds wins:
  1. Any selected venue is failing → "The last sync is failing", with a link to the account.
  2. No filters active, and the list says `fills_stored` is 0 everywhere, or the list is
     unavailable → "No fills imported yet".
  3. Filters are active → "Nothing matches these filters", with **Clear filters**.
  4. Otherwise → "No fills imported yet".

### Types

`schema.ts` is regenerated from the backend, and every type comes from it. The frontend
developer can start from the response shape pinned above before regeneration, and must
switch to the generated types before the gate.

## Acceptance criteria

These are the issue's criteria, all of them, backend and frontend, plus:

1. `usdt_unvalued_fill_count` is correct per asset, and the per-asset row states it.
2. The performance figures are recorded, and the guard test exists and passes in the gate.
3. The account entries have stable anchors, and the failing-account alert links to them.

## Test plan

| Area | Tests |
|---|---|
| Domain | `tests/domain/test_fill_totals.py`: every totals field; negative nets; signed fees; a null fee asset; mixed quotes; a >28-digit exact sum that fails under the default context; Hypothesis: totals over pages concatenated equal totals over the whole |
| Repository | `raw_payload` never selected, checked on the statement; the account filter; another owner's fills excluded |
| Service and API | filtering by none, one and several venues; half-open boundaries with two adjacent ranges; 422 cases (unknown venue, naive, inverted, digit string, limit and offset bounds); paging invariance of totals; an empty result; strings for money; 401; allowlist pinned; `raw_payload` absent from OpenAPI and from a real response; the timing guard |
| Frontend | loading, error (fills and list each isolated), the three empty states and precedence, filtered, paged (with `aria-disabled` at the ends), URL round-trip and Clear filters, local-day conversion and the stated timezone, negative net with a sign, the non-USDT group, each completeness notice, Sync history open and closed plus the owner's toggle winning, the failing-account alert link, derived marker, "none" order id, `<data value>` exactness, and the invalidation after a sync |

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/**`; `frontend/src/api/generated/schema.ts` (regenerated only); `docs/api.md` or the relevant doc if one lists endpoints |
| frontend-dev | `frontend/src/**` except tests, `frontend/src/test/**` and `schema.ts` |
| tester (backend) | `backend/tests/**`. **Runs the final gate.** |
| tester (frontend) | `frontend/src/**/*.test.ts(x)`, `frontend/src/test/**`. Runs no `check.py`. |
| reviewer | nothing |
| tech lead | this spec, and the browser check at 1280 px and 375 px against a seeded local backend |

## Risks

- **The page changes shape.** The existing `ExchangesPage` tests assume Accounts first and a
  whole-page error on a failed list. They change with the page. The frontend tester must say
  which assertions changed, and why.
- **Local days and DST.** "Next local midnight" across a DST change is not
  `midnight + 24 h`. The conversion uses calendar arithmetic on local dates, and a test
  covers a DST boundary in a fixed timezone (the test setup pins one).
- **Owner data.** The owner's real fill count is not written anywhere in the repository. The
  budget is stated against synthetic rows.

## Rulings during implementation

- **R1. `money.add` and `subtract` become 27× faster, to meet the budget (backend developer).**
  As first built, 20,000 fills took 0.446 s on the development machine, about 1.8 s on the
  Pi against a 1 s budget.
  - **Profile.** About 69,000 `money.add` calls at about 2.6 µs each. The rest was SQLAlchemy
    row access and enum construction in the load.
  - **Filtering in SQL would not help.** The worst case is no filter, which totals every
    fill anyway.
  - **Two fixes, both taken:**
    - **The load** unpacks row tuples and looks the enums up in dicts, inside the repository.
    - **`_exact_sum` becomes one call** on an explicit module-level `decimal.Context` with
      maximum precision and exponent range. It traps `Inexact`, `Rounded`, `Overflow` and
      `InvalidOperation`, so a rounding raises and never passes silently.
  - **Same results.** The public API and every result are unchanged. The developer checked
    them bit-identical on `as_tuple()` over 300,000 random pairs, with signed zeros,
    exponents from −60 to 60, and 5,001-digit coefficients, plus the 1E+100000 + 1E−100000
    gap.
  - **A standing guarantee.** A permanent Hypothesis property now holds the new functions
    equal to the old integer algorithm, kept in the test module as an oracle. A lab mutant
    lowering the precision to 28 must fail it.
  - **Why in this issue.** The primitive belongs to the accounting engine (#17), and changing
    it here is deliberate. It is the only change that meets the budget without a written
    exception, and the engine gets faster too. It lands as its own commit.
- **R2. Frontend interpretations (developer), accepted.**
  - **Empty states.** The precedence is: failing sync, then filters active ("Nothing matches
    these filters"), then "No fills imported yet". The spec's rows 2 and 4 render the same
    state, so `fills_stored` is not read for it. Reading it would be a branch nothing could
    observe.
  - **An inverted day range stays where the owner typed it.** It is not dropped. The form
    shows an alert and marks the To input `aria-invalid`, the fills query is disabled, and
    no results render. Dropping it would make a controlled date input eat digits mid-typing.
  - **Filters apply on change**, and each change is a history entry. Typing a year digit by
    digit can push one entry per valid intermediate date. That is accepted.
  - **Clear filters is always rendered,** and `aria-disabled` when nothing is set, so focus
    is never dropped.
  - **Paging keeps the previous page** while the next loads: rows dim with `aria-busy`, and
    the pagination stays mounted, so focus survives. A filter change shows the skeleton.
  - **A page past the end** (a hand-edited URL) keeps the totals and offers the last page.
  - **Fees show their raw sign.** A rebate reads as negative, and a fee paid has no `+`,
    because `+` would read as a credit. A legend says which is which.
  - **The failing-account link is a plain `#exchange-<key>` anchor.** A router link would
    drop the filters held in the URL.
  - **Sync history is a controlled `<details>`.** The owner's click is the only thing that
    records a choice. The heading sits outside the `<details>`, and with no runs there is
    no `<details>` at all.
  - **The run log table sits in a labelled scroll region** ("Sync runs"). It overflowed a
    phone before this issue.
  - **A DST bug was caught by the tester and fixed.** On a day whose local midnight does not
    exist (Santiago, 6 Sep 2026), the next-day boundary came out an hour late. It is now
    built from calendar fields.
