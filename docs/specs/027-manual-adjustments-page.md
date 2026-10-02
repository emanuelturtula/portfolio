# 027 — A page to record and edit manual adjustments

Issue: #111
Status: done

## Problem

Manual adjustments (spec 023) exist as an API only. The owner records an opening balance
through the authenticated `/api/docs`, and deletes one from the browser console, because
Swagger UI cannot send the delete. That is not how anyone should enter money.

The dashboard's holdings check (spec 025) tells the owner that an opening balance records
what the history is missing, and can only name a section of the documentation.

## Scope

- A page at `/adjustments`: the list, a form that creates and edits, and a confirmed delete.
- One small read endpoint, `GET /api/accounting/first-trades`, which the form needs for the
  issue's third criterion: a date "before the earliest fill of that asset" is a fact no
  endpoint serves today.
- A link from the holdings check to the page, with the asset carried over.
- The documentation says the page is how adjustments are entered.

## Non-goals

- No change to what an adjustment is, to its validation, or to the four existing endpoints.
- No outflows, transfers or disposals. An adjustment is an inflow (spec 023).
- No bulk entry and no import from a file.
- The quantity is never prefilled from the holdings check: the difference shown there can
  include coins in transit between two readings (spec 025, R9 and R10).
- The page re-implements none of the server's rules. See *Validation*.

## Design: backend

### `GET /api/accounting/first-trades`

`operationId: readFirstTrades`. Authenticated like every path under `/api`. Nothing is added
to `PUBLIC_API_PATHS`.

```jsonc
{
  "assets": [
    { "asset": "BTC", "first_trade_at": "2025-03-01T10:00:37Z" }
  ]
}
```

- One entry per asset that takes part in at least one of the owner's imported fills, with
  the instant of the earliest such fill.
- **An asset takes part in a fill** as its base asset, as its quote asset, or as its fee
  asset when the fee amount is not zero. A zero fee moves nothing, so it does not count.
- **Cash assets are left out** (`DEFAULT_CASH_ASSETS`). An adjustment of one is refused, so
  a date for one has no use.
- Sorted by `asset`, in code-point order. An owner with no fills gets an empty list.
- Manual adjustments are not counted: the list answers "when does the imported history of
  this asset begin".

Implementation:

- `AccountingService.first_trades(user_id)` reads the fills the recompute already reads
  (`ExchangeFillRepository.list_fills_for_accounting`) and reduces them in Python with a pure
  module-level function in `services/accounting.py`. The minimum is taken on `datetime`
  values in Python. No `MIN()`, `ORDER BY` or comparison on the `executed_at` TEXT column
  (rule 2 covers datetimes stored as text as well as money).
- It reads the record's own columns and converts nothing, so the reduction raises on no
  record it is given (R7).
- The router parses nothing, calls the service and serialises. Schema models
  `FirstTradeResponse` and `FirstTradesResponse` in `api/schemas/accounting.py`.

### Documentation

- `docs/accounting.md`, "Recording what the history does not show": the Adjustments page is
  how an adjustment is entered, edited and deleted. `/api/docs` and the console line for the
  delete stay as the alternative, after it.
- `docs/operations.md`, section 15: the same, plus the new endpoint in the endpoint table,
  and the troubleshooting row about deleting from `/api/docs` points to the page first.

## Design: frontend

### Route and navigation

- `/adjustments`, wrapped in `RequireSession` like every route but `/login`.
- A fourth link in the main navigation, "Adjustments", after "Exchanges". The header must
  still fit a 375 px screen (spec 022 fixed an overflow there).

### Files

- `api/adjustments.ts`: the types from the generated schema, `useAdjustments()`
  (key `['accounting', 'adjustments']`), `useFirstTrades()`
  (key `['accounting', 'first-trades']`), and the three mutations. Every mutation
  invalidates the `['accounting']` root on success, which covers the positions, the holdings
  check, the list and the first trades.
- `lib/adjustments.ts`: everything that is not React. Mapping a failed mutation to field
  errors, the date conversions, the suggested date, the plain spelling of a stored amount.
- `pages/AdjustmentsPage.tsx` and `pages/adjustments/AdjustmentForm.tsx`,
  `AdjustmentList.tsx`.

### The page

In this order:

1. `<h2>Adjustments</h2>` and a short introduction: an adjustment records coins the
   imported history does not show, such as an opening balance bought before an exchange's
   history begins, or coins acquired off an exchange. It names `docs/accounting.md`,
   "Recording what the history does not show", in plain text.
2. **A failed recompute, when there is one.** The page reads `usePositions()` only for
   `last_recompute`. When its outcome is `failed`, a `role="alert"` paragraph says the last
   recompute of the positions failed, when, and with which error class, and that changes
   made here are saved while the dashboard's figures are from before it. The wording and
   the instant follow the dashboard's (`failedRecompute`, an absolute time). Nothing is
   shown while that query is pending or when it fails: the page's own job does not depend
   on it.
