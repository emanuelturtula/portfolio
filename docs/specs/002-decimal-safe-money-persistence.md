# 002 — Decimal-safe money persistence and domain money helpers

Issue: #2
Status: implementing

## Problem

The schema from #1 has no way to store a monetary value. `sqlalchemy.Numeric` on SQLite
round-trips through `float` and silently destroys precision, so the obvious answer is the
one answer that is forbidden. Every later issue — exchange fills, cost basis, invested per
asset — writes money, so the representation has to be settled and mechanically enforced
before any of them start, not after.

Today the float ban in `CLAUDE.md` rule 2 is documentation only: it cites an AST test in
`backend/tests/security/`, and that directory does not exist.

## Scope

- `NumericText`, a `TypeDecorator` storing a canonical `Decimal` string in a `TEXT` column.
- `BaseUnits`, an integer type for on-chain quantities (satoshis, sompi).
- `domain/money.py`: the decimal context, and base-unit conversion in both directions.
- `MoneyStr`, a Pydantic annotation that serializes `Decimal` as a JSON **string**.
- The AST test that makes the float ban real.
- `docs/architecture.md`.
- One layering contract change, described below.

## Non-goals

- **No table gets a money column.** Nothing has money to store yet; holdings, fills and
  valuations arrive in M2 and M3. Adding a speculative column now would mean guessing its
  scale, and the scale is part of the column's meaning.
- **No migration.** Follows from the above.
- **No shipped endpoint returns money.** The `MoneyStr` API-level test mounts a throwaway
  router on a throwaway app, so `frontend/src/api/generated/schema.ts` is untouched and the
  OpenAPI drift job stays green.
- **No frontend work.** The ESLint rule banning `parseFloat` / `Number()` / `parseInt` on
  money already exists in `frontend/eslint.config.js`. `decimal.js` becomes a dependency
  when something actually renders money, which is #4 at the earliest.
- **No aggregation helpers, no rounding policy for display.** Summing in Python is a
  repository and service concern; display rounding is a frontend one.

## Design

### Layering: `domain` moves strictly below `db`

The contract in `backend/.importlinter` currently reads:

```
portfolio.repositories | portfolio.providers
portfolio.db | portfolio.domain
```

Items on one line are *independent* to import-linter: siblings may not import each other.
That makes `portfolio.db -> portfolio.domain` a violation, and it is exactly the import
this change needs — `NumericText` must quantize using the same rule `domain/money.py`
defines, or "how money rounds" ends up defined in two places and drifts.

The contract becomes:

```
portfolio.repositories | portfolio.providers
portfolio.db
portfolio.domain
```

`domain` still imports nothing, which is the property that actually matters. Persistence
depending on the domain vocabulary is the right direction; the reverse would not be.
`CLAUDE.md` rule 4 carries the same diagram and is updated with it.

*Rejected:* duplicating the two-line quantization in `db/types.py`. It is small enough to
look harmless, which is what makes it dangerous — a future change to the rounding mode
would land in one copy.

### `NumericText(scale)`

`impl = Text`. Scale is a **required** constructor argument: a money column without a
declared scale has no defined rounding, and a default would let one be omitted by accident.

On bind:

| Input | Behaviour |
|---|---|
| `None` | `None` |
| `Decimal` | quantized to `scale` with `ROUND_HALF_EVEN`, rendered fixed-point |
| `int` | exact, converted to `Decimal` |
| `bool` | **`TypeError`** — `bool` is an `int` subclass, and `True` is not an amount |
| `float` | **`TypeError`**, before any conversion can hide the damage |
| `Decimal("NaN")`, `Decimal("Infinity")` | **`ValueError`** |
| anything else | **`TypeError`** |

The stored form is `format(quantized, "f")`: never scientific notation, always exactly
`scale` decimal places, so every value in a column has the same shape. Negative zero is
normalised to zero, otherwise a column has two spellings of the same amount.

On result: `Decimal(value)`.

`cache_ok = True` with a constructor argument means SQLAlchemy folds `scale` into the
type's cache key. That is asserted rather than assumed — a shared cache entry between two
scales would round values to the wrong place with no error.

*Rejected:* a zero-padded, offset-encoded form that sorts correctly as text. It would make
`ORDER BY` on a money column work, which is precisely the thing rule 2 forbids; making the
wrong approach ergonomic is worse than leaving it broken.

### `BaseUnits`

`impl = BigInteger`, holding integer base units. On-chain APIs return integers, and a
satoshi or a sompi is the indivisible unit — there is nothing to round.

Rejects `float`, `bool` and any non-integer, and rejects values outside signed 64-bit
range, which is what SQLite stores. The limit is not theoretical for every chain: 21M BTC
is 2.1e15 satoshis and Kaspa's supply is 2.87e18 sompi, both comfortable, but an
18-decimal EVM token would not be. V1 has no EVM chain; the docstring records the ceiling
so the next person meets it as a documented limit rather than as silent truncation.

