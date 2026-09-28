# 019 — Weighted-average cost-basis engine

Issue: #17
Status: implementing

## Problem

The fills are stored, but nothing turns them into what the product exists to report: how much
has been invested in each asset, at what average cost, and what has been realized. The engine
that does this is the correctness core. Its answer must depend on the events alone, and not on
the clock, on the order in which rows came back, or on how many times a fill was read. It also
has to be honest about incomplete history. Exchange retention guarantees that the history is
incomplete, and a confident wrong number is worse than a flagged one.

## Scope

- A pure function `replay(events, config) -> AccountingResult` in a new package
  `portfolio.domain.accounting`, with the event vocabulary it consumes and the result it
  returns. The events are a `Trade` (one fill), an `Adjustment` (an inflow the owner records,
  with or without a cost; #18 persists these) and a `Transfer` (a relocation).
- Three exact helpers in `domain/money.py`: `add`, `subtract` and `divide`. `divide` is the
  one place the engine rounds.
- A `domain-is-pure` import contract with a planted-violation test, and an AST test for the
  clock, which an import contract cannot see.
- A domain coverage floor of 95% lines and 90% branches, enforced in `scripts/check.py` and CI.
- `hypothesis` property tests for I1–I8, a golden scenario of about 40 fills, and
  `docs/accounting.md` with worked examples.
- An ADR recording weighted average and the IRPF caveat.

## Non-goals

- **Persistence.** The `position_snapshots` and `lots` tables, the accounting service, and
  recompute after a sync belong to #19. This package has no database. `replay` emits the lots
  that #19 persists; see *Lots*.
- **Manual adjustments' table, API and UI** are #18's. The engine's `Adjustment` event is here
  because its semantics (unknown basis) are engine semantics, and it leaves #18 as persistence
  and wiring.
- **Prices, market value and unrealized P&L** are #19's. The engine reports cost, not value.
- **FIFO** is future work: a second pure function over the same events.
- **Historical prices.** None exist in this system. `prices` holds one current row per pair.
- **Per-location holdings.** Positions are per asset across every venue and wallet. Where a
  coin sits comes from balances, not from replay, since deposits and withdrawals are not
  imported.
- **Positions for cash assets.** Cash is the unit of account (see *Vocabulary*), not inventory.
- **Outflow adjustments** (a loss, a gift sent). #18 or later, if the owner needs them.
- **Tax.** This is a dashboard figure. See the ADR.

## Design

### Where it lives

```
backend/src/portfolio/domain/accounting/
    __init__.py      # the public names, re-exported
    events.py        # EventKey, Trade, Adjustment, Transfer, AccountingConfig, validation
    results.py       # Position, PositionFlag, NegativeInventory, UnattributedFee, Lot, AccountingResult
    replay.py        # replay() and the per-asset pool arithmetic
    fingerprint.py   # canonical serialization and SHA-256
backend/src/portfolio/domain/money.py   # + add, subtract, divide
```

The internal split is the implementer's to adjust. The public names are the contract.
`FillSide` is reused from `domain/exchanges.py`.

### Vocabulary

- **Cash assets**: `config.cash_assets`, by default `DEFAULT_CASH_ASSETS = {"USDC", "USDT"}`.
  Each is pinned at a unit cost of exactly 1. That makes them the **unit of account**: every
  cost, basis, proceeds and P&L figure is in "cash units", which with the default set means
  US dollars in practice. Cash is never inventoried, has no position, never produces a
  negative-inventory warning, and a depeg is invisible by construction (see *Risks*).
- **Non-cash asset**: any other symbol. Symbols compare as exact strings.
- **Position** (one per non-cash asset): known-basis quantity `Qk`, known basis `C`,
  unknown-basis quantity `Qu`, realized P&L `R`, unmatched proceeds `U`, and sticky flags.
  Total quantity is `Qk + Qu`.

### Constants

`METHOD = "weighted_average"`. `ENGINE_VERSION = 1`, bumped by any change that can alter the
result for the same input. `QUANTITY_SCALE = BASIS_SCALE = AVERAGE_COST_SCALE = 18`, matching
`FILL_SCALE`, so sums of stored amounts stay exact and every derived value is representable
by #19's `NumericText(18)` columns.

### Events

All three are frozen dataclasses that validate in `__post_init__` and raise `ValueError`
(a `TypeError` for a wrong type). A bad event is a caller's defect, not a replay warning.

- `EventKey(occurred_at, source, external_id)`. `occurred_at` must be timezone-aware and is
  normalised to UTC on construction. `source` is the venue key (`"bitget"`, `"bingx"`) for
  fills and `"manual"` for #18. It and `external_id` are non-blank and UTF-8 encodable, since
  a lone surrogate would otherwise escape from the fingerprint as a `UnicodeEncodeError` (the
  #12 lesson).
- `Trade(key, base_asset, quote_asset, side: FillSide, quantity, quote_quantity, fee_amount,
  fee_asset)`. `quantity` and `quote_quantity` are greater than 0. `fee_amount` is signed
  (positive is paid, negative is a rebate), and `fee_asset` is `None` exactly when the fee is
  zero. `base_asset` is not equal to `quote_asset`. A fee in the received asset must leave
  something received, and a rebate in the given asset must leave something given (see *Legs*).
- `Adjustment(key, asset, quantity, unit_cost: Decimal | None)`. `quantity > 0`, and
  `unit_cost >= 0` or `None`. `None` means unknown basis, which is **not** zero. An
  `Adjustment` of a cash asset is accepted and changes nothing.
- `Transfer(key, asset, quantity, from_location, to_location)`. `quantity > 0`, the two
  locations are non-blank and differ. It changes no position.
- **Every amount**: a finite `Decimal` (never `bool`, `int` or `float`), at most 18 fractional
  digits and at most `MONEY_PRECISION - 18` integer digits. This is the rule `NormalizedFill`
  applies, so a stored fill's amounts always convert. Its shape may not; see R8.
- `AccountingConfig(cash_assets: frozenset[str])`: non-empty, non-blank members.

### Ordering and identity

- **Identity** is `(kind, source, external_id)`, where kind is `trade`, `adjustment` or
  `transfer`. Kind is part of it because a venue's withdrawal ids and trade ids are separate
  number spaces.
- **I6**: an event whose identity repeats with **equal** content is counted once. A repeat
  with different content raises `ConflictingEventError(ValueError)`. The database's unique
  constraints make that unreachable from stored rows, so it signals corrupted input.
- **Order**: sorted by `(occurred_at, source, external_id, kind)`, in that order. The first
  three are the issue's key. Kind breaks the tie only between different kinds with the same
  key. `external_id` compares as a plain string. Same-millisecond fills are therefore ordered
  deterministically, though not necessarily as the venue executed them (see *Risks*).

### Arithmetic

- No bare `+ - * /` on a `Decimal` anywhere in the package. Everything goes through
  `money.add`, `money.subtract`, `money.multiply` (all exact and unbounded, built on integers
  like `multiply` already is) and `money.divide`.
- `money.divide(dividend, divisor, scale)` returns the quotient rounded **once**, half to
  even, to `scale` places, using integer arithmetic. It refuses a zero divisor, and it raises
  `decimal.InvalidOperation` for a quotient beyond `MONEY_PRECISION`, the same type
  `quantize` raises. It is the only rounding in replay except `Adjustment` cost, which is
  `quantize(multiply(unit_cost, quantity), BASIS_SCALE)`.
- **Complements are computed by subtraction, never by a second rounding.** Whenever an amount
  is split, one part comes from `divide` and the other is `total - part`. That is what makes
  I8 (conservation) exact rather than approximate.
- The result does not depend on the ambient decimal context. A test replays inside
  `localcontext(prec=6, rounding=ROUND_UP)`.

### Pool operations (per non-cash asset X)

**acquire(X, known_qty, basis, unknown_qty)** adds to `Qk`, `C` and `Qu`.

**dispose(X, a, key)** with `a > 0`, where `T = Qk + Qu`. It returns
`(known_out, basis_out, uncovered)`:

- If `a >= T`, it takes everything: `known_out = Qk`, `basis_out = C`, and the pool is left at
  zero. `shortfall = a - T`. When `shortfall > 0`, it emits
  `NegativeInventory(key, X, shortfall)` and sets `HISTORY_INCOMPLETE` on X. **I1**: the
  quantity never goes below zero, and nothing raises.
- Otherwise it takes a proportional share of the known and unknown parts.
  `known_out = a` if `Qu == 0`, else `divide(multiply(a, Qk), T, QUANTITY_SCALE)`. The
  unknown part taken is `a - known_out`. `basis_out = C` when `known_out == Qk`, else
  `divide(multiply(C, known_out), Qk, BASIS_SCALE)`.
- `uncovered = a - known_out`, which is the unknown-basis part plus any shortfall.

**I4** follows from "take all": a pool whose known quantity reaches zero gives up all of its
basis, so `Qk == 0` implies `C == 0` exactly. The rounding residue of every earlier partial
disposal lands in the realized P&L of the disposal that empties the pool.

### Legs of a trade

For a `BUY`, the asset received is the base (`quantity`) and the asset given is the quote
(`quote_quantity`). For a `SELL`, it is the reverse. The fee then folds into one of three
places:

- **Fee asset is the received asset**: `received = amount - fee`, which must stay > 0. On a
  buy of BTC with the fee in BTC, the basis is unchanged and the quantity smaller, so the fee
  raises the unit cost.
- **Fee asset is the given asset**: `given = amount + fee`, which must stay > 0.
- **Otherwise, a third-asset fee leg** `(F, fee)`.

The **known value given** `V` is the value of the given leg plus the value of a third-asset
fee leg:

- A cash leg is worth its amount. A cash rebate (negative fee) is worth a negative amount.
- A non-cash given leg is worth `basis_out` from `dispose`. That is its **carried cost**, not a
  market price.
- A non-cash third-asset fee paid is disposed the same way, and it contributes its
  `basis_out`. Any `uncovered` part emits `UnattributedFee(key, F, uncovered, charged_to)` and
  sets `UNATTRIBUTED_FEE` on the `charged_to` position (none for a cash-to-cash trade).
- A non-cash third-asset **rebate** acquires `|fee|` of F at unknown basis. It adds nothing to
  `V`, and a lot is recorded.

The four shapes, each of which also yields the outcome I5 asks for:

| Given | Received | What happens |
|---|---|---|
| cash | non-cash X | **Buy.** `acquire(X, received, V, 0)`. A fee in cash raises basis by exactly the fee. |
| non-cash X | cash | **Sale.** `proceeds = received - V_fee`, where `V_fee` is the third-asset fee's value, so a sell fee reduces proceeds by exactly the fee. `known_out, basis_out, uncovered = dispose(X, given)`. `proceeds_known = proceeds` if `uncovered == 0`, else `divide(multiply(proceeds, known_out), given, BASIS_SCALE)`. `R += proceeds_known - basis_out` and `U += proceeds - proceeds_known`. |
| non-cash Y | non-cash X | **Swap, carry-over.** Y is disposed, and its `basis_out` plus the fee value becomes X's cost. No P&L is realized. `known_in = received` if `uncovered == 0`, else `divide(multiply(received, known_out), given, QUANTITY_SCALE)`. The rest is `unknown_in = received - known_in`. If `known_in > 0`, `acquire(X, known_in, V, unknown_in)`. If `known_in == 0`, `acquire(X, 0, 0, received)` and `V` is added to `unallocated_costs` (it has no known quantity to attach to). |
| cash | cash | **Conversion.** Both sides are pinned at 1, so no position changes. The fee's known value goes to `unallocated_costs`: its amount for a fee in any cash asset (folded or not), or its `basis_out` for a fee in a non-cash asset. |

`unallocated_costs` is a result-level total: known value that belongs to no position.

### Lots

`replay` emits one `Lot(asset, key, quantity, cost_basis, unknown_basis_quantity)` for every
acquisition into a non-cash asset: a trade's received leg, an `Adjustment`, and a third-asset
rebate. They are in event order, and the cost is as this method attributed it. The issue's
"populate the `lots` table from day one without reading it" is a persistence act, and so it is
#19's. #19 stores these with `METHOD`, so that a later FIFO function writes its own lots under
its own method into the same table without a migration.

### Result

- `Position(asset, quantity, unknown_basis_quantity, cost_basis, average_cost, realized_pnl,
  unmatched_proceeds, flags)`. `quantity = Qk + Qu`, and `cost_basis = C` is the basis of the
  known part only. `average_cost` is `divide(C, Qk, AVERAGE_COST_SCALE)`, or `None` when
  `Qk == 0`. It is derived for display and **never fed back** into `C`.
- There is one position for every non-cash asset touched by a `Trade` leg or an `Adjustment`,
  sorted by asset. A `Transfer` never creates one.
- `PositionFlag` (`StrEnum`):
  - `UNKNOWN_BASIS` is set while `Qu > 0`.
  - `HISTORY_INCOMPLETE` is set once any disposal of the asset fell short. It is sticky.
  - `UNATTRIBUTED_FEE` is set once a fee charged to the asset's trades could not be
    attributed. It is sticky.
- `warnings`: `NegativeInventory(key, asset, shortfall)` and
  `UnattributedFee(key, fee_asset, quantity, charged_to)`, in event order. `key` carries the
  timestamp that I1 requires.
- `AccountingResult(method, engine_version, input_fingerprint, positions, warnings, lots,
  unallocated_costs, event_count)`, where `event_count` is the count after deduplication.
- Warnings are **returned, never logged**. `domain` does not log.

### Fingerprint (I3)

`input_fingerprint` is the SHA-256 hex digest of canonical JSON with sorted keys and no
whitespace. It covers `METHOD`, `ENGINE_VERSION`, the sorted `cash_assets`, and every event
after deduplication, in replay order.

- An event is serialized as its kind, its key (`occurred_at` as UTC ISO-8601 with
  microseconds and a `Z`), and every field.
- An amount is serialized as `quantize(v, 18)` in fixed notation, with negative zero written
  as zero. Numerically equal inputs therefore fingerprint identically: `1`, `1.0` and `-0` vs
  `0`.
- `None` is JSON `null`.

**`ENGINE_VERSION` is in the fingerprint on purpose.** #19 skips a recompute when the
fingerprint is unchanged, so a fixed engine bug that left the fingerprint alone would leave a
wrong snapshot in place forever.

### Rejected alternatives

- **FIFO.** Under incomplete history, it matches disposals against specific wrong lots, and
  repairing it needs every missing lot. Weighted average spreads a missing buy over one
  average, and one opening balance repairs it. Neither method can see a gap that no sale
  exceeds. See the ADR.
- **State `(Q, average)` with basis derived as `average × Q`.** It makes I2 hold by
  construction, but every acquisition then rounds the average and leaks the residue.
  `(Q, C)` loses nothing, and I8 can be exact.
- **`fractions.Fraction` internally.** It is exact, but denominators grow with every partial
  disposal, which puts a slow recompute on the Pi at stake. It is used only as the tests'
  oracle.
- **Valuing swaps and third-asset fees at an execution-time market price.** No historical
  price source exists, and a current price presented as the price on the day is the failure
  #93 also refuses. Carried cost needs no price and conserves the money actually put in. See
  *Acceptance criteria*, I5.
- **Zero basis for an unknown-cost inflow.** It reports a fictitious profit on the next sale.
  `Qu` keeps such units out of the average and out of realized P&L.

## API contract

None. No endpoint changes. #19 exposes the result.

## Data model

None. No migration. The tables are #19's.

## Acceptance criteria

Copied from #17, with the interpretation where one was needed.

1. **`replay` is pure, enforced by the import contract.** A new `domain-is-pure` forbidden
   contract with source `portfolio.domain` forbids `sqlalchemy`, `httpx`, `fastapi`,
   `starlette`, `pydantic`, and the stdlib modules that do I/O or introduce nondeterminism:
   `os`, `io`, `pathlib`, `socket`, `ssl`, `subprocess`, `urllib`, `http`, `sqlite3`,
   `asyncio`, `threading`, `time`, `random`, `secrets`, `uuid`, `logging`. The review added
   `zoneinfo`, `tempfile`, `shutil`, `glob`, `multiprocessing`, `concurrent`, `ctypes`,
   `signal`, `select`, `selectors`, `platform` and `locale`. A planted-violation test proves
   the contract can fail. The clock is a call, not an import, because `datetime` is needed
   for the key. An AST test therefore forbids `now`, `utcnow`, `today` and `fromtimestamp`
   calls in `domain/`, and also an argument-less `astimezone()`, which reads the host's time
   zone. The builtins `open`, `print` and `input` need no import at all, so the same test
   forbids them too.
2. **I1, no negative inventory.** A violation emits a warning naming the asset and timestamp,
   and never crashes. Covered by `dispose` above. It is checked after every prefix of the
   event sequence, not only at the end.
3. **I2, `cost_basis_total == avg_cost * quantity` at every step.** *Interpretation*: in
   exact arithmetic this is the definition of the average. What can go wrong is a basis
   derived from a rounded average. So `C` is the state, the average is derived from it, and
   the test asserts that `|multiply(average_cost, Qk) - C| <= Qk × 5·10⁻¹⁹` (the average is
   the correctly rounded quotient) after every prefix. I8 asserts that the basis never leaks.
4. **I3, replaying identical events twice yields identical output, verified by an
   `input_fingerprint`.** This also holds for any permutation of the same events, and for
   numerically equal amounts written differently. Changing any single field of any event
   changes the fingerprint.
5. **I4, quantity zero forces cost basis exactly zero, and the residue goes to realized
   P&L.** Every position with `Qk == 0` has `cost_basis == 0`, and one with `quantity == 0`
   also has `unknown_basis_quantity == 0`.
6. **I5, fees.** Buy fees raise basis. Sell fees reduce proceeds. Third-asset fees are
   converted at execution-time price or explicitly flagged as unattributed, and never
   silently dropped. *Interpretation*: **there is no execution-time price in this system**,
   so a third-asset fee is converted at the fee asset's **carried average cost**, which is
   weighted average's own valuation of that asset. Any part that cannot be (the fee asset's
   inventory is short or of unknown basis) is flagged with `UnattributedFee` and
   `UNATTRIBUTED_FEE`. This departs from the issue's wording, and is recorded under
   "Decisions" in the pull request for the owner to reverse.
7. **I6, re-ingesting a fill changes nothing.** Duplicate events, including the whole log
   twice, give an equal result and an equal fingerprint.
8. **I7, a venue-to-wallet transfer changes neither total quantity nor total basis.**
   Inserting a `Transfer` anywhere leaves every position and warning unchanged.
9. **`hypothesis` property tests assert the invariants over random valid event sequences.**
   They cover I1–I7, plus **I8, conservation**: for every sequence,
   `Σ C − Σ R − Σ U + unallocated_costs == N + A`. Here `N` is the net cash put in by
   trades, and `A` is the summed known cost of `Adjustment`s. For a trade with a non-cash
   leg, `N` counts the cash given (after the fee fold), plus a third-asset fee paid in cash
   (signed, so a rebate counts negative), minus the cash received (after the fee fold). For
   a conversion, `N` counts only a fee paid in any cash asset, because both principal legs
   are pinned at 1. Exact equality holds with no tolerance. Replay is also shown to be
   independent of the ambient decimal context.
10. **A committed golden-file scenario of around 40 synthetic fills matches the expected
    output.** The expected values come from an independent `Fraction` oracle in the tests and
    are cross-checked by hand on round numbers. **They are never produced by running the
    engine under test.**
11. **`docs/accounting.md` has worked examples, including a third-asset fee.** Every worked
    example is also a test case, so the document cannot drift from the engine.
12. **An ADR records the weighted-average decision and the IRPF caveat**:
    `docs/adr/0001-weighted-average-cost-basis.md`.
13. **95% line and 90% branch coverage on `domain/`.** A step in `scripts/check.py` (full
    mode) and in CI, after the coverage run, fails below either figure for
    `src/portfolio/domain/`. The repository-wide 99.7 floor is unchanged.

## Test plan

| # | Criterion | Test |
|---|---|---|
| 1 | purity contract | `tests/test_import_contracts.py::test_domain_is_pure_*` (pinned text + planted `import socket` / `import sqlalchemy` in a shadow `portfolio.domain`) |
| 1 | no clock | `tests/security/test_domain_has_no_clock.py` (AST over `src/portfolio/domain/**`) |
| 2 | I1 | `tests/domain/accounting/test_invariants.py::test_i1_*` (property, every prefix) + `test_replay.py::test_sell_more_than_held_*` |
| 3 | I2 | `test_invariants.py::test_i2_average_is_correctly_rounded_quotient` |
| 4 | I3 | `test_invariants.py::test_i3_*` (twice, permutation) + `test_fingerprint.py` (field sensitivity, `1` vs `1.0`, `-0`, UTC normalisation, `ENGINE_VERSION` and config included) |
| 5 | I4 | `test_invariants.py::test_i4_*` + `test_replay.py::test_full_liquidation_residue_goes_to_realized` |
| 6 | I5 | `test_replay.py::test_fee_*` (quote, base, third asset carried, third asset unattributed, rebates, conversion) + `test_invariants.py::test_i5_fee_delta_*` (raising a cash fee by `d` raises basis / lowers realized by exactly `d`) |
| 7 | I6 | `test_invariants.py::test_i6_*` + `test_replay.py::test_conflicting_duplicate_raises` |
| 8 | I7 | `test_invariants.py::test_i7_transfer_changes_nothing` |
| 9 | I8 + context | `test_invariants.py::test_i8_conservation`, `::test_ambient_context_is_irrelevant` |
| 10 | golden | `tests/domain/accounting/test_golden.py` + `golden/scenario.json`, `golden/expected.json`; oracle in `tests/domain/accounting/oracle.py` |
| 11 | worked examples | `tests/domain/accounting/test_worked_examples.py`, one test per example, named after it |
| 12 | ADR | reviewed, not tested |
| 13 | coverage floor | `tests/test_import_contracts.py` (or a sibling) pins the step in `scripts/check.py` and `ci.yml` |
| — | validation | `tests/domain/accounting/test_events.py`: every refusal in *Events*, including `bool`/`float`/`int` amounts, 19 decimals, 21 integer digits, naive datetime, lone surrogate, blank fields, fee/fee-asset pairing, fee exceeding what is received |
| — | money helpers | `tests/domain/test_money.py`: `divide` single rounding (ties both ways, a value whose double rounding differs), zero divisor, precision refusal, context independence; `add`/`subtract` exact beyond 38 digits |

Property tests draw from a small universe (`BTC`, `KAS`, `BGB`, `USDT`, `USDC`), every
event kind, fees in every position (received, given, third cash, third non-cash, rebates),
and amounts at 18 decimals, so rounding paths are exercised rather than avoided.

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/domain/accounting/**` (new), `backend/src/portfolio/domain/money.py`, `backend/.importlinter`, `scripts/check.py`, `scripts/domain_coverage.py` (new), `.github/workflows/ci.yml` |
| tester | `backend/tests/**` |
| reviewer | nothing (reads, reports) |
| tech lead | this spec, `docs/accounting.md`, `docs/adr/0001-weighted-average-cost-basis.md`, `docs/architecture.md`, `backend/pyproject.toml` |

## Risks

- **The carried-cost reading of I5 departs from the issue's words.** It is a decision the
  owner may reverse. Reversing it needs a historical price source, which is its own issue.
- **Crypto-to-crypto swaps carry basis over.** Spanish tax treats a swap as a disposal at
  market value, and so does "realized" in most tax tools. The dashboard's realized P&L
  therefore differs from a tax figure by design. The ADR says so.
- **Cash pinned at 1 hides a depeg** and the USDC/USDT spread of a conversion. That is
  acceptable for a dashboard. A multi-fiat unit of account is out of scope.
- **Same-millisecond order is deterministic but arbitrary.** A buy and a sell of the same
  asset in the same millisecond, where the sell's id sorts first, show a transient shortfall
  and a `NegativeInventory` warning that the true order would not. It is rare for a personal
  account, and a warning rather than a wrong number.
- **`import-linter` and stdlib modules.** This was confirmed to work, with import-linter
  2.15 and grimp 3.17, when the contract was written. `import-linter` only checks a forbidden
  external that is already in the graph, so a stdlib name that nothing imports passes
  silently. The planted-violation test is therefore what proves the contract, not a green
  run.
- **The engine's range is 10²⁰ cash units.** That is the same range as the `NumericText(18)`
  columns #19 writes to. The average cost and `Adjustment` cost are guarded (see *Rulings*).
  A basis or proceeds total past 10²⁰ is not, and as few as one absurd fill reaches it: a
  buy of 1 BTC for 9E19 USDT with a 9E19 USDT fee returns a basis of 1.8E20. `add` returns
  such a value unbounded, #19's write refuses it, and the next proportional `divide` raises
  `InvalidOperation`, which quotes no amount. No personal portfolio reaches that total, and
  the property strategies stay inside it.
- **A gap that no sale exceeds is invisible to replay.** Suppose an early buy is older than
  the venue's retention and the owner still holds it. Every sale then fits inside the
  recorded pool, so nothing warns, and the average and realized P&L are computed without that
  buy. Any method has this blind spot, since what is missing leaves no trace in the events.
  Only a comparison of replay's quantity with the balances actually held can surface it,
  which #19 should consider.
- **Bitget's BGB fee deduction** is the likely real-world third-asset fee. What Bitget reports
  in `feeDetail` for it is undocumented (spec 014). If the provider records BGB fees, they
  land in `UnattributedFee` unless BGB was bought through a fill.

## Rulings during implementation

The tester's oracle surfaced these. Each is binding on the engine and the oracle alike.

- **R1. The average cost never raises.** `average_cost` is `None` when `Qk == 0`, and also
  when the quotient does not fit `AVERAGE_COST_SCALE` within `MONEY_PRECISION` (10²⁰ or
  more cash units per unit). Validated input reached this: a buy of 1E-18 BTC for 100 USDT,
  or a fee in the received asset leaving 1E-18. The average is a display figure, and the
  basis and quantity beside it are still reported.
- **R2. `Adjustment` refuses a cost it cannot represent.** When
  `quantize(multiply(unit_cost, quantity), BASIS_SCALE)` does not fit, construction raises
  `ValueError` naming the rule, not the amounts. For example, 1E19 at a unit cost of 1E19
  passes each amount's own rule and would otherwise raise out of `replay`.
- **R3. `charged_to`.** An `UnattributedFee` names the non-cash principal of the trade: the
  received asset for a buy or a swap, the given asset for a sale, and `None` for a
  conversion.
- **R4. Order of work inside one trade**, which fixes the order of warnings and lots:
  1. dispose the given leg;
  2. then the third-asset fee leg (a disposal, or a rebate acquisition);
  3. then acquire the received leg.
- **R5. `Lot.quantity` is the total received**, with `unknown_basis_quantity` beside it, as
  on `Position`. An unknown-cost acquisition (an `Adjustment` without a cost, a third-asset
  rebate, or a swap with `known_in == 0`) is a lot with `cost_basis == 0` and all of its
  quantity unknown.
- **R6. I8's `A` counts adjustments of non-cash assets only.** An `Adjustment` of a cash asset
  changes nothing, so it is outside both sides of the equation.
- **R7. A third-asset cash rebate larger than the trade is accepted.** It can drive the basis
  negative. `Trade` cannot refuse it at construction, because what counts as cash is
  configuration. No venue pays that, and every invariant still holds.
- **R8. A fee asset may be named beside a zero fee**, as `NormalizedFill` allows. The rule
  becomes: `fee_asset` is required when the fee is not zero, and a zero fee creates no leg
  and touches no position.

  **Correction from the review:** a stored fill still does not always convert. `Trade`
  refuses three shapes that `NormalizedFill` accepts:
  - `base_asset == quote_asset`;
  - a fee in the asset received that consumes everything received;
  - a rebate in the asset given that exceeds what was given.

  The refusals stay, because the engine cannot account for any of them. No venue is known to
  produce them. Refusing them at ingestion, so that the stored log never holds a row replay
  cannot read, is #99. #19 depends on it.
- **R9. The fractional-digit rule is value-based**, as in `NormalizedFill`: the amount is
  refused when `quantize(v, 18) != v`. So `1.0000000000000000000` is accepted and
  `0.0000000000000000001` is refused.
- **R10. Kind strings** are `"adjustment"`, `"trade"` and `"transfer"`, compared as strings in
  the tie-break. The fingerprint's JSON key names are the engine's to choose. Fingerprints are
  tested by their properties, not against a stored digest.