3. The form.
4. The list.

### The form

One form, in one of two modes. **Create** is the default. **Edit** is entered from a row's
Edit button, which fills the fields with that adjustment and moves focus to the form's
heading. The heading reads "Record an adjustment" or "Edit adjustment". The buttons are
"Record adjustment", or "Save changes" and "Cancel". Cancel returns to an empty create form.

| Field | Control | Notes |
|---|---|---|
| Asset | text input with a `<datalist>` | `autoCapitalize="characters"`, `autoComplete="off"`, `spellCheck={false}`. Hint: the symbol exactly as the exchanges spell it, in upper case, such as BTC. The datalist offers the assets of `first-trades`, which are spelled as the exchanges spell them. |
| Quantity | text input | No `inputMode` (R9). Hint: "Use a dot for the decimals, such as 0.5." |
| Unit cost (USD) | text input | No `inputMode` (R9). Optional. Hint: "Use a dot for the decimals. Leave empty if unknown." and that an unknown cost is not zero: the units count toward the quantity held and are left out of the average cost. |
| Acquired on | `<input type="datetime-local">` | In the browser's local time. |
| Note | `<textarea>` | Required. Hint: why you are recording this, in your words. |

Asset, Quantity, Acquired on and Note carry `aria-required="true"`, and not `required`:
native validation must not pre-empt the form's own refusals.

**What is sent.**

- `asset`, `quantity` and `unit_cost` are sent as typed, with surrounding whitespace
  removed. Nothing else is changed: a lower-case symbol is sent lower-case, and the server
  refuses it. An empty unit cost is sent as `null`.
- `note` is sent exactly as typed.
- `occurred_at` is the local date and time as an ISO 8601 string in UTC
  (`new Date(value).toISOString()`).
- **In edit mode the stored instant is sent unchanged unless the owner changes the date
  field.** The field shows the stored instant to the minute, in local time. A stored instant
  can carry seconds, and re-sending a rounded one would move the adjustment among fills of
  the same minute without the owner having asked. "Unchanged" means the field holds the
  string the form was opened with (R12).
- **In edit mode the amounts are shown without trailing zeros** (`1.5`, not
  `1.500000000000000000`), spelled with `decimal.js`, which is exact. They are strings from
  the first byte to the last.
- Create is a `POST`. Edit is a `PUT` with all five fields, `unit_cost` included.

**Validation.** The server is the validator (spec 023), and the form does not copy its
rules. The form refuses locally, with no request sent, only what it cannot send:

- an empty asset, quantity or note (empty after trimming), each "… is required." under its
  field;
- an empty date, and a date the browser's clock cannot represent (for which `toISOString`
  would throw), under the date field.

Everything else is the server's answer. A 422's `errors` are mapped by the last element of
`loc` onto `asset`, `quantity`, `unit_cost`, `occurred_at` and `note`, each shown under its
field with `role="alert"` and tied to the input with `aria-describedby` and `aria-invalid`.
An entry at any other location, and any other failure, is shown at the bottom of the form.
A 404 on save (the adjustment was deleted elsewhere) shows the API's detail there and
refetches the list. Changing a field clears the errors of the last attempt, as the wallet
form does.

**The suggested date.** When the asset field, trimmed, is exactly an asset of
`first-trades`, a hint under the date field says when the earliest imported trade of that
asset is, as an absolute local time, and which coins the date is for (R8): "Coins you
already held by then should be dated before it; coins acquired later should carry the date
you acquired them."
A button under it, "Use <date>", sets the date field to **local midnight at the start of the
day before** that trade's local day, computed with calendar arithmetic. It is a button and
never a prefill: the form cannot know whether the owner is recording an opening balance or
a later acquisition. When `first-trades` is pending or has failed there is no hint and no
datalist, and the form works.

**The asset in the URL.** `/adjustments?asset=BTC` opens the create form with the asset
field filled. Nothing else is read from the URL.

**After a success** the form returns to an empty create form and a `role="status"` line
says "Adjustment recorded." or "Adjustment updated.". The line is one element that is always
in the document, with its text swapped in (R10). The submit button and Cancel are disabled
while the request is pending.

### The list

- `<h3>Recorded adjustments</h3>` and a table inside a `.table-scroll` region labelled by
  it, in the endpoint's order (by `occurred_at`, then id).
- Columns: Asset, Quantity, Unit cost (USD), Acquired, Note, and the actions.
  - Quantity through `<Money>`, with the format the positions table uses for a quantity.
  - Unit cost through `<Money>` with `UNIT_PRICE_FORMAT`. **A `null` cost is the text
    "Unknown cost", never a zero and never a dash.**
  - Acquired through `<AbsoluteTime>`.
  - The note as text. It wraps, and a long one does not widen the table.
