# 020 — Refuse at ingestion the fills the accounting engine cannot replay

Issue: #99
Status: implementing

## Problem

`NormalizedFill` stores three fill shapes that the engine's `Trade` refuses (spec 019, R8):

- a fill whose base and quote asset are the same;
- a fee paid in the asset received that consumes everything received;
- a rebate paid in the asset given that is at least everything given.

The fill log is append-only. Once such a row is stored it stays, and #19's recompute cannot
build its event list: every position becomes uncomputable because of one row. No venue is
known to send these shapes. The refusal still belongs where a venue's answer becomes a row,
not in the middle of a recompute.

## Scope

- A **single definition** of what makes a trade's shape unaccountable, in
  `portfolio.domain.accounting`. Both `Trade` and `NormalizedFill` use it, so the two cannot
  drift apart again.
- `NormalizedFill.__post_init__` refuses the three shapes with `ExchangeSchemaError`. It names
  the rule and never an amount, like its other refusals.
- A property test holding the real invariant: **every fill `NormalizedFill` accepts converts
  to a `Trade`**, over strategies that span everything `NormalizedFill` accepts.
- `docs/providers.md` lists the new refusals.

## Non-goals

- **Rows already stored.** No stored fill is known to have these shapes. What #19 does with a
  stored row that predates this check and still does not convert is #19's to implement. The
  policy this spec recommends is under *For #19*.
- **Any change to the engine's semantics.** `Trade` keeps its refusals and its messages.
- **The fill-to-`Trade` mapper.** It is #19's. The property test builds the `Trade` inline
  from the fill's fields.

## Design

In `backend/src/portfolio/domain/accounting/events.py` (re-exported from the package):

```python
class TradeShapeProblem(StrEnum):
    SAME_ASSET = "same_asset"
    FEE_CONSUMES_RECEIVED = "fee_consumes_received"
    REBATE_EXCEEDS_GIVEN = "rebate_exceeds_given"

def trade_shape_problem(
    *, base_asset, quote_asset, side, quantity, quote_quantity, fee_amount, fee_asset
) -> TradeShapeProblem | None: ...
```

- The function is pure and assumes its arguments already passed each type's own field rules:
  finite `Decimal`s within the amount rule, non-blank text, and a `FillSide`.
- It compares at `AMOUNT_SCALE`, exactly as `Trade` does today (commit d24ef91).
- A zero fee is no leg (R8), so it never produces a fee problem.
- `Trade.__post_init__` calls it after its field checks and raises `ValueError` with **the
  same messages it raises today**, so no existing test changes.
- `NormalizedFill.__post_init__` calls it after its own field checks and raises
  `ExchangeSchemaError` with its own detail text. That text names `base_asset`/`quote_asset`,
  or `fee_amount`, and the rule, never a value.

`providers.exchanges.base` importing `portfolio.domain.accounting` is legal: `providers` sits
above `domain`. It is also the right direction, since what the log must hold is decided by
what the ledger can read.

**Rejected:** copying the three checks into `NormalizedFill`. Two copies of a rule are how
spec 019's R8 claim went false in the first place.

## For #19

Criterion 3 of #99 is carried here as a recommendation #19 must implement or explicitly
overrule. A stored row that does not convert to a `Trade` must **fail the recompute loudly**,
with an error that carries the exchange account and the fill's `external_trade_id` **as
attributes, never in its message**. That follows the pattern of `ConflictingEventError`,
because a message is what gets logged and stored as a sync run's `detail`, and this
codebase keeps trade ids out of both. The row must not be skipped silently: a skipped fill
is a position that is wrong with nothing to say so, which is exactly the
confident-wrong-number failure the engine was built to avoid. The existing snapshot then
stays as it was.

No stored row is known to have these shapes:

- Fills arrived through `NormalizedFill`, where no venue's fee parsing can produce them.
- The one-time backfills were loaded with a zero fee, which rules out both fee shapes.
- A base equal to its quote is not a real pair.

The first recompute #19 runs is what would surface an exception.

## API contract

None.

## Data model

None. No migration.

## Acceptance criteria

1. `NormalizedFill.__post_init__` refuses the three shapes with `ExchangeSchemaError`, naming
   the rule and never the amounts, like its other refusals.
2. A test builds each shape as a `NormalizedFill` (refused). A property test shows that every
   fill `NormalizedFill` accepts converts to a `Trade`, over the same strategies.
   *Interpretation*: those are strategies spanning everything `NormalizedFill` accepts. If
   the property finds a further discrepancy, the tester reports it and the fix is decided
   against this spec: refuse it in `NormalizedFill` if the engine cannot account for it.
3. #19's mapper documents what it does with a stored row that predates this check. It is
   recorded under *For #19* above, and #19's spec must pick it up.

## Test plan

| # | Criterion | Test |
|---|---|---|
| 1 | three refusals, no amount in the message | `backend/tests/providers/exchanges/test_base.py::test_normalized_fill_refuses_*` (same asset; fee in the received asset equal to it and above it, for both a buy and a sell; rebate in the given asset equal to it and above it) + a check that the detail quotes no amount |
| 1 | boundaries still accepted | a fee one unit (1E-18) below the received quantity; a rebate one unit below the given; a zero fee naming the received asset |
| 2 | every accepted fill converts | `backend/tests/providers/exchanges/test_fill_converts_to_trade.py` (hypothesis) |
| — | one definition | `backend/tests/domain/accounting/test_events.py`: `trade_shape_problem` returns each member for its shape and `None` otherwise; `Trade`'s messages are unchanged |

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/domain/accounting/events.py`, `backend/src/portfolio/domain/accounting/__init__.py`, `backend/src/portfolio/providers/exchanges/base.py`, `docs/providers.md` |
| tester | `backend/tests/**` |
| reviewer | nothing |
| tech lead | this spec |

## Risks

- **A venue that does send one of these shapes stops its account's sync.** The page fails
  with a schema error, the account goes to `error`, and a person must look. That is the
  existing contract for any fill the application cannot account for, and it is the point: the
  alternative is a log that breaks every recompute after it.
- **The property test is only as wide as its strategies.** It must include fees in every
  position, rebates, zero fees naming an asset, 18-place and 20-integer-digit amounts,
  timestamps across the whole range `executed_at` accepts, and text at the edges of the UTF-8
  rule.

## Rulings during implementation

- **R1. A fourth shape: an `executed_at` that cannot be expressed in UTC.** Examples are
  `datetime.min` at +05:00 and `datetime.max` at −05:00. Such a value is timezone-aware, so
  `NormalizedFill` accepted it. `EventKey` refuses it, and the `UtcDateTime` column's bind
  would raise a bare `OverflowError`, outside the error taxonomy. No venue sends one, because
  `datetime_from_epoch_ms` always returns UTC. Under criterion 2's decision rule,
  `NormalizedFill` now refuses it with `ExchangeSchemaError`
  ("executed_at is outside the range a UTC datetime can represent"). The stored value stays
  as given. The backend developer found it by reading, before the property test ran.
