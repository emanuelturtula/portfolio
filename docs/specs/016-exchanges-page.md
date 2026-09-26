# 016 — Exchanges page: sync status and history

Issue: #16
Status: implementing

## Problem

Since #15, the Pi imports Bitget fills on a timer. Everything it knows about that import is on
the wire: which venues are configured, where each account's sync stands, how much history
the venue kept, and what every run did. None of it can be seen without `curl` and a session
cookie. The one recovery that needs the owner, a refused key, ends with a manual
`POST /api/exchanges/sync` that no page offers.

## Scope

- A `/exchanges` page, behind the session guard, linked from the header navigation. It has:
  - one entry per venue, with a distinct status, what it holds and what is wrong;
  - a concrete remediation for `auth_failed`, which is the status that needs the owner;
  - a banner per venue whose history the venue's retention cut short, naming the instant
    from which the history is complete;
  - a **Sync now** button, with progress while it runs and a summary of what it did;
  - the run log: the last 20 runs, with counts and the errors each account met.
- An empty state for "no exchange configured", saying where keys go: environment variables
  on the host, never this UI.

**No backend change** except one docstring, which is wrong. The OpenAPI description of
`POST /api/exchanges/sync` says to fix the key and *restart* the container, and `env_file` is
read at creation, so it must say *recreate*. `docs/operations.md` already says recreate, and
this page's remediation says it too. `schema.ts` is regenerated for that docstring and nothing
else.

## Non-goals

- **Showing fills.** No endpoint serves them, and reading them is M4's job (cost basis). This
  page shows counts.
- **Entering, editing or testing a credential.** That is ruled out in CLAUDE.md rule 3 and
  in criterion 6.
- **Changing the history start.** That is `PORTFOLIO_EXCHANGE_HISTORY_START` on the host.
- **Per-venue sync.** The endpoint syncs every configured venue. A venue that fails does not
  stop the others.