- Each row has **Edit** and **Delete**, each with an accessible name that includes the
  asset and the date. Delete asks first: the button is replaced by "Confirm delete" and
  "Cancel", focus moves to the confirm button, and Cancel returns focus to Delete. After a
  delete a `role="status"` line says "Adjustment deleted.". Confirming a delete clears what
  the line said before. A delete that fails shows the API's detail in a `role="alert"`
  beside the row. A 404 (deleted elsewhere) is handled as a delete that happened, and the
  line says "That adjustment was already deleted." (R10, R13).
  Deleting the adjustment the form is editing returns the form to an empty create form.
- States: a `Skeleton` while loading; an `ErrorState` with a retry when the first load
  fails; an alert above the stale list when a refetch fails; an `EmptyState` titled "No
  adjustments yet" when there are none. The form is independent of the list's state: it
  works while the list is loading or has failed.

### The holdings check

`HELD_EXCEEDS_HISTORY_GUIDANCE` loses its last sentence (the pointer to the
documentation). After the guidance paragraph, the "Held exceeds history" box gains a line:
"If the gap is real, record the missing coins for:" (R8) followed by one link per listed asset,
to `/adjustments?asset=<asset>`, with the asset as the link text. The asset is put in the
query string with `URLSearchParams`, never by string concatenation.

## Acceptance criteria

Backend:

1. `GET /api/accounting/first-trades` requires a session (the route-walking contract test
   covers it) and returns, per non-cash asset, the instant of its earliest fill as base,
   quote or non-zero-fee asset, sorted by asset.
2. A zero fee does not count, cash assets are absent, another owner's fills are not read,
   and no fills gives an empty list.
3. No SQL aggregation, ordering or comparison on a datetime or money column is added. The
   layering contracts hold.
4. The OpenAPI document and `schema.ts` carry the endpoint, with no drift.

Frontend:

5. `/adjustments` is behind the session guard, and the navigation has the link.
6. The list shows asset, quantity, unit cost, date and note. A `null` cost reads "Unknown
   cost". Loading, empty, first-load error with retry, and refetch error are each covered.
7. Creating sends a `POST` with the five fields as specified in *What is sent*, and editing
   sends a `PUT` with all five. Amounts are JSON strings, an empty cost is `null`.
8. An untouched date in edit mode re-sends the stored instant byte for byte. A changed one
   sends the new local time as UTC.
9. The local refusals send no request. A 422 shows each message under its field, and
   anything else at the bottom of the form.
10. The suggestion appears only for an asset of `first-trades`, names the earliest trade,
    and its button sets local midnight of the day before, including across a month
    boundary and a clock change.
11. `?asset=` fills the asset field in create mode.
12. Delete asks for confirmation, moves focus as specified, and a confirmed delete sends
    one `DELETE`.
13. Every mutation invalidates `['accounting']`, so a mounted positions query refetches.
14. A failed last recompute is shown on the page. A pending or failed positions query shows
    nothing.
15. The holdings check links each `history_short` asset to `/adjustments?asset=<asset>`.
16. No amount is parsed as a number, summed or compared as a string anywhere in the new
    code.
17. At 375 px the page does not scroll sideways, and the header still fits.

Both:

18. The documentation names the page as the way to enter adjustments.
19. The full gate passes with the coverage floors unchanged: backend 99.7% total, domain
    100% lines and branches as measured, frontend 100% on all four metrics.

## File ownership

| Agent | Files |
|---|---|
| `backend-dev-111` | `backend/src/portfolio/services/accounting.py`, `backend/src/portfolio/api/routers/accounting.py`, `backend/src/portfolio/api/schemas/accounting.py`, `backend/src/portfolio/api/dependencies.py` if needed, `docs/accounting.md`, `docs/operations.md`, and the regenerated `frontend/src/api/generated/schema.ts` |
| `frontend-dev-111` | `frontend/src/App.tsx`, `frontend/src/api/adjustments.ts`, `frontend/src/lib/adjustments.ts`, `frontend/src/pages/AdjustmentsPage.tsx`, `frontend/src/pages/adjustments/**`, `frontend/src/lib/accounting.ts`, `frontend/src/lib/money.ts` (R5), `frontend/src/pages/dashboard/HoldingsLists.tsx`, `frontend/src/pages/dashboard/InvestedSection.tsx` (only to share the failed-recompute rendering), `frontend/src/index.css` |
| `tester-backend-111` | `backend/tests/**`, and the gate. **Sole gate owner** |
| `tester-frontend-111` | every frontend test file and `frontend/src/test/**` |

The tech lead owns this spec and does the browser check at 1280 px and 375 px.

## Rulings

From the implementers' reports:

- **R1. A rebate counts.** `fee_amount` is signed, and a negative one is a rebate that moves
  the fee asset. "Not zero" means exactly that: a rebate makes its asset take part.
