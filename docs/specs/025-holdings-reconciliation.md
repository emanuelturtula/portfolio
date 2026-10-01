# 025 — Reconcile replayed quantities with the balances held

Issue: #104
Status: done

## Problem

A buy older than a venue keeps, whose coins are still held, leaves no trace in the events:
every later sale fits inside the recorded pool, the engine warns about nothing, and the
average cost and the profit are computed without it (`docs/adr/0001-weighted-average-cost-basis.md`).
What can show the gap is a comparison of two quantities per asset: what the replay says is
held, and what is actually held. The second needs the exchange balances, which nothing reads
today.

## Scope

- Read each configured venue's **spot** balances, with the read-only key already configured,
  at the end of that venue's successful fill sync, and store the last reading.
- A pure comparison in `domain/`, and `GET /api/accounting/reconciliation`, which serves it
  per asset with the state of every source it read.
- A "Holdings check" block in the dashboard's Invested section, and a badge on a position
  whose balances exceed its history.

## Non-goals

- **Automatic correction.** The owner decides what the missing units cost (#18).
- **A link to a page that records the adjustment.** That page is #111. Until it exists the
  block names the documentation, and #111 turns the sentence into a link.
- **Earn, futures, margin and funding accounts.** Only the spot account is read. See *The
  held side is a lower bound*, which is what makes this safe to leave out.
- **A history of balances.** One reading per account is kept, replaced whole.
- **Valuing the difference.** Only two assets have a price, so the tolerance is relative to
  the quantity and not to its value.
- Showing the balances on the Exchanges page.

## The held side is a lower bound

This is the rule every other decision follows from. The balances read are never everything
the owner holds: a wallet not registered here, an Earn or futures account, a venue whose key
was refused, a wallet no sync has read yet. So:

- **Balances above the history is a finding** (`history_short`). The owner holds at least what
  was read, the history accounts for less, so acquisitions are missing from it. Reading more
  sources could only make the gap larger.
- **History above the balances is not a finding** (`history_over`). Coins held where this
  application does not read, a withdrawal, a network fee, or a trading fee the import did not
  record all produce it. It is shown, quietly, and nothing asks the owner to correct it.

A source that contributes **nothing** therefore never produces a false `history_short`. It
can hide one, and the block says which source is missing.

**A reading that is out of date is not a lower bound** (R9). Coins moved after a reading was
taken are counted where they were and where they are now. So a reading is compared only while
it is current, and one that is not contributes nothing:

- A venue's reading is current when its last balance read succeeded, its last fill sync
  succeeded, and it is at most `MAX_READING_AGE` (24 hours) old.
- A wallet's reading is current when it is at most `MAX_READING_AGE` old.

What remains is the gap between two current readings: coins moved between a venue's reading
and a wallet's are counted twice, or not at all, until both have been read again. That is
minutes while both syncs are running. It is up to 24 hours when a source has stopped being
read without a recorded failure: a wallet whose chain is failing (#116), a venue whose
credentials were removed or whose timer is off, and the double failure of R5. The block shows
each reading's age, and this is why a `history_short` is a prompt to look and not a verdict.

## Design: providers

Confirmed against each venue's documentation on 2026-10-01. `docs/providers.md` records the
table and the sources, in the form the fills sections use.

| | Bitget | BingX |
|---|---|---|
| endpoint | `GET /api/v2/spot/account/assets` | `GET /openApi/spot/v1/account/balance` |
| documented at | Classic, "Get Account Assets" (`/legacy-docs/classic/spot/account/Get-Account-Assets`) | V3, Spot, Account Endpoints, "Query Assets" |
| rate limit | 10 a second per UID | 5 a second per UID, 3 a second per IP |
| key permission | not stated on the page | "Read" |
| parameters | `coin` and `assetType`, both optional; `assetType` is `hold_only` (the default) or `all` | `timestamp` required, `recvWindow` optional |
| answer | the usual envelope; `data` is an array of `{coin, available, frozen, locked, limitAvailable, uTime}`, all strings | the usual envelope; `data.balances` is an array of `{asset, free, locked}`, amounts as strings |

**What is sent.** Bitget: `assetType=hold_only`, written out, so the query is never empty and
the request is signed exactly as a fills request is. BingX: `timestamp` alone, signed and
sent as the fills request is. Both are labelled `exchange_balances`, a new entry in
`providers/http.py`'s labels, so the transport logs `https://<host>/exchange_balances` and
never a path or a query.

**The seam** (`providers/exchanges/base.py`):

```python
@dataclass(frozen=True, slots=True)
class AssetBalance:
    asset: str          # non-blank text that encodes as UTF-8
    quantity: Decimal   # finite, >= 0, storable at FILL_SCALE without rounding

class ExchangeProvider(Protocol):
    async def fetch_balances(self) -> Sequence[AssetBalance]: ...

def assemble_balances(items: Iterable[AssetBalance]) -> tuple[AssetBalance, ...]: ...
```

`fetch_balances` answers the spot account's balances: **one entry per asset, the total held
in the spot account, zero balances left out, sorted by asset**. `assemble_balances` enforces
it the way `assemble_fill_page` enforces a page: an asset named twice is an
`ExchangeSchemaError`, a zero is dropped, the result is sorted. `AssetBalance` refuses in
`__post_init__` what the column would transform, as `NormalizedFill` does. Every failure is
one of the seven exchange error classes.

**The total.**

- Bitget: `available + frozen + locked`. `limitAvailable` ("restricted availability, for spot
  copy trading") is **not added**: whether it is part of `available` or beside it is not
  documented, and adding it could count units twice. Under-reading is the safe side.
- BingX: `free + locked`, each decoded with `from_binary_float` first (R1).

Sums use `money.add`. Each field passes `require_fill_amount`; a negative amount is a schema
error.

**The asset's name must be the one that venue's fills carry**, or the comparison splits one
asset in two.

- BingX: `asset` as reported, held to the rule its fill parser applies to a base asset.
- Bitget: `coin` **upper-cased**. The documentation's sample is `"usdt"`, while the names on
  fills come from symbol info for symbols that are `[A-Z0-9]`. Upper-casing is right under
  either spelling. Two entries equal after upper-casing are refused by `assemble_balances`.

**Not documented, and designed around** (recorded in `docs/providers.md`):

- What Bitget answers for a Unified Trading Account. The owner's account is Classic (#76).
  A `null` `data` is refused; an empty array is "the spot account holds nothing".
- Whether BingX's spot balance and its "fund account" balance
  (`GET /openApi/fund/v1/account/balance`, whose documented sample is identical) are one
  account or two. Only the spot endpoint is read: reading both could count units twice.
- Whether `limitAvailable` overlaps `available`, as above.

## Design: storage

Migration `v0010`, following `v0007` for the changed table.

- `exchange_balances`: `id`, `exchange_account_id` (FK, `ON DELETE CASCADE`), `asset` `TEXT`,
  `quantity` `NumericText(FILL_SCALE)`; `UNIQUE (exchange_account_id, asset)`.
- `exchange_accounts` gains `balances_read_at` (`UtcDateTime`, null) and `balances_error`
  (`TEXT`, null, a named `CHECK` over `ExchangeSyncErrorKind`'s values).

`balances_read_at` is when a read last **succeeded**, on our clock. `balances_error` is the
kind the last attempt failed with, and `null` when the last attempt succeeded or none was
made. A failed read keeps the rows and `balances_read_at` of the last good one. That is a
storage rule only: the kept reading says when the venue was last read, and it is not
compared (R9).

`ExchangeBalanceRepository` (`repositories/exchange_balances.py`): `replace(account_id,
balances, read_at)` deletes the account's rows, inserts the new ones and sets the two columns,
in the caller's transaction; `record_failure(account_id, kind)`; `list_for_user(user_id)`,
which returns each account's key, two columns and rows. No SQL aggregate, comparison or
ordering touches `quantity`.

## Design: the sync

In `ExchangeSyncService._sync_account`, **after** the account's success outcome is committed:

1. If `balances_error` is `auth` or `insufficient_scope` and the trigger is not manual, the
   read is skipped, for the reason an `auth_failed` account is skipped.
2. `provider.fetch_balances()`, through `_with_rate_limit_retry`. No write transaction is
   open during the call.
3. Success: `replace(...)`, commit, and `exchange_balances_read` is logged with
   `exchange_key` and the number of assets.
4. `Exception`: rollback, `record_failure(account.id, failure_of(error).error_kind)`, commit,
   and `exchange_balances_read_failed` is logged with `exchange_key`, `error_kind` and
   `error_type`; an `internal` kind logs with the traceback, as `_report_failure` does.

**A failed balance read changes nothing else.** The account's outcome, its `sync_status` and
the run's status describe the fills, and stay as they were committed. The failure is visible
where it matters, in the reconciliation.

No read happens for an account whose fill sync failed or was skipped: fresh balances beside a
stale history would produce differences that mean nothing.

No log line, column or response carries an asset name or an amount, except the
reconciliation endpoint itself.

## Design: the comparison

`domain/accounting/reconciliation.py`, pure, exported from `domain.accounting`:

```python
RECONCILIATION_TOLERANCE_PCT: Final = Decimal(1)

class ReconciliationStatus(StrEnum):
    MATCH = "match"
    HISTORY_SHORT = "history_short"
    HISTORY_OVER = "history_over"

@dataclass(frozen=True, slots=True)
class AssetReconciliation:
    asset: str
    history_quantity: Decimal
    wallet_quantity: Decimal
    exchange_quantity: Decimal
    held_quantity: Decimal      # wallet + exchange
    difference: Decimal         # held - history
    status: ReconciliationStatus

def reconcile(
    history: Mapping[str, Decimal],
    wallets: Mapping[str, Decimal],
    exchanges: Mapping[str, Decimal],
    *,
    cash_assets: frozenset[str] = DEFAULT_CASH_ASSETS,
) -> tuple[AssetReconciliation, ...]: ...
```

- The assets are the union of the three mappings, **minus the cash assets** (the engine
  keeps no quantity for USDT and USDC), minus any asset whose history and held quantities are
  both zero. Sorted by asset.
- `status` is `match` when `|difference| * 100 <= RECONCILIATION_TOLERANCE_PCT * max(history,
  held)`, compared exactly: no division and no rounding. Otherwise `history_short` when
  `difference > 0` and `history_over` when it is negative.
- A negative input quantity raises `ValueError`.
- Every figure is carried at `AMOUNT_SCALE`, through `money.add` and `money.subtract`.

**Why one percent.** The one-time historical imports hold zero fees, and a venue's fee is
about a tenth of a percent of a trade, so a complete history sits a fraction of a percent
above the balances. One percent keeps that from being reported on every asset. A missing buy
that small moves the average cost by about as much, unless its price was far from the
average.

## Design: service and endpoint

`services/reconciliation.py`, importing no provider:

- The history side is the stored snapshot's `Position.quantity` per asset, read through
  `AccountingService`'s consistent snapshot read (spec 021, R5), exposed as a public method
  and not implemented a second time.
- The wallet side is each active wallet's latest snapshot, when current,
  `from_base_units(confirmed, decimals)`, summed per `ChainKey.asset_symbol`. A wallet with
  no snapshot adds nothing and is counted as unread; one whose snapshot is too old adds
  nothing and is counted as stale.
- The exchange side is the stored `exchange_balances` rows of the owner's accounts whose
  reading is current, summed per asset.
- The clock is injected. A reading dated after `now` is current.

`GET /api/accounting/reconciliation`, `operationId` `readReconciliation`, authenticated like
every endpoint, no parameters:

```json
{
  "computed_at": "2026-10-01T10:00:00Z",
  "tolerance_pct": "1",
  "assets": [
    {
      "asset": "BTC",
      "history_quantity": "0.500000000000000000",
      "wallet_quantity": "0.700000000000000000",
      "exchange_quantity": "0.300000000000000000",
      "held_quantity": "1.000000000000000000",
      "difference": "0.500000000000000000",
      "status": "history_short"
    }
  ],
  "max_reading_age_hours": 24,
  "last_recompute": {"at": "2026-10-01T10:00:00Z", "outcome": "written", "error": null},
  "exchanges": [
    {"exchange_key": "bitget", "balances_read_at": "2026-10-01T09:59:00Z",
     "balances_error": null, "not_compared_reason": null}
  ],
  "wallets": {"compared": 2, "stale": 0, "unread": 0, "oldest_observed_at": "2026-10-01T09:45:00Z"}
}
```

- `computed_at` is the snapshot's. **With no snapshot it is `null` and `assets` is empty**:
  "not computed", never "every balance is unaccounted for". `exchanges` and `wallets` are
  still answered.
- Every quantity is a JSON string. `exchanges` lists every account row, by key.
- `oldest_observed_at` is the oldest `observed_at` among the compared wallets, `null` when
  none is: the comparison is as old as its oldest input.
- `not_compared_reason` is `null` for a venue whose reading is compared, and otherwise the
  first of these that applies: `read_failed` (`balances_error` is set), `never_read`,
  `sync_failed` (the account's `sync_status` is not `ok`), `out_of_date`.
- `last_recompute` is what the positions endpoint serves under that name. When its outcome is
  `failed` the history is older than the balances, and the block shows no comparison.

## Design: frontend

`api/accounting.ts` gains `useReconciliation`, key `['accounting', 'reconciliation']`, polled
every 60 s, so the invalidation after a sync already covers it. Types come from the
regenerated `schema.ts`.

`InvestedSection` renders `<HoldingsCheck />` after its content. The block owns its query and
fails on its own:

| Query state | Renders |
|---|---|
| pending | nothing (the section already shows one loading region) |
| failed with no data | an `ErrorState` at heading level 3, "Could not load the holdings check", with Retry |
| `computed_at` is `null` | nothing (the section already says positions are not computed) |
| loaded | the block below; a failed refresh adds "Could not refresh the holdings check: … Showing what was last loaded." |

The block, under `<h3 id="holdings-check">Holdings check</h3>`:

1. One sentence: what is compared, and that a difference of `tolerance_pct` percent or less
   counts as a match. The percentage comes from the response.
2. **Sources left out of the comparison**, each a `role="alert"` paragraph, by the venue's
   `not_compared_reason`:
   - `read_failed`: that its balances could not be read and why (a message per error kind,
     in `lib/accounting.ts`), that they are left out, and the absolute time of the last good
     reading when there is one. For `auth` and `insufficient_scope`: that the key was refused
     for the balance read, that scheduled syncs will not ask again, and that a sync from the
     Exchanges page retries once the key is fixed;
   - `never_read`: that its balances are read after its next successful sync;
   - `sync_failed`: that its last sync failed, so its balances were not read and are left
     out, with the time of the last reading;
   - `out_of_date`: the time of the last reading, and that a reading older than
     `max_reading_age_hours` is left out;
   - `wallets.stale > 0` and `wallets.unread > 0`: how many, and that their coins are left
     out.
3. **When `last_recompute.outcome` is `failed`**: one `role="alert"` paragraph saying the
   history is older than the balances, so nothing is compared. Items 4 to 6 are not shown.
4. **`history_short` assets**, when there are any: a table (Asset, In history, Wallets,
   Exchanges, Difference), each quantity in a `<data value>` carrying the exact string, the
   difference with an explicit sign. Above it: that the balances hold more than the history
   accounts for; that the usual cause is buys older than an exchange keeps or coins acquired
   elsewhere, and that average cost and profit then leave those units out; that readings are
   taken at different moments, so coins moved between two of them are counted twice until
   the next sync, which is worth ruling out first; and that an opening balance records what
   is really missing (`docs/accounting.md`, "Recording what the history does not show").
5. **`history_over` assets**, when there are any: the same table inside a closed `<details>`
   whose summary counts them. The text names the causes (coins held where this application
   does not read, withdrawals, network fees, trading fees the import did not record, and a
   sale or conversion the import did not see) and says this check cannot tell them apart, so
   it flags nothing.
6. When neither list has an entry: "Every quantity matches the balances read." or, when
   `assets` is empty, "There is nothing to compare yet."
7. For the sources that were compared: venue names with a `RelativeTime` each, and the
   wallets' `oldest_observed_at`.

`PositionTable` shows a badge, "Held exceeds history", on a held position whose asset is
`history_short`. It links to `#holdings-check`. The legend explains it. No badge is shown
while the reconciliation is loading or failed.

The table must not overflow a 375 px viewport; it scrolls inside its own wrapper as
`PositionTable` does.

## Acceptance criteria

1. Each venue's provider reads its spot balances from the endpoint above, signed as its fills
   request is, logged as `exchange_balances`, and every failure is one of the seven classes.
2. One entry per asset, zero balances dropped, a duplicate refused, Bitget's name upper-cased,
   the total as defined, amounts never a float.
3. A successful fill sync is followed by a balance read whose result replaces the stored one;
   a failed read keeps the last good reading, records its kind, and leaves the account's
   outcome, `sync_status` and the run's status untouched.
4. An `auth` or `insufficient_scope` balance failure is retried only by a manual sync.
5. `reconcile` implements the union, the cash exclusion, the tolerance and the three statuses
   exactly, with no rounding.
6. The endpoint returns the shape above, strings for every quantity, `401` without a session,
   and an empty `assets` with `computed_at: null` when no snapshot exists.
7. No request path reaches an exchange provider; the import contracts pass unchanged.
8. The dashboard shows the block in every state of the table above, the sources that are
   missing, the two lists with their different weight, and the badge.
9. `docs/accounting.md` explains the check and what each direction means;
   `docs/providers.md` records both endpoints, their sources and what is not established;
   `docs/operations.md` says only the spot account is read.
10. The coverage floors hold: backend total and domain as they are, frontend 100% on all four.

## Test plan

| Area | Tests |
|---|---|
| Providers | per venue: the request (path, query, signature, label), a parsed answer, the total, zero dropped, a duplicate, a negative, a non-string amount, a `null` `data`, an empty list, Bitget's upper-casing and its collision, each error class through the envelope; `assemble_balances` and `AssetBalance` on their own; no value in any message |
| Migration and repository | upgrade and downgrade; replace is whole; a failure keeps rows and `balances_read_at`; cascade; the `CHECK`; no aggregate on `quantity` |
| Sync | read after success; none after a failed or skipped account; failure isolation (outcome and status unchanged); the auth-skip rule and the manual retry; rate-limit retry; no network call inside a write transaction; log fields |
| Domain | every status at and around the tolerance boundary; union and cash exclusion; both-zero omitted; negative input; exactness beyond 28 digits; Hypothesis: `held - history == difference`, statuses partition, order is sorted |
| Service and API | the three sides summed; an unread wallet; no snapshot; several accounts; `401`; allowlist pinned; strings for quantities; OpenAPI carries the operation |
| Frontend | every row of the state table; each source notice; both lists; the closed disclosure; the match and empty lines; the badge present, absent while loading, and its link; `<data value>` exactness; the signed difference |

## File ownership

| Agent | Owns |
|---|---|
| backend-dev (providers) | `backend/src/portfolio/providers/**`; `docs/providers.md` |
| backend-dev (core) | every other file under `backend/src/portfolio/**`; `docs/accounting.md`, `docs/operations.md`; `frontend/src/api/generated/schema.ts` (regenerated only) |
| frontend-dev | `frontend/src/**` except tests, `frontend/src/test/**` and `schema.ts` |
| tester (backend) | `backend/tests/**`. **Runs the final gate.** |
| tester (frontend) | `frontend/src/**/*.test.ts(x)`, `frontend/src/test/**`. Runs no `check.py`. |
| reviewer | nothing |
| tech lead | this spec, and the browser check at 1280 px and 375 px against a seeded local backend |

The providers developer commits `AssetBalance`, `assemble_balances` and the protocol method
first, so the core developer can build on them.

## Risks

- **Neither balance endpoint has been called with a real key by this project.** The parsers
  refuse whatever they do not recognise, so most surprises are a recorded `schema` failure on
  the balance read; the fills are untouched by it. Not all of them: a JSON object naming a key
  twice is read with its last value, by the decoder every provider shares (#114), and
  `docs/providers.md` lists what is accepted without being established, such as a BingX asset
  spelled differently from its fills.
- **Balances and history are read at different moments.** A withdrawal in transit between a
  venue and a wallet, or a trade between two syncs, shows as a difference until the next
  sync. The block shows how old each reading is, compares no reading that is not current
  (R9), and words a `history_short` as something to check.
- **Dust.** A venue lists remainders too small to trade. They surface as `history_over` or
  as tiny `history_short` rows, and the relative tolerance cannot tell them from a real gap
  without a price. They are shown as they are.
- **Owner data.** No balance, asset list or count from the owner's accounts is written in the
  repository. Tests and the browser check use synthetic values.

## Rulings during implementation

- **R1. BingX balance amounts are decoded as binary floats (providers developer).** The V3
  sample's `"244.18616265388994"` is seventeen significant digits: the shortest spelling of a
  double, the artefact the fills' `quoteQty` shows. A dust balance written that way has more
  than eighteen places, `AssetBalance` would refuse it, and the read would fail `schema` on
  every run. `free` and `locked` are therefore each decoded with the existing
  `from_binary_float` (fifteen significant digits) before they are added. The cost is at most
  half a unit in the fifteenth digit, far inside the tolerance. Bitget's amounts are exact
  strings and are not touched. **Nothing is rounded to `FILL_SCALE`**: a balance that still
  has more than eighteen places after the decode needs a venue precision finer than the
  column's, and stays a recorded `schema` failure. BingX's `VST` demo token gets no
  exclusion; whether a real spot account reports it non-zero is not established.
- **R2. A closed position that is `history_short` is named (tech lead, from the frontend
  developer's report).** The history saying "no longer held" while the balances hold the asset
  is the sharpest form of the problem, and the table has no row for it. The closed-positions
  line names "Held exceeds history" beside such an asset, after its flags, and the legend
  explains the label whenever any position, held or closed, carries it (spec 022's rule that
  flags on closed positions stay visible).
- **R3. Accepted as built in the frontend.** The tolerance is rendered as plain text and not
  as a quantity: it is a rule's constant, not an amount of the owner's. Each table is followed
  by one line saying what Difference is, because the table has no Held column. An account row
  whose credentials were later removed and whose balances were never read is told they are
  read "after its next successful sync", which will not come; the Exchanges page already says
  the venue is not configured, and no `configured` field is added for it.
- **R4. Accepted as built in the providers.** `assemble_balances` checks for a duplicate
  before it drops zeros, so an asset named twice is refused even when one entry is zero: a
  shape that is not recognised is refused. `_require_utf8` no longer keeps the
  `UnicodeEncodeError` as the refusal's context, because its arguments hold the whole string,
  which can now be an asset name. BingX's `unwrap_envelope` takes the envelope member as a
  keyword, defaulting to the fills', in place of a second envelope function.
- **R5. The isolation of a failed balance read is absolute (tech lead, from the core
  developer's report).** If recording the failure itself raises, for a locked database say,
  the sync rolls back, logs `exchange_balances_failure_not_recorded` with the venue and the
  exception's type, and goes on with the outcome it already committed. Without it one
  ancillary write could leave the next account unsynced and the run row `running` after
  every fill had been imported. Also accepted as built: a skipped read is logged as
  `exchange_balances_read_skipped` with its reason; a failure is logged before it is recorded,
  as `_sync_account` does; a failure while storing a good answer is recorded as `internal`;
  and `tolerance_pct` is served through `MoneyStr` like every other figure.
- **R6. Bitget's coin name is held to the rule BingX's asset is (tech lead, from the backend
  tester's finding).** A `coin` with whitespace, a control character or more than forty
  characters is a `schema` failure, checked before it is upper-cased, and nothing is stripped.
  A padded `" usdt "` would otherwise become an asset that is not the cash asset `USDT`, and
  the whole stablecoin balance would surface as `history_short`. An amount written as a JSON
  number is accepted, as `require_fill_amount` documents: it arrives as a `Decimal` or an
  `int`, never a float.
- **R7. The branch was fast-forwarded to `c95de52` (#107) before the gate (tech lead, from the
  backend tester's finding).** Without #107 a balance write put asset names and amounts on
  stdout at DEBUG, through aiosqlite's statement logging. The security tests run the sync at
  INFO and at DEBUG.
- **R8. Two behaviours older than this issue are filed, not fixed here (tech lead, from the
  backend tester's findings).** The shared JSON decoder keeps the last value of a repeated key
  (#114), and BingX ignores `Retry-After` on a throttle answered with HTTP 200 (#115). Both
  apply to fills as much as to balances. R1's decode has two visible consequences, pinned by
  tests: an integer balance past fifteen digits is moved in its sixteenth, and one that
  rounds up to twenty-one digits is refused.
- **R9. A reading is compared only while it is current (tech lead, from the review's
  must-fix).** The spec claimed that a source that could not be read never produces a false
  `history_short`. That holds for a source that contributes nothing, and the build kept a
  failed venue's last reading in the sum. Coins withdrawn to a wallet after that reading were
  then counted twice, and the block told the owner to record an opening balance for units
  that do not exist. The same happened, with no notice at all, for a venue whose fill sync was
  failing, for credentials removed after a read, and for a wallet whose chain kept failing.
  Now a reading that is not current contributes nothing and its source is named with the
  reason (*The held side is a lower bound*). `last_recompute` is served, and a failed one
  hides the comparison: a history older than the balances shows every asset bought since as
  missing. The texts changed with it: a `history_short` says what the usual cause is and asks
  the owner to rule out coins in transit before recording anything; a `history_over` names a
  sale or conversion the import did not see among its causes and says the check cannot tell
  them apart, where it said nothing needs correcting; a key refused on the balance read says
  that scheduled syncs will not ask again. The sentence justifying one percent was false for
  a missing buy far from the average price, and is corrected.
- **R10. The window is stated as it is (tech lead, from the delta review).** R9 bounded the
  stale-reading cases at 24 hours; it did not remove them, and six sentences called the gap
  "minutes". They now say minutes while both syncs run and up to 24 hours when a source has
  stopped being read without a recorded failure, and the guidance says the double count lasts
  until both sources have been read again. Leaving out a wallet whose chain failed in the
  last balance run, the way a venue with a failed sync is left out, is #116. Two more windows
  are stated where the check is described: between a sync's commits and its recompute a
  bought asset can show as `history_short`, and after a restart `last_recompute` is `null`
  until the startup recompute ends. The delta review found the original must-fix closed and
  no new one.