The `decimals` column on `assets` from #1 is the exponent these values are interpreted
with.

### `domain/money.py`

```python
MONEY_PRECISION = 38
MONEY_ROUNDING = ROUND_HALF_EVEN

def quantize(value: Decimal, scale: int) -> Decimal
def to_base_units(amount: Decimal, decimals: int) -> int
def from_base_units(units: int, decimals: int) -> Decimal
```

At import the module sets **both** `decimal.getcontext().prec = 38` and
`decimal.DefaultContext.prec = 38`. `getcontext()` is thread-local: setting it alone
configures the importing thread and nothing else, so a value computed inside
`anyio.to_thread.run_sync` — which #1 already uses for migrations — would silently run at
the default precision of 28. `DefaultContext` is the template new threads copy. Both are
asserted, and the thread case is asserted from an actual thread.

Mutating a global at import is a side effect in a module that is otherwise pure. It is the
issue's explicit instruction and it is the only way to make the guarantee process-wide;
the alternative, passing a context into every call, puts the burden on every future caller
and fails open when one forgets.

### `MoneyStr`

In `api/schemas/money.py`:

```python
MoneyStr = Annotated[Decimal, BeforeValidator(...), PlainSerializer(...), WithJsonSchema(...)]
```

Serializes with `format(value, "f")` to a JSON string. Validation accepts a string or an
integer and **rejects a JSON number that parsed as `float`** — a client sending
`{"amount": 0.1}` is already lossy by the time Pydantic sees it, so accepting it would
launder the error rather than report it. `WithJsonSchema` pins `type: "string"` so the
generated TypeScript sees a string, which is what the frontend ESLint rule assumes.

### The AST test

`backend/tests/security/test_no_float.py` parses every module under `domain/`, `services/`
and `providers/` and fails on an `ast.Constant` holding a `float`, or a call to the builtin
`float`. The report names file and line; a test that says only "a float exists somewhere"
costs more time than it saves.

There is no allowlist. If one is ever genuinely needed it should be an argued exception in
a pull request, not a list that quietly grows.

`backend/tests/security/__init__.py` is required: mypy is strict over `tests`, and without
package markers a second `conftest.py` collides as a duplicate module and mypy stops
checking everything. That lesson is from #1.

## API contract

None shipped. `MoneyStr` is exercised by a router defined in the test module and mounted on
a `FastAPI()` built inside the test, so no operation is added to the real schema and
`frontend/src/api/generated/schema.ts` must come out of the drift job unchanged.

## Data model

**No table, column, constraint, index or migration changes.** The types land, ready for the
first column that needs them.

Tests that need a mapped class must define it on their **own** `MetaData` / declarative
base, never on `portfolio.db.base.Base`. A test model attached to `Base` pollutes
`portfolio.db.models.metadata` and breaks `test_models_and_migrations_have_not_drifted`
from #1 — which would look like a drift bug rather than a test-isolation bug.

## Acceptance criteria

Copied from the issue.

1. `NumericText` round-trips `Decimal("0.000000012345678901")` exactly.
2. Binding a `float`, `NaN` or `Infinity` raises rather than being silently coerced.
3. Quantization uses `ROUND_HALF_EVEN` to the column's declared scale.
4. `domain/money.py` sets `decimal.getcontext().prec = 38` at import, asserted by a test.
5. A property test shows base-unit conversion round-trips for all valid inputs.
6. A Pydantic `MoneyStr` annotation serializes `Decimal` as a JSON string, proven by an
   API-level test.
7. An AST test fails the build on any `float(` call or float literal in `domain/`,
   `services/` or `providers/`.
8. `docs/architecture.md` records why `Numeric` is forbidden and why money is never
   aggregated in SQL.

**Interpretation of 1.** "Exactly" means the round-tripped value satisfies both `==` and
`str()` equality. Testing `==` alone would pass for `Decimal("0.10")` against
`Decimal("0.1")`, which is the mistake this criterion is guarding against.

The stored form is padded to the column's declared scale, so `str()` equality holds when
the column's scale **equals** the value's own scale — `Decimal("0.000000012345678901")` has
18 decimal places and round-trips identically through `NumericText(18)`. Through
`NumericText(20)` it comes back as `Decimal("1.234567890100E-8")`: equal in value, padded
in form. That is deliberate and is what `DECIMAL(p, s)` does in every other database: the
scale is part of the column's meaning, uniform text is what makes an equality lookup or a
unique constraint on a money column mean anything, and it is the same argument as
normalising `-0`. The test therefore uses a column whose scale matches the value.

**Interpretation of 3.** Asserted at the boundary cases ROUND_HALF_EVEN exists to
disambiguate — `0.5` rounding to `0` and `1.5` rounding to `2` at the declared scale — not
merely by reading the constant back out of the module.