- **R2. A non-zero fee with no fee asset names nothing.** The engine's `Trade` refuses that
  shape, but this read builds no `Trade`, so such a stored row answers with its base and
  quote assets only.
- **R3. `first_trade_at` is the stored instant, unchanged.** It can carry fractional
  seconds. The frontend treats it as an instant and assumes no precision.
- **R4. The read loads the owner's whole fill history on each request, as the recompute
  does.** Accepted: every mutation of an adjustment already triggers a recompute that loads
  the same rows, so the page's refetch of `first-trades` after a change adds one read of
  what was just read. The reduction is a single pass with no decoding of amounts.
- **R5. The plain spelling of an amount lives in `lib/money.ts`.** `decimal.js` is imported
  by that module only, so `plainMoney` is added there and `frontend-dev-111` owns that one
  addition.
- **R6. The documentation points every "use a `PUT`" row at the page.** Three more
  troubleshooting rows of `docs/operations.md` than the spec named, so that the document
  does not send the owner to the API in one row and to the page in the next.
- **R7. "Cannot raise" is a claim about the reduction, not about the whole read.** A row
  the recompute refuses, a fee that is not a number and an unknown side all answer. A
  hand-edited row whose stored text does not decode fails in the repository, before the
  reduction, as it does for the recompute and for every other read of that table. A fee
  that is not a number is "not zero", so its asset is listed.

From the review of the working tree (reviewer: no must-fix, seven should-fix) and the tech
lead's browser check:

- **R8. The page does not call every gap an opening balance.** `first-trades` is per asset
  across every venue, and histories begin per venue. Coins acquired after the asset's first
  imported fill, such as a buy on a venue whose history starts later, are wrong at the
  offered date: the method is the weighted average, so an inflow dated too early changes
  the cost applied to every sale between that date and the real one, and no warning fires.
  The reviewer ran it on the engine and a gain became a loss of the same size. The button
  stays, because the date is right for coins held before that fill. The hint says which
  coins it is for, the holdings prompt says "the missing coins", and `docs/accounting.md`
  says the same under "Dating an opening balance".
- **R9. The amount fields have no `inputMode="decimal"`.** On an iPhone the decimal keypad
  shows only the region's separator, which is a comma in many regions, and the server
  refuses a comma. The fields use the default keyboard and a hint says to use a dot. The
  comma is not translated: amounts go as typed. Not verified on a device; the choice is the
  one that cannot leave the owner without a dot.
- **R10. Four behaviours the review found.**
  - Confirming a delete clears the status line, so a stale "Adjustment deleted." never sits
    beside a delete that failed, and a second delete is announced again.
  - The status line is one always-mounted `role="status"` element (spec 016, R11).
  - A 404 on delete refetches the list, as a 404 on save does. Otherwise the row stays, and
    its Edit opens a form that can only fail.
  - The form's Cancel is disabled while the save is pending. Otherwise Cancel after "Save
    changes" discards the form while the change still lands, with nothing said.
- **R11. The documentation says where an adjustment's id is.** The page shows no ids, and
  the `UnconvertibleAdjustmentError` row names one. The row points at
  `GET /api/accounting/adjustments` in `/api/docs` for them.
- **R12. Smaller points taken.**
  - An untouched date is compared with the string the form was opened with, so a change of
    the browser's zone between Edit and Save does not shift it. Inside the repeated hour of
    an autumn clock change, a changed time resolves to the first occurrence.
  - Deleting the adjustment being edited while focus is inside the form moves focus to the
    new form's heading, not to `<body>`.
  - The required fields say so with `aria-required`.
  - At 375 px "Unknown cost" stays on one line and the "Use <date>" button has the regular
    button size.
- **R13. A 404 on delete is a delete that already happened.** Found by the browser check
  of R10: with only a refetch, the row went, nothing was said, focus fell to `<body>`, and a
  form editing that adjustment stayed in edit mode. The adjustment is gone, which is what
  the owner asked for, so a 404 gets the hand-off of a successful delete: the whole
  `['accounting']` root is invalidated (the delete made elsewhere moved the positions
  too), focus goes where a delete sends it, a form editing that adjustment is emptied, and
  the status line reads "That adjustment was already deleted.". Any other failure keeps
  the row and shows its alert.
- **Accepted as they are.**
  - Every mutation awaits the refetch of the three accounting queries (R4).
  - After a 404 on save the form stays in edit mode with what was typed.
  - A save that fails on the network does not refetch the list. If the server committed
    first, a retry makes a second row, which the list then shows.
  - Two adjustments of one asset in the same minute share the accessible names of their
    row controls.
  - `App.tsx` spells `/adjustments` literally, as it spells every route.
  - A two-digit year typed in the date field is sent as typed. The server refuses only the
    future (spec 023).
