# 011 — Wallets page and portfolio value dashboard

Issue: #11
Status: implementing

## Problem

Everything the product needs to answer "what is my portfolio worth" exists behind the API
since #10, and none of it can be seen. There is no way to register a wallet except `curl`.
The dashboard is a placeholder that says "No wallets yet" whatever the database holds.

## Scope

- A `/wallets` page with these parts:
  - a list of the owner's wallets;
  - an add form with advisory, client-side chain hints;
  - archive, and restore for an archived wallet.
- Server-side refusals render next to the field they are about.
- The dashboard at `/` has four parts:
  - the total, marked partial whenever the backend says it is;
  - a row per asset, with quantity, price and value;
  - a row per wallet, with its reading, its freshness and its value;
  - a refresh button and a "last updated" indicator.
- Every way the picture can be incomplete is rendered as that, never as a zero:
  - an unread wallet;
  - an unpriced asset;
  - a stale price;
  - a chain that failed its last sync;
  - an endpoint that did not answer.
- Header navigation between the two pages.
- The deferrals earlier specs addressed to #11 are rendering `pending` (#7) and deciding what a
  stale balance looks like (#10). The rest of this section resolves both.

**No backend change.** Every field this needs has been on the wire since #10. The one
aggregation it performs is summing wallet rows into asset rows, which runs over the strings
the backend already sent. It is exact; see [Asset rows](#asset-rows-are-sums-of-the-wallet-rows-the-response-already-carries).

## Non-goals

- **A value-over-time chart.** None of #11's criteria asks for one, so the wallet history
  endpoint has no consumer here. The backward-cursor question #10 left for #11 goes with the
  chart, and no issue asks for that yet.
- **Renaming a wallet.** `PATCH` supports it, but the criteria name list, add and archive.
  Restore is in scope only because the backend's own 409 tells the owner to restore; see
  [Restore](#restore-is-in-scope-because-the-backend-already-tells-the-owner-to-restore).
- **A currency switcher.** The dashboard asks for the backend's default, EUR, and renders
  whatever `quote_currency` comes back. `?quote_currency=USD` already works for anyone who
  wants it.
- **Any network check on registration.** That is #55. The hints below mention testnet
  prefixes, and refuse nothing.
- **Extended public keys** (#24). The hints recognise one, say it is not supported, and
  refuse nothing.
- **Invested-per-asset and profit and loss** (#20). **Exchanges** (#16).
- **#46** (`addMoney` rounds past 40 significant digits). Realistic sums here are about 33
  digits; see Risks.
- **#47** (frontend module boundaries). New code follows the existing layout: `api/`, `lib/`,
  `components/`, `pages/`.

## Design

### Routes and navigation

| Path | Element | Guard |
|---|---|---|
| `/` | `DashboardPage` | `RequireSession` |
| `/wallets` | `WalletsPage` | `RequireSession` |

`App.tsx` gains a `<nav aria-label="Main">` holding `NavLink`s to both pages. Like
`AccountControls`, it renders only when a session exists. The route table otherwise stays
as #4 left it.

### Queries

| Key | Request | Used by |
|---|---|---|
| `['wallets', { includeArchived }]` | `GET /api/wallets[?include_archived=true]` | both pages |
| `['balances', 'current']` | `GET /api/balances/current` | dashboard |
| `['balances', 'runs']` | `GET /api/balances/runs?limit=2` | dashboard |

| Mutation | Request | On success, invalidates |
|---|---|---|
| refresh | `POST /api/balances/sync` | `['balances']` |
| add | `POST /api/wallets` | `['wallets']`, `['balances']` |
| archive | `DELETE /api/wallets/{id}` | `['wallets']`, `['balances']` |
| restore | `PATCH /api/wallets/{id}` `{"archived": false}` | `['wallets']`, `['balances']` |

Fetchers and hooks live in `src/api/wallets.ts` and `src/api/balances.ts`, following
`session.ts`. Types come from `src/api/generated/schema.ts` and nowhere else.

Both balance queries set `refetchInterval: 60_000`. Neither endpoint reaches a vendor: the
current view reads the snapshot and price tables, and #9's contract keeps prices out of
request paths. So polling costs a database read. Without it, a dashboard left open never
shows the scheduler's next run, and its "last updated" line is the only thing that ages.

**The dashboard reads the wallet list for one reason: addresses.** `GET /api/balances/current`
leaves them out deliberately, and an unlabelled wallet has nothing else to be recognised by.
The wallet rows join on `wallet_id`.

### Freshness comes from the run log, not from an age threshold

#10 left one decision here. A wallet whose chain has failed on every run since its first
success keeps an old `observed_at`, and `complete` stays true. The question was what age is
too old to render as current.

**Age is the wrong test.** The frontend does not know the sync interval, which is a setting
that defaults to fifteen minutes. And "older than an hour" says nothing about why. The run
log records *which chain failed and whose fault it was*, so the rule reads that:

1. `settled` is the first run in `GET /api/balances/runs?limit=2` whose status is not
   `running`. `inProgress` is whether the first run is `running`.

   Two runs are enough because at most one run is ever `running`. The coordinator runs one at
   a time, and every run sweeps orphaned `running` rows before it opens its own.
2. For a wallet row on chain `C`, let `outcome` be `settled.chains`' entry for `C`. The rows
   below are checked in order, and the first match decides:

| Case | Status | Rendered |
|---|---|---|
| no settled run | never synced | "No sync has finished yet" |
| `outcome.status` is `success` and `observed_at` is at or after `settled.started_at` | fresh | "Up to date" |
| `outcome.status` is `failed` | failed | the error kind's sentence, then "showing the balance from" and the reading's age |
| `settled.status` is `interrupted`, and the reading is at or after `settled.started_at` | fresh | "Up to date" |
| `settled.status` is `interrupted` otherwise | interrupted | the last sync was interrupted before it read this wallet |
| anything else | not covered | not covered by the last sync |

**An interrupted run has no chain outcomes, ever**, and the first draft of this table assumed
it did. Review read the backend: `finish_run` writes the outcomes in the same transaction as
the final status, and the orphan sweep only flips the status. A chain's snapshots commit
before that close-out, though. So after an interrupted run, a reading stamped at or after its
start is evidence that the run read that chain before it stopped, and it is fresh. The draft
called every row "interrupted", including the chains the run did read, and its tests were
built on a fixture of an interrupted run *with* outcomes, a state the backend cannot write.

"No sync has finished yet" rather than "has run": when the only run is an orphaned `running`
row, a sync did run, and some of its readings may be on screen.

The second half of the fresh test is not decoration. **This change adds restore**, and a
restored wallet brings back the reading it had before it was archived. Its chain can succeed
in the latest run, which skipped the wallet because it was archived at the time, so a
chain-only test would call that reading fresh.

`observed_at` is stamped when a chain's read begins inside the run. It is therefore never
earlier than the run's `started_at`, and comparing at millisecond precision cannot make a
fresh reading look older.

**Timestamps are parsed through one `parseInstant`, which truncates the fraction to three
digits**, added in review. The backend sends microseconds, and ECMAScript only guarantees
`Date` parsing of its own format, which has exactly three fractional digits. Truncating
rather than rounding keeps `observed_at >= started_at` true whenever it was true in
microseconds.

When the runs query fails, the balances still render. A notice says freshness could not be
determined, and no row claims to be fresh.

**The same holds when the balances query is the one failing a poll**, which was added in the
second review. Suppose a new run lands, `/runs` refreshes, and `/current` does not. Judging the
readings still on screen against the newer run would call every row "not covered", and would
make the total claim balances the sync did in fact refresh.

The interrupted sentence names the *wallet*, not the chain. When a restored wallet's reading
predates an interrupted run that did read its chain, "before it read this chain" was false while
its siblings said "Up to date".

This logic lives in one pure module, `src/lib/freshness.ts`, with no React in it.

### Never a silent zero

| Situation | Where it shows | What it says |
|---|---|---|
| no active wallets | dashboard | an empty state linking to `/wallets` |
| unread wallet (`observed_at` null) | its row, and the total's "missing" list | "Not read yet", and the chain's failure if there is one; quantity and value render "—" |
| read wallet whose chain failed | its row; the total counts it | the reason and the reading's age; the total says it includes balances the last sync could not refresh |
| `price.stale` | the asset row and the wallet rows | the price's age, and that it is stale; the total says stale prices were used |
| unpriced asset | the asset row, and the "missing" list | the reason's sentence; value "—" |
| `complete: false` | the total | labelled partial, followed by what is missing |
| `complete: false` and no wallet has a value | the total | "—" in place of the amount: the backend's `"0"` is then an empty sum, not a value |
| a `running` first run | the status line, which is not a live region: its clock ticks | when that run started and that it has not finished. It does **not** disable Refresh |
| runs query failed | a notice | sync status unavailable; balances still shown |
| wallets query failed | a notice | addresses unavailable; rows fall back to label, or chain name and id |
| current query failed, nothing loaded yet | the page | `ErrorState` with retry, the only whole-page failure |
| current query failed on a later poll | a notice | the error; the data already on screen stays |
| refresh failed | beside the button | the error; the data already on screen stays |

A `null` value never reaches `formatMoney`, and no code path renders the string `0` for a
value the backend sent as `null`.

The sentences for each `SyncErrorKind` and each `PriceUnavailable` live in `Record`s keyed by
the generated union types. A member added to either enum then fails `tsc` until it has a
sentence.

### Asset rows are sums of the wallet rows the response already carries

For each `asset_symbol`, the asset row is built from the wallet rows that have a reading:

- `quantity` is the sum of their quantities.
- `value` is the sum of their values, or null when the asset is unpriced. A price is per
  asset, so all of an asset's values are null or none are.
- `price` is the asset's price.

When an asset has unread wallets, its row says the sum excludes them. When every wallet of
an asset is unread, the row renders "Not read yet".

Sums use `addMoney` from `src/lib/money.ts`, which works at 40 significant digits over plain
decimal strings. The rejected alternative was an `assets` list added to
`/api/balances/current`. The backend does compute one internally, but using it would put a
backend change into a frontend issue for a sum that is already exact here. It would also
give the page two sources for one number: a wallet row and its asset row would come from
different computations, while these rows are the wallet rows added up.

The total is **not** recomputed. It renders the backend's `total`, whose own sum is over the
same wallet values. There is one exception, added in review: when nothing could be valued, the
backend's `"0"` is an empty sum and renders as "—". Only an incomplete total can hit this. A
complete portfolio that is genuinely worth nothing still shows `0.00`.

**Prices render with between 2 and 8 fraction digits. Values and the total use exactly 2.**
The draft gave prices the fiat format too, so KAS at `0.084912345678` read "0.08". That is 6%
low, and the price column stopped multiplying out to the value beside it.

### Pending

#7 said "#10 stores it, #11 renders it". `pending` is a signed base-unit string, or null, and
Kaspa always sends null. When it is non-null and non-zero, the wallet row shows it as a
signed amount after the quantity, for example `+0.00012 BTC pending`.

**Spendable is not rendered.** #7 expected #11 to compute `confirmed + pending`. It is left
out because the value column multiplies `confirmed` alone. A third quantity beside a value
that does not cover it invites the reader to believe that it does.

`fromBaseUnits(units, decimals)` joins `src/lib/money.ts`, the one module allowed to use
`decimal.js`. The conversion is exact, because dividing by a power of ten only moves the
decimal point. It refuses anything that is not an integer string.

### Chain hints are advisory; the server's checksum is the only verdict

The domain verifies bech32, bech32m, Base58Check and Kaspa's CashAddr offline, in
`domain/addresses.py`. A TypeScript copy would be a second implementation of the same
codecs. When the two disagree, one of them blocks a valid address or passes an invalid one
that the server then refuses anyway. So the form checks one thing itself, that the trimmed
address is not empty, and sends everything else.

`src/lib/chains.ts` holds, per `ChainKey`:

- a display name;
- a format hint. For Bitcoin: `bc1`, `1` or `3`, and `tb1`, `m`, `n` or `2` on testnet. For
  Kaspa: `kaspa:`, or `kaspatest:` on testnet, and the prefix is part of the address.

It also holds a pure function returning at most one advisory hint for a typed address:

| Typed | Hint |
|---|---|
| empty | the chain's format hint |
| an extended key prefix (`xpub`, `ypub`, `zpub`, `tpub`, `upub`, `vpub`) | only single addresses are supported |
| a Kaspa prefix while Bitcoin is selected | looks like Kaspa, with a control that switches the chain |
| a Bitcoin shape while Kaspa is selected | looks like Bitcoin, with the same control |
| Kaspa selected, no `:` | Kaspa addresses include their network prefix |

A hint never disables the submit button. The table is `Record<ChainKey, …>`, so a new chain
fails `tsc` until it has an entry.

`WalletResponse.chain_key` and `WalletBalanceResponse.chain_key` are typed `string` in the
schema, not `ChainKey`. Rendering a name therefore falls back to the raw key for a chain this
build does not know.

### Field-level errors

The backend's 422 is a problem document plus `errors: [{loc, msg, type}]`, where
`loc = ["body", "<field>"]`. `api/errors.py` renders it, and `routers/wallets.py` routes a
refused address through the same shape. `ApiError` currently drops everything but the
standard members. `readProblem` gains a defensive parse of `errors`, which keeps only entries
whose `loc` is an array of strings and whose `msg` is a string. The result is exposed as
`problem.errors`.

The form maps `["body", "address" | "label" | "chain_key"]` onto that input. The message
goes under the input, linked with `aria-describedby`, and the input gets
`aria-invalid="true"`. An error at any other location renders at form level.

**A 409 renders under the address field.** A duplicate is a fact about the address, and the
backend's detail never names it. The submit button is disabled while the request is in
flight, which is the client half of #5's double-click finding. A retry that still arrives
twice gets a 409, rendered as a field error rather than a failure.

### Restore is in scope because the backend already tells the owner to restore

An archived duplicate answers:

> This address is already registered for this chain and is archived. Restore it instead of
> adding it again.

A page that renders that sentence with no way to restore sends the owner to `curl`. So the
list has a "Show archived" control. It switches the query to `include_archived=true`, marks
archived rows in text, not colour alone, and gives each a Restore button.

The UI does not branch on the 409's prose to find the archived row. There is no machine-readable
type to branch on. The detail says what to do, and the control to do it is on the same page.

**Archive is two steps.** An inline confirmation says the wallet will stop being read and will
leave the total. Archiving is reversible, but a mis-click silently changes the total, which is
the one number this page exists to show.

### Addresses: truncated, copyable, never in a URL

`src/lib/addresses.ts` truncates an address this way:

- With a `:`, it keeps the prefix, the `:` and 6 characters, then `…`, then the last 6.
- Without one, it keeps the first 8, `…`, then the last 6.
- An address short enough not to need it stays whole.

`<Address>` renders the short form, with the full address in `title` and a "Copy address"
button. Copying uses `navigator.clipboard.writeText`. On success it announces that the
address was copied through `role="status"`. On failure it says so and renders the full
address in a selectable element, so the owner can still copy it by hand.

**No address is ever put in a route, a query string or a log.** Wallets are addressed by id.
A test records every request URL the pages make and asserts that none contains a fixture
address.

### Time

`<RelativeTime value={iso}>` renders `<time dateTime>` with an absolute `title` and relative
text, for example "5 minutes ago". A `useNow(30_000)` hook re-renders it. Without that tick,
"just now" would stay on screen for as long as the tab stays open, which is exactly the kind
of truthful-looking stale label this page exists to avoid. Locale is fixed to `en`, because
UI strings are English.

The "last updated" indicator has two parts:

- **"Balances as of …"** is `as_of`: the newest reading.
- **"Last sync …"** is the settled run's status and its `finished_at`, or its `started_at` for
  an interrupted run.

The two answer different questions. They differ exactly when a sync ran and could not read
anything.

## API contract

Nothing changes. Consumed as #5 and #10 shipped them:

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/wallets?include_archived=` | `{wallets: WalletResponse[]}` |
| `POST` | `/api/wallets` | `201 WalletResponse`; `422` with `errors[]`; `409` |
| `PATCH` | `/api/wallets/{id}` | `{"archived": false}` restores; `404` |
| `DELETE` | `/api/wallets/{id}` | `204`; idempotent |
| `GET` | `/api/balances/current` | money fields are **strings**; `confirmed`/`pending` are base-unit **strings** |
| `GET` | `/api/balances/runs?limit=2` | newest first |
| `POST` | `/api/balances/sync` | `200 SyncTriggeredResponse` whatever the run's status; may take tens of seconds |

## Data model

None. No migration.

## Acceptance criteria

Verbatim from the issue, with the reading taken where the words leave room:

1. Wallets page: list, add with client-side chain hints, archive.
   *Hints are advisory and never block a submission.* The server is the only validator,
   apart from refusing an empty address locally. Restore is included; see above.
2. Adding a wallet surfaces server-side validation as field-level errors.
   *The 422 `errors[]` is mapped by `loc`, and a 409 renders under the address field.*
3. Dashboard shows total value, value per wallet, and quantity plus fiat value per asset.
4. A manual refresh button, and a "last updated" indicator.
   *"Last updated" is both `as_of` and the last settled run, relative and ticking.*
5. **Stale prices and provider failures are rendered explicitly, never as a silent zero.**
   *A stale price is the backend's `stale`. A provider failure is a failed chain outcome in the
   last settled run. A stale balance is a row that is not fresh by the rule above.*
6. A partial failure (one chain down) shows which part of the picture is missing rather than
   failing the whole page.
   *Two layers:*
   - *Data: a partial run, where the rows of the failed chain say so and the total says what it
     includes or lacks.*
   - *Requests: the runs query or the wallets query fails while the balances load.*
7. The empty state guides a first-time user toward adding their first wallet.
8. Amounts render from strings at full precision.
   *`<data value>` carries the exact string. Base units are converted without a JavaScript
   number, including values past `Number.MAX_SAFE_INTEGER`.*
9. Addresses are truncated in the UI with copy-to-clipboard.
10. Tests cover loading, empty, error and partial-failure states on both pages.

## Test plan

Vitest with MSW, rendered through `renderApp`/`renderWithProviders` under the shipped query
client. Fixtures use testnet addresses only.

| # | Criterion | Test |
|---|---|---|
| 1 | list | `pages/WalletsPage.test.tsx`: "lists the owner's wallets with chain, label and address" |
| 1 | add | "adds a wallet and shows it in the list", "disables the submit button while the request is in flight" |
| 1 | hints | `lib/chains.test.ts`: one test per row of the hint table. `WalletsPage.test.tsx`: "a hint never disables submission", "the switch-chain control changes the selected chain" |
| 1 | archive / restore | "archiving asks for confirmation first", "archiving removes the wallet from the active list", "show archived lists archived wallets with a restore button", "restoring returns the wallet to the active list" |
| 2 | field errors | "a 422 on the address renders under the address field", "a 422 on the label renders under the label field", "a 422 at an unknown location renders at form level", "a 409 renders under the address field", "an empty address is refused without a request". `api/client.test.ts`: "keeps a well-formed errors array", "drops malformed errors entries" |
| 3 | values | `pages/DashboardPage.test.tsx`: "renders the total, each wallet's value and each asset's quantity and value" |
| 3 | asset sums | "an asset held in two wallets shows the sum of both" |
| 4 | refresh | "refresh posts a sync and re-reads the balances", "refresh is disabled while its own request is in flight", "a running run does not disable refresh", "a failed refresh keeps the balances on screen and says why", "a failed poll keeps the dashboard on screen" |
| 4 | last updated | "shows balances-as-of and the last sync, relative to now", "the relative time advances without a reload" (fixed clock) |
| 5 | stale price | "a stale price is labelled stale on the asset and wallet rows" |
| 5 | unpriced | "an unpriced asset shows its reason and no value", one case per `PriceUnavailable` |
| 5 | provider failure | "a wallet whose chain failed shows the reason and the age of its reading", one case per `SyncErrorKind` in `lib/freshness.test.ts` |
| 5 | no silent zero | "a wallet with no value renders no zero", "an unread wallet renders no zero" |
| 5 | freshness rule | `lib/freshness.test.ts`: one test per row of the freshness table, including an interrupted run, which has no outcomes, that read the chain; a restored wallet whose reading predates the run; a running first run falling through to the second; and microsecond timestamps within one millisecond |
| 6 | partial run | "one chain down: the other chain's rows are fresh, the failed chain's rows say so, and the total says what it includes" |
| 6 | partial requests | "balances render when the runs request fails", "balances render when the wallets request fails" |
| 7 | empty | "no wallets: the dashboard links to the wallets page" |
| 8 | precision | "a balance past MAX_SAFE_INTEGER base units renders exactly", with the `<data value>` attribute asserted. `lib/money.test.ts`: `fromBaseUnits` round trips, negatives, refusal of a non-integer |
| 8 | pending | "a non-zero pending renders as a signed amount", "a zero or null pending renders nothing" |
| 9 | addresses | `lib/addresses.test.ts`: prefixed, unprefixed, short. `components/Address.test.tsx`: "copies the full address", "a failed copy shows the full address to select" |
| 10 | states | loading, empty, error, and partial failure on **each** page, named as above. Wallets page partial failure: "the add form still works when the list fails to load", "a failed archive leaves the row in place and says why" |
| — | nav | `App.test.tsx`: "the header links to the dashboard and the wallets page", "/wallets requires a session" |
| — | privacy | "no request URL carries an address", recorded across both pages' flows |

Coverage stays at 100% on all four measures. `vite.config.ts` does not move.

## File ownership

| Agent | Owns |
|---|---|
| frontend-dev | `frontend/src/**` **except** test files and `frontend/src/test/**`; plus `frontend/README.md`, which still describes a walking skeleton |
| tester | `frontend/src/**/*.test.ts`, `frontend/src/**/*.test.tsx`, `frontend/src/test/**` |
| reviewer | nothing |
| tech lead | `docs/specs/011-wallets-page-value-dashboard.md` |

Nobody edits `backend/**` or `frontend/src/api/generated/**`. If either turns out to need a
change, it comes back to the tech lead first.

## Risks

- **The freshness rule leans on a backend invariant**: at most one `running` run at a time.
  It holds today because of the coordinator and the per-run sweep, and it is written down
  here so that a change to either knows it has a reader.
- **`POST /api/balances/sync` holds the request for the whole run.** Thirty Bitcoin wallets
  take at least thirty seconds. If a proxy ever cuts the request, the run continues server
  side, because the coordinator shields it. The UI shows the refresh as failed, and the next
  poll shows the result. It is worded so that "failed" does not claim the sync did not run.
- **The clipboard API needs a secure context.** Production is HTTPS, since the `__Host-`
  session cookie requires it, and `localhost` counts as secure. The failure path is rendered
  anyway, because an unavailable clipboard is not an exception worth a blank button.
- **#46 is not reached.** Backend values come out of a 38-digit `Decimal` context
  (`domain/money.py`; the draft said 28, and review corrected it). The margin is therefore
  narrower than the draft claimed, but it is there. A quantity has 8 decimal places and a
  price 12, so a value has 20. The largest quantity, KAS supply, is 19 digits. A realistic
  sum stays near 33 significant digits, under `addMoney`'s 40. #46 is still the fix that makes
  this a refusal rather than an argument.
- **Clocks.** `started_at` and `observed_at` both read the backend's wall clock. If NTP steps
  it backwards between the two, a fresh row reads "not covered". That errs toward saying
  less than is true, and it is accepted.
- **100% frontend coverage.** Every branch of the hint table and the freshness rule needs a
  test. That is intended, and it is also where the time will go.
- **Time in tests.** Relative time is asserted under a fixed system time, faking `Date`
  only. Faking the timers too would stall MSW's promises.

## What the plan got wrong

Filled in at the end, before the pull request opens.