- **BingX itself** (#14). BingX is already in `ExchangeKey`, so every `Record` here has an
  entry for it. It renders like Bitget once #14 configures it.
- **An exchange panel on the dashboard.** Nothing on the dashboard reads fills until M4.

## Design

### Route, navigation, files

| Path | Element | Guard |
|---|---|---|
| `/exchanges` | `ExchangesPage` | `RequireSession` |

`MainNav` gains a third `NavLink`, **Exchanges**, after Wallets.

| File | Holds |
|---|---|
| `src/api/exchanges.ts` | types from the generated schema, `useExchanges`, `useExchangeRuns`, `useSyncExchanges` |
| `src/lib/exchanges.ts` | pure, no React: venue names and variable names, the status rule, every sentence `Record`, and the remediation steps |
| `src/lib/time.ts` | gains `formatDuration(ms)` and `formatHistoryStart(iso)` |
| `src/pages/ExchangesPage.tsx` | the page: loading, error, empty, toolbar, and the sections below |
| `src/pages/exchanges/*.tsx` | `ExchangeList`, `TruncationBanner`, `SyncResult`, `SyncRunTable` |

Headings follow `WalletsPage`. The page is headed by an `h2` "Exchanges", and each section by an
`h3`: "Accounts" and "Sync history". A banner is headed by an `h3` too.

### Queries

| Key | Request | Polls |
|---|---|---|
| `['exchanges', 'list']` | `GET /api/exchanges` | every 5 s while any venue is `syncing` or this page's own sync is pending; otherwise every 60 s |
| `['exchanges', 'runs']` | `GET /api/exchanges/runs?limit=20` | every 5 s while the newest run is `running` or this page's own sync is pending; otherwise every 60 s |

| Mutation | Request | On settle (success **or** error) |
|---|---|---|
| sync | `POST /api/exchanges/sync` | invalidates `['exchanges']` |

Neither query reaches a venue: both are database reads. The fast rate is what turns them into
the progress display. Each page of fills is its own commit (#15), so `fills_stored` rises and
`pending_windows` falls while a sync runs. A 5-second poll therefore shows real progress
rather than a spinner.

Each query decides its own rate from its own data, so neither needs to watch the other. A
scheduled run that starts while the page is open is noticed within a minute, and after that
it is polled fast. The mutation invalidates on error as well as on success, because a cut-off
`POST` very often means the run is still going (see Risks).

### What the backend can write: the states to render

The status rule was designed against the code that writes each field, not against the
response type. Spec 011's lesson was that fixtures built from the type alone produced states
the backend cannot write, and missed ones it can. So here is what
`services/exchange_sync.py` and `services/exchanges.py` actually do:

- A configured venue with **no account row** is `never_synced`. Every instant on it is
  `null`, every count is 0, and `last_error` is `null`.
- The row is created at the start of a run, as `never_synced`. The plan then sets
  `requested_since`, `effective_since` and the pending windows. After that, each page commits
  its fills.
- **A run interrupted mid-account changes no status.** An account can therefore be
  `never_synced` with fills stored and windows pending. It can also be `ok`, from an earlier
  run, with windows pending.
- A failure sets `error` or `auth_failed` and leaves `last_synced_at` alone. So an `error`
  account can have a `last_synced_at` from its last success.
- A success sets `ok` and `last_synced_at` together.
- `syncing` is the coordinator's in-flight flag **and** `configured`, so an unconfigured venue
  is never `syncing`. `syncing` is true for **every** configured venue for the whole run,
  including the one not reached yet and the one already done.
- `configured: false` with a row means the credentials were removed. The row keeps its last
  state, and scheduled runs no longer include the venue.
- `auth_failed` comes only from an `auth` or `insufficient_scope` failure. Scheduled runs skip
  the account, and a skip does not replace `last_error`. So `last_error.error_kind` is one of
  those two, or `last_error` is `null` after a hand edit of the database.

The test fixtures encode these rules: a builder refuses `syncing` without `configured`, for
example. The tests cover each combination above that is marked possible.

### Status: one label, never colour alone

The label is `syncing ? 'Syncing' : STATUS_LABELS[status]`:

| `status` | Label |
|---|---|
| `ok` | Up to date |
| `never_synced` | Never synced |
| `error` | Sync failed |
| `auth_failed` | Authentication failed |
| (`syncing`) | Syncing |

**`syncing` replaces the label, not the rest of the entry.** An `auth_failed` venue that is
being retried still shows its error and its remediation, because until the run ends they
are still true.

Each entry also shows:

| Fact | Rendered |
|---|---|
| last complete sync | `last_synced_at`, relative and ticking; otherwise "Never" |
| fills stored | `fills_stored`, as a count |
| history complete from | `effective_since` through `formatHistoryStart`; otherwise "Not planned yet" |
| windows still to read | `pending_windows`, only when it is above 0 |

And, in this order, whichever of these messages apply:

1. **Not configured** (`configured: false`): "No credentials for {Venue} are configured on the
   host. The fills already imported are kept, and nothing new is read."
2. **Syncing**: "A sync is reading {Venue} now." The counts above update as it goes.
3. **`last_error`**, whenever present: `ERROR_KIND_SENTENCES[kind]`, then "Detail:" and the
   `detail` as plain text.
4. **`auth_failed`**: the remediation below.
5. **`error`**, configured, not syncing: "The next scheduled sync tries again."
6. **Windows pending**, configured, not syncing: "{N} windows of history are still to read.
   The next sync continues from them." For one window, "1 window of history is still to read.
   The next sync continues from it."

Rules 5 and 6 require `configured`, as rule 7 does. An unconfigured venue is in no run, so
promising a next sync beside rule 1's "nothing new is read" would contradict it on screen.
Rules 3 and 4 still apply: the last error is history, and the key remediation is exactly how
the credentials come back.
7. **`never_synced`, configured, not syncing**: "No sync has finished for {Venue} yet. The next
   scheduled sync imports its history, or press Sync now."

`ERROR_KIND_SENTENCES`, `STATUS_LABELS`, the run-status, trigger and outcome labels, and the
venue table are all `Record`s keyed by the generated unions. A member added to any enum then
fails `tsc` until it has a sentence.

| `error_kind` | Sentence |
|---|---|
| `auth` | {Venue} refused the API key. |
| `insufficient_scope` | The API key does not have read permission at {Venue}. |
| `rate_limited` | {Venue} throttled the requests for longer than the sync waits. |
| `unavailable` | {Venue} could not be reached, or answered that it was unavailable. |
| `retention_window` | {Venue} refused a window of history as older than it keeps. |
| `invalid_request` | {Venue} refused a request this application built. |
| `schema` | {Venue} answered in a shape this application could not read. |
| `conflict` | A fill {Venue} returned differs from the one stored under the same id. The sync stops at that page until someone looks. |
| `internal` | A defect in this application stopped the sync. The container log has the details. |

### Remediation for `auth_failed` (criterion 2)

This is an ordered list of steps, chosen by `last_error.error_kind`.

**`auth`, or no `last_error`:**

1. At {Venue}, check that the API key still exists, or create a new **read-only** key. If the
   key has an IP allowlist, it must include the address the host reaches the internet from.
2. Put the values in `secrets.env` on the host: {the venue's variables, each in `<code>`}.
3. Recreate the container with `docker compose up --force-recreate`. A restart does not
   re-read `secrets.env`.
4. Press **Sync now**. Scheduled syncs skip {Venue} until a sync you start succeeds.

**`insufficient_scope`:**

1. At {Venue}, edit the API key and grant **read** permission. Grant nothing else, and never
   trade, transfer or withdrawal.
2. Press **Sync now**. Scheduled syncs skip {Venue} until a sync you start succeeds.

   A new key instead needs steps 2 and 3 above first.

Both end with "docs/operations.md, section 13, has the full procedure."

The venue table is `Record<ExchangeKey, {name, variables}>`:

| Key | Name | Variables |
|---|---|---|
| `bitget` | Bitget | `PORTFOLIO_BITGET_API_KEY`, `PORTFOLIO_BITGET_API_SECRET`, `PORTFOLIO_BITGET_API_PASSPHRASE` |
| `bingx` | BingX | `PORTFOLIO_BINGX_API_KEY`, `PORTFOLIO_BINGX_API_SECRET`, **to be confirmed by #14** |

These are variable **names**, which are public in `docs/operations.md`, never values. No
value can reach this page, because no endpoint returns one.

### The truncation banner (criterion 5)

There is one banner per venue with `history_truncated: true` and a non-null
`effective_since`. The banners sit above the Accounts section, below the toolbar. Each is a
`<section>` headed "{Venue} history is incomplete" and styled as a warning. It is **not** a
live region: it is page state, and a 5-second poll would re-announce it.

> {Venue} does not return trades older than its retention window, so the history imported
> here is complete only from **Jun 27, 2026, 12:05:37 PM UTC**. Any trade made before then
> may be missing.

When `pending_windows > 0` it adds: "The import has not finished. That is where the history
will be complete from once it does ({N} {window|windows} still to read)."

**Why `effective_since`:** `docs/operations.md` defines it as "the instant from which the
history held is complete" once nothing is pending. That is the "exact earliest date actually
held" the criterion asks for. Older fills may exist, from before a venue refused a window,
but nothing promises them. So the sentence says "may be missing" and not "are missing".

**`formatHistoryStart(iso)`** renders the instant in **UTC**, at second precision, and
**rounds up** to the next whole second. It uses `toLocaleString('en', {dateStyle: 'medium',
timeStyle: 'long', timeZone: 'UTC'})`.

- **UTC** is the zone `PORTFOLIO_EXCHANGE_HISTORY_START` is read in, so the owner compares
  like with like. It also makes the tests independent of the machine's zone.
- **Rounding up** means the named instant is never earlier than the true one. Otherwise a
  trade in the truncated sub-second would be claimed as held.
- `<time dateTime>` carries the exact ISO string, and `title` the local time.

### Manual sync: progress and result (criterion 3)

**The button.** A **Sync now** button is shown when at least one venue is `configured`. With
none, a sync records an empty run and does nothing else. It is disabled only while this
page's own request is pending. **It is not disabled while a venue is `syncing`**, for spec
011's reason: a status must not become a lock. A click while a run is in flight joins that
run.

**Progress.** While the request is pending:

- a `role="status"` line says "Syncing exchanges… the first import can take several minutes.";
- both queries poll every 5 s, so each syncing venue's fills and windows move, and the run log
  shows the running run.

**Result.** This comes from the `POST` response, in a `role="status"` block that stays until
the next sync:

- "The sync {run-status verb}: {fills_inserted} new {fill|fills} ({fills_seen} read) from
  {accounts_total} {exchange|exchanges}, in {duration}."
- When `joined`: "It joined a sync that was already running."
- One line for each `failed` account: "{Venue}: {sentence}".
- One line for each `skipped` account: "{Venue} was skipped, because its key was refused
  earlier and only a sync you start retries it. Press Sync now again to retry it."

  Only a joined scheduled or startup run skips an account (spec 015, "Handed on"). The
  `POST` returns when that run ends, so pressing again starts a manual run.

**Failure.** A `role="alert"` says "The sync did not complete: {describeApiError(…, 'The server
could not be reached.')} A sync may still be running on the server; this page updates when
it finishes." The accounts and the run log stay on screen.

### Run log (criterion 4)

The Sync history section holds a table of the 20 newest runs:

| Column | Content |
|---|---|
| Started | `started_at`, relative, with the absolute time in `title` |
| Trigger | Scheduled, Manual, At startup |
| Status | Running, Succeeded, Partially succeeded, Failed, Interrupted |
| Duration | `formatDuration(duration_ms)`, or "—" when `null` (a running or interrupted run) |
| Exchanges | settled run: "{n} succeeded, {n} failed, {n} skipped", zero counts left out. `running` or `interrupted`: "{accounts.length} of {accounts_total} finished". "None" when `accounts_total` is 0 |
| Fills | "{fills_inserted} new of {fills_seen} read" |
| Details | per account: "{Venue}: {outcome label}". A failed one adds its sentence and "Detail: {detail}" |

An empty log renders "No exchange sync has run yet."

**The three account counters are written only when a run closes** (`finish_run`), while each
account's outcome is committed as that account finishes (`record_outcome`). A `running` or
`interrupted` run therefore has `accounts_total` from its opening, all three counters at 0,
and the outcomes of the accounts it got through. So its Exchanges cell counts `accounts`
rather than the counters, and it doubles as progress while the run is in flight. Its Fills
cell is right as it stands: the backend sums fills from the recorded outcomes.

**"Redacted errors" is the backend's guarantee, and the frontend's job is not to undo it.**
`detail` is built from a fixed summary, an HTTP status and a digits-only venue code (spec 012).
It never carries a trade id, a symbol or venue text, and
`backend/tests/security/test_exchange_sync_secrets.py` holds the backend to that. On this
side, an error renders as two things only:

- the sentence for its kind, from a fixed `Record`;
- `detail` as a React text node. It is never HTML, never a link, never placed in a URL, and
  never logged.

A test feeds a `detail` holding markup and asserts that it renders as literal text.

### Loading, error, empty

| Situation | Rendered |
|---|---|
| list pending | `Skeleton` "Loading exchanges…" |
| list failed, nothing loaded | `ErrorState` "Could not load exchanges", with retry. This is the only whole-page failure |
| list failed on a later poll | `role="alert"` notice; the entries already on screen stay |
| runs failed | a notice in the Sync history section; the accounts still render |
| `exchanges: []` | `EmptyState` "No exchange connected", described below. No Sync now button, no run log |

The empty state reads: "Exchange API keys are read from environment variables on the host,
for example `PORTFOLIO_BITGET_API_KEY`, and are never entered in this app. docs/operations.md,
section 12, explains how to create a read-only key and where to put it."

### Formatting

- **Counts** use `toLocaleString('en')`: "1,234 fills". They are counts, not money. The API
  sends them as integers, and the money lint rule does not apply.
- **`formatDuration(ms)`**:

  | Input | Output |
  |---|---|
  | under 1,000 ms | "under a second" |
  | under a minute | "N second(s)", floored |
  | under an hour | "M minute(s)", then " S second(s)" when S > 0 |
  | an hour or more | "H hour(s)", then " M minute(s)" when M > 0 |

### After review: what changes (R1–R14)

This section overrides the design above wherever the two disagree. It came from the
reviewer, and from the tech lead's own look at the page in a browser against a stub backend.
**The root cause was the same one spec 011 recorded.** "What the backend can write" missed
the most common failure shape. At a venue with no symbols call, which is Bitget, the plan is
committed before the first fetch (`_read_account`). A window is deleted only after its last
page commits (`_drain_window`). So **every `auth_failed` account, and every `error` account
except `internal`, has `requested_since` set and at least one window pending.** A first run
with a refused key leaves about 13 windows, 0 fills and `history_truncated: true`. The
fixtures defaulted to 0 windows, so no test ever rendered that state.

**R1. The "history complete from" fact is qualified while windows are pending.** With
`pending_windows > 0` it reads "History complete from: {instant} once the windows still to read
are read." It is unchanged otherwise. `models.py` defines `effective_since` as complete only
"once no window is pending".

**R2. Rule 6 has its own wording for `auth_failed`.** Scheduled runs skip that account, so
"The next sync continues from them" is false there. For `auth_failed`: "{N} {window|windows} of
history {is|are} still to read. The first sync you start after fixing the key continues from
{it|them}." It is gated like rule 6, on `configured` and not syncing: an unconfigured venue
has R13's sentence and rule 1, and a syncing one has R5's. Other statuses keep rule 6.

**R3. The scope remediation no longer points at steps that are not on screen.** Its note
reads: "A new key must first go into `secrets.env` on the host ({variables}), and the
container be recreated with `docker compose up --force-recreate`." **An unconfigured venue
always gets the `key` steps**, whatever the kind: its credentials have to come back first, and
without them the Sync now button is hidden. So `remediationFor` returns `'key'` when
`!configured`.

**R4. The label rule, in precedence order:**

| Condition | Label |
|---|---|
| `!configured` | Not configured |
| `status === 'auth_failed'` | Authentication failed, **even while `syncing`** |
| `syncing` | Syncing |
| `status === 'ok'` and `pending_windows > 0` | Unfinished |
| otherwise | `STATUS_LABELS[status]` |

**R5. Message 2 no longer claims the venue is being read.** It says "A sync is running." For
`auth_failed` it adds: "Only a sync you start retries {Venue}; a scheduled one skips it."
`syncing` is run-wide, a scheduled run skips an `auth_failed` venue, and with #14 BingX
backfills for minutes while Bitget would otherwise claim to be read.

**R6. No promise of a timer that may be off.**
- Rule 5 reads "The next sync tries again." It is not shown when `last_error.error_kind` is
  `conflict`, whose own sentence already says the sync stops there.
- Rule 7 reads "No sync has finished for {Venue} yet. Press Sync now to start one."

**R7. Fills on a `running` or `interrupted` run.** The account in flight has committed pages
but no outcome yet, and the backend sums only outcomes. So the cell reads "{n} new of {m} read
(finished exchanges only)". Settled runs keep "{n} new of {m} read". This reverses the
`cfd3dad` ruling, whose premise was right and whose conclusion was not.

**R8. The result names what was read, and leads with a skip.**
- `attempted = accounts_total − accounts_skipped`.
- The headline reads "The sync {verb}: {n} new {fill|fills} ({m} read) from {attempted}
  {exchange|exchanges}, in {duration}." When `attempted` is 0, it is "No exchange was read."
- When any account was skipped, the result **starts** with the skip lines, then "It joined a
  sync that was already running.", then the headline. The failed-account lines always come
  after the headline.

**R9. A failed `POST` does not claim that the server was unreachable, or that nothing ran.**
- The alert reads: "The sync request failed: {describeApiError(…, 'No answer came back from
  the server.')} A sync may still be running on the server; this page updates when it
  finishes."
- **It is cleared once the run log shows the outcome.** At the click, the page records the
  newest run's `run_id` and whether it was `running`, or 0 and false if there was none. The
  mutation is reset, so the alert disappears, when the newest run is settled and either:
  - its `run_id` is greater than the recorded one; or
  - its `run_id` equals the recorded one, and that run was `running` at the click, which means
    the request joined it.

  `run_id` rather than timestamps, so no clock is compared. If no new run ever appears, the
  request never started one, and the alert stays.

**R10. Focus stays on Sync now.** While its request is pending the button is
`aria-disabled="true"`, and a click on it does nothing. It is not `disabled`, which drops
focus to `<body>`, verified in the browser. The button keeps focus through the pending state
and after it. The Dashboard's Refresh has the same defect, and it is filed separately rather
than fixed here.

**R11. One persistent live region.** The toolbar holds one `role="status"` element that is
always in the DOM. The pending line and the result are swapped in as its children, because a
region inserted together with its text is not reliably announced. The failure stays a
`role="alert"`.

**R12. An orphaned `running` row does not keep the run log polling fast.** The runs query
polls fast while this page's `POST` is pending, or while the newest run is `running` **and**
the list shows a venue `syncing`. The list's `syncing` is the coordinator's in-flight flag.
A `running` row without it is an orphan left by a failed close-out, and it is swept at the
next run. The page passes the list's "any syncing" into `useExchangeRuns`. This is the one
place where a query reads the other's data.

**R13. The banner.**
- Its `title` is the local time of the same **rounded-up** instant, with seconds, so the
  tooltip never names an earlier minute than the text.
- The body text is not muted. Only the heading used the warning colour, against
  "prominent".
- When the venue is not configured, the pending sentence is replaced by "The import stopped
  before it finished ({N} {window|windows} still to read). Nothing new is read until
  credentials for {Venue} are configured again."

**R14. Small things.**
- In the run log, a period follows the outcome label: "Bitget: Failed. Bitget refused the API
  key."
- Sync now is not stretched to the toolbar's width.
- The venue's name is an `h4`, under the `h3` "Accounts".
- `docs/operations.md` says the page covers "the last step" of the recovery, not "the whole
  thing".

**Accepted, not changed:**
- `<time dateTime>` carries six fractional digits, which HTML does not allow. `RelativeTime`
  already does the same everywhere, and browsers parse it.
- Unmounting during a pending `POST` loses its result on screen. `onSettled` still
  invalidates, so the run log shows it.
- Page tests use `OUTCOME_LABELS` from the module under test. The spec leaves those words
  open, and `lib/exchanges.test.ts` holds them distinct.

## API contract

No endpoint changes. The page consumes #15's three endpoints as shipped: `GET /api/exchanges`,
`GET /api/exchanges/runs?limit=20` and `POST /api/exchanges/sync`. No field is monetary. The
one change is to the `description` of `syncExchanges`: "restarted the container" becomes
"recreated the container (`up --force-recreate`)".

## Data model

None. No migration.

## Acceptance criteria

These are verbatim from the issue, each followed by how this spec reads it:

1. **Accounts listed with a distinct status: ok, auth failed, syncing, never synced.**
   Each is a distinct text label. `error`, the fifth stored status, and "not configured" are
   rendered too. `syncing` overrides the label and keeps the rest of the entry.
2. **`auth_failed` shows a concrete remediation hint, since it needs the user to act.**
   Ordered steps, different for `auth` and `insufficient_scope`. They name the venue's
   variables, the recreate, and the manual sync that retries.
3. **Manual sync shows progress and a result summary.** Progress is a pending status line,
   plus 5-second polling that moves each venue's counts and shows the running run. The
   result is the `POST` response, summarised, including `joined` and skipped accounts.
4. **Run history with counts and redacted errors.** The last 20 runs, with account and fill
   counts. Errors render as the kind's sentence plus the backend-redacted `detail`, as text.
5. **A prominent banner when history is truncated by a retention window, naming the exact
   earliest date actually held.** It is a headed warning section above the accounts, naming
   `effective_since` in UTC, to the second, rounded up. It says so when the import has not
   finished.
6. **No credential input field exists anywhere in the UI; the empty state explains that keys
   are configured through environment variables on the host.** The exchanges page has no form
   control but its buttons. On every route, the only password input is the login page's, and
   no input anywhere is named like a key, a secret, a passphrase or a token.
7. **Tests cover every status plus loading and error.**

## Test plan

Vitest with MSW, through `renderApp`/`renderWithProviders`. Relative time is tested under a
fixed system time, faking `Date` only.

| # | Criterion | Test |
|---|---|---|
| 1 | statuses | `pages/ExchangesPage.test.tsx`: "an ok venue says up to date, when it last synced and what it holds", "a never-synced configured venue with no row says no sync has finished", "a never-synced venue with fills and windows pending shows both", "an error venue shows the kind's sentence, the detail and that the next sync retries", "a syncing venue is labelled syncing and keeps its error", "an unconfigured venue says its credentials are gone and keeps its counts", "an ok venue with windows pending says the next sync continues from them" |
| 1 | labels | `lib/exchanges.test.ts`: the label rule, with `syncing` overriding each status; one sentence for every `ExchangeSyncErrorKind` |
| 2 | remediation | "auth_failed with an auth error lists the four steps and the venue's variables", "auth_failed with insufficient scope asks for read permission", "auth_failed with no last error falls back to the key steps". `lib/exchanges.test.ts`: the step choice |
| 3 | progress | "Sync now shows a pending line and disables itself while its request is in flight", "while the sync runs, the venue's fills stored and windows left update from the poll", "a running run appears in the history while the sync is pending" |
| 3 | result | "the result summarises new and read fills, exchanges and duration", "a joined run says so", "a skipped account says to press Sync now again", "a failed account is named with its sentence" |
| 3 | not a lock | "Sync now stays enabled while a scheduled run is syncing" |
| 3 | failure | "a failed sync says a run may still be going and keeps the page", "a failed sync still refetches the list" |
| 3 | no venue | "no Sync now button when no venue is configured" |
| 4 | history | "the run log shows trigger, status, duration, account and fill counts", "a failed account's error shows its sentence and detail", "a detail holding markup renders as literal text", "an empty run log says no sync has run", "a failed run log is a notice and the accounts still render" |
| 5 | banner | "a truncated venue gets a banner naming effective_since in UTC", "the banner rounds a sub-second instant up", "the banner says the import has not finished while windows are pending", "no banner when history is not truncated", "the banner comes before the accounts in document order" |
| 5 | format | `lib/time.test.ts`: `formatHistoryStart` (whole second, sub-second rounded up, UTC whatever the machine zone); `formatDuration` (each row of its table, singular and plural) |
| 6 | no credential input | `credentials.test.tsx`: renders every route in `App`'s table (`/`, `/wallets`, `/exchanges`, `/health`, an unknown path, and `/login` signed out). It asserts that no `input`, `textarea`, `select` or `contenteditable` is named, labelled, `id`-ed or `name`-d like /key\|secret\|passphrase\|token\|credential/i, and that the only `type="password"` input is on `/login` |
| 6 | empty state | "no exchange: the empty state says keys come from environment variables on the host" |
| 7 | loading / error | "shows a skeleton while loading", "a failed first load is a page error with retry", "a failed poll keeps the entries on screen" |
| — | nav | `App.test.tsx`: "the header links to the dashboard, the wallets page and the exchanges page", "/exchanges requires a session" |
| — | polling | "the list polls fast while a venue is syncing and slows when it stops", using the query's `refetchInterval` observed through request counts under fake `Date` and real timers, or through the hook's option, whichever the tester can make deterministic |

Coverage stays at 100% on all four measures. `vite.config.ts` does not move. The backend
floor does not move either: the only backend change is a docstring.

## File ownership

| Agent | Owns |
|---|---|
| frontend-dev | `frontend/src/**` **except** test files and `frontend/src/test/**`. Also `frontend/README.md` (the pages list, and its "API types" section, which still says the schema is read over HTTP) and `docs/operations.md` (section 13 only: the page exists and **Sync now** is the manual trigger) |
| tester | `frontend/src/**/*.test.ts`, `frontend/src/**/*.test.tsx`, `frontend/src/test/**` |
| reviewer | nothing |
| tech lead | `docs/specs/016-exchanges-page.md`; the docstring fix in `backend/src/portfolio/api/routers/exchanges.py` and `services/exchange_sync.py`, and `frontend/src/api/generated/schema.ts` regenerated for it (done before the team starts, so no implementer touches `backend/**`) |

## Risks

- **`POST /api/exchanges/sync` holds the request for the whole run.** A first backfill takes
  minutes, and a proxy may cut the request first. The coordinator shields the run, so it
  continues. The page says so, keeps polling, and shows the result in the run log.
- **`syncing` is run-wide.** It is true for every configured venue for the whole run, so a
  venue already done still says "Syncing" until the run ends. It is correct, if coarse: the
  API has no per-venue in-flight flag, and adding one is not worth a backend change here.
- **BingX's variable names are a guess** at #14's naming, and only the remediation text
  shows them. #14 must confirm them or correct this table.
- **Polling at 5 s during a sync** runs two `COUNT`s per venue per poll against SQLite in WAL
  mode. That is negligible, and it stops when the tab is hidden.
- **The banner's instant is only as exact as `effective_since`.** A retention refusal can
  raise it later (#15, F2), and the banner then moves with it. That is the intended behaviour.

## Handed on

- **#14:** confirm `PORTFOLIO_BINGX_*` in `src/lib/exchanges.ts`. Check that BingX's auth codes
  map to `auth`, because this page's remediation keys on that.