**Interpretation of 5.** `hypothesis`, added as a dev dependency. "All valid inputs" is
read as: any `Decimal` representable at `decimals` places, for `decimals` in the range the
`assets` table admits, in both directions, plus the explicit rejection of an amount with
more precision than `decimals` can hold.

**Interpretation of 6.** Proven through a real HTTP round trip over ASGI — a response body
parsed from raw JSON text and asserted to be a string, not `response.json()` compared to a
number, which would hide the difference Python re-parses away.

**Interpretation of 7.** The test must itself be proven able to fail: a companion feeds it
a synthetic module containing a float and asserts it is reported, so the day someone
narrows the walk to the wrong directory the failure is visible.

## Test plan

| # | Criterion | Test |
|---|---|---|
| 1 | exact round trip, `==` and `str()` | `backend/tests/db/test_money_types.py::test_numeric_text_round_trips_a_high_precision_decimal` |
| 1 | the value survives a real column, not just the type | `::test_numeric_text_round_trips_through_a_real_column` |
| 2 | a `float` is rejected | `::test_numeric_text_rejects_a_float` |
| 2 | `NaN` and `Infinity` are rejected | `::test_numeric_text_rejects_nan_and_infinity` (parametrized) |
| 2 | a `bool` is rejected although it is an `int` | `::test_numeric_text_rejects_a_bool` |
| 3 | `ROUND_HALF_EVEN` at the declared scale | `::test_numeric_text_quantizes_half_to_even` (parametrized on the tie cases) |
| 3 | the scale is part of the type's cache key | `::test_two_scales_do_not_share_a_cache_key` |
| 3 | negative zero is normalised | `::test_numeric_text_normalises_negative_zero` |
| — | `BaseUnits` rejects float, bool and a fractional value | `::test_base_units_rejects_a_non_integer` (parametrized) |
| — | `BaseUnits` rejects an out-of-range value | `::test_base_units_rejects_a_value_sqlite_cannot_hold` |
| 4 | the context precision is 38 at import | `backend/tests/domain/test_money.py::test_the_decimal_context_precision_is_38` |
| 4 | …and in a thread that imported nothing | `::test_a_worker_thread_inherits_the_precision` |
| 5 | base units round-trip both ways | `::test_base_unit_conversion_round_trips` (hypothesis) |
| 5 | an amount finer than `decimals` is rejected | `::test_to_base_units_rejects_unrepresentable_precision` |
| 6 | `MoneyStr` serializes as a JSON string over HTTP | `backend/tests/api/test_money_schema.py::test_money_is_serialized_as_a_json_string` |
| 6 | the OpenAPI schema types it as a string | `::test_money_is_typed_as_a_string_in_the_schema` |
| 6 | a JSON float is rejected on input | `::test_a_json_number_is_rejected` |
| 7 | the float ban holds across the three packages | `backend/tests/security/test_no_float.py::test_no_float_in_the_pure_layers` |
| 7 | the ban can fail, and names file and line | `::test_the_float_ban_reports_a_synthetic_violation` |
| 8 | `docs/architecture.md` exists and covers both rules | `backend/tests/security/test_no_float.py::test_architecture_documents_the_money_rules` |
| — | the real schema is unchanged | existing `test_openapi.py`, `test_migrations.py::test_models_and_migrations_have_not_drifted` |

## File ownership

Disjoint. Nobody writes a path outside their row.

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/db/types.py`, `backend/src/portfolio/domain/**`, `backend/src/portfolio/api/schemas/**`, `backend/.importlinter`, `backend/pyproject.toml`, `backend/uv.lock`, `docs/architecture.md` |
| tester | `backend/tests/**` |
| reviewer | nothing |
| tech lead | `docs/specs/002-decimal-safe-money-persistence.md`, `CLAUDE.md` |

`backend/src/portfolio/db/models.py`, `engine.py`, `migration_guards.py` and everything
under `migrations/` are **out of bounds** this time: no column changes, so nothing there
needs to move.

## Risks

- **The layering change is the one thing here that a reviewer should push back on if they
  disagree.** It is one line in `.importlinter` and one diagram in `CLAUDE.md`, and the
  alternative — duplicating the quantization — is a real option, just a worse one.
- **`getcontext()` being thread-local is the subtle one.** If `DefaultContext` turns out
  not to propagate the way this plan assumes, the guarantee is weaker than criterion 4
  implies and the spec is wrong rather than the code. Test it from a real thread and report
  what you actually observe.
- **`cache_ok = True` on a parameterized type is a known footgun.** If SQLAlchemy does not
  fold `scale` into the cache key the way this plan assumes, the correct response is
  `cache_ok = False` with a comment, not a test that asserts the broken behaviour.
- **`hypothesis` will find edge cases this plan has not thought about** — that is why it is
  here. If it finds one that implies a design change rather than a bug fix, stop and say so.
- Nothing here depends on an external API, so there is nothing to verify against vendor
  documentation.
