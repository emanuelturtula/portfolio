# Architecture

How the backend is arranged, and the two decisions that are easiest to undo by accident:
how money is represented, and which layer may import which.

## Money

### The representation, end to end

| Where | Representation |
|---|---|
| Python | `decimal.Decimal` |
| SQLite | `TEXT`, via the `NumericText` type decorator |
| On-chain quantities | `INTEGER` base units (satoshis, sompi) plus a `decimals` column |
| Over the wire | a JSON **string** |
| TypeScript | a `string`, formatted with `decimal.js` |

One rule underneath all five rows: a monetary value never becomes a binary floating point
number, at any point in its life. `float` is banned outright in `domain/`, `services/` and
`providers/`, and an AST test in `backend/tests/security/` fails the build on a float
literal or a `float(` call in any of them.

The context precision is 38 significant digits, and the rounding mode is
`ROUND_HALF_EVEN`. Both are set once, in `portfolio/domain/money.py`, which also sets
`decimal.DefaultContext` and not only `decimal.getcontext()` — the latter is thread-local,
and this process does real work in worker threads.

### Why `sqlalchemy.Numeric` is forbidden

SQLite has no decimal storage class. Its numeric affinity is an 8-byte IEEE-754 `double`,
and `sqlalchemy.Numeric` on SQLite converts through exactly that on the way in and on the
way out.

Measured on this project's SQLAlchemy version, against an in-memory SQLite database with a
`Numeric(38, 20)` column:

```
wrote   12345678901234567890.12345678901234567890
read    12345678901234567168.00000000000000000000
```

`typeof()` on the stored value reports `real`. No exception, and — checked, not assumed —
no warning either: the value is written, accepted, and returned as a `Decimal` of the
declared scale, so every type annotation in the codebase still reads correctly while 722
units of the amount have evaporated. A smaller value may survive the round trip intact,
which is worse rather than better: the corruption depends on the magnitude, so it passes
the first test anyone writes and fails in production. It surfaces months later as a total
that is off, with no commit to blame.

`NumericText` stores the canonical fixed-point string instead — `format(value, "f")`,
never scientific notation, always exactly the column's declared scale, one spelling of
zero. SQLite stores that byte for byte and hands it back unchanged, and `Decimal(text)`
reconstructs the exact value. The scale is a required constructor argument, because a
money column without a declared scale has no defined rounding.

On-chain quantities do not use it. A satoshi and a sompi are indivisible and every chain
API reports them as integers, so `BaseUnits` stores the integer count and `assets.decimals`
records the exponent to read it with. There is nothing to round, so nothing can round
wrongly. The ceiling is SQLite's signed 64-bit integer; `BaseUnits` rejects anything past
it rather than truncating.

### Why money is never aggregated in SQL

Because a money column is `TEXT`, and every SQL operation that treats it as a number
coerces it to a float first:

- `SUM(amount)` and `AVG(amount)` apply numeric affinity, which is the `double` again —
  the exact thing `NumericText` exists to avoid, reintroduced in the one place where it
  is applied to every row at once.
- `ORDER BY amount` on `TEXT` sorts lexicographically, so `"9.00"` sorts after `"10.00"`.
  Casting it to make the sort correct is the float coercion again.
- `WHERE amount > :threshold` has both problems: a string comparison that is not a
  numeric one, or a cast that is lossy.

So: **load the rows and aggregate in Python**, with `Decimal`. No `SUM()`, no `ORDER BY`,
no `<` or `>` on a money column. Sorting by value is done in Python too, on the `Decimal`
values, after loading.

The cost of that is the reason it is affordable here: one user, thousands of rows. A
portfolio with a decade of daily fills is a table of tens of thousands of rows on a
machine with gigabytes of memory, and reading all of them costs milliseconds. Trading
correctness for a performance gain nobody would notice is a bad trade, and a product whose
core output is a set of numbers cannot afford numbers that are almost right.

`NumericText` deliberately does not make this ergonomic. A zero-padded, offset-encoded
storage form that sorted correctly as text was considered and rejected: it would make
`ORDER BY` on a money column work, and making the forbidden approach comfortable is worse
than leaving it visibly broken.

## Layering

```
{ api.routers , cli } -> services -> { repositories , providers } -> db -> domain
domain                -> nothing
```

Enforced by `import-linter` contracts in `backend/.importlinter`, run in CI and by
`python scripts/check.py`.

- **`api.routers`** parse a request, call a service, serialize the result. They may not
  import `repositories`, `providers`, `sqlalchemy` or `httpx`; a separate `forbidden`
  contract says so by name, because "thin" is otherwise a matter of opinion.

  What makes that hold in practice is that a router is never handed a database session.
  `api.dependencies` opens the session, builds the service around it and yields the
  service; the router's signature mentions neither `AsyncSession` nor a repository, so
  the contract has nothing to catch because there is nothing to write.
- **`cli`** is a sibling of `api`, not a layer above it. Both are entry points onto the
  same services, and neither imports the other. `python -m portfolio create-user` and the
  login endpoint reach the same password policy and the same hasher, which is the point:
  an account created from the command line and one created by the bootstrap path cannot
  end up under different rules.
- **`services`** hold the business logic and may not import `fastapi`. A service that
  raises `HTTPException` has put an HTTP concern in the only layer that should be
  testable without one.
- **`repositories` and `providers`** are independent of each other: the database side and
  the outside-world side never import across.
- **`db`** sits directly above `domain`. This is the one place a persistence concern is
  allowed to depend on the domain vocabulary, and it exists so that `NumericText` rounds
  by `domain.money.quantize` rather than carrying a second copy of the rounding rule.
  Two copies of "how money rounds" is how the two stop agreeing.
- **`domain`** imports nothing from the application: no I/O, no clock, no network, no
  ORM. The current time is passed in as an argument. That is what makes a domain
  calculation reproducible from its inputs alone, in a test and in an incident.

## Authentication

Single user, one password, opaque server-side sessions. No JWT and no refresh tokens: a
stateless token cannot be revoked, and revocation is the only session behaviour this
product actually needs.

Two hashes, for two different threats:

- **Argon2id** over the password, because a password is low entropy and guessable, so the
  hash has to be slow and memory-hard. The cost parameters are settings, floored at the
  OWASP minimum in production, and measured on the deployment hardware rather than
  copied — see `docs/operations.md`.
- **SHA-256** over the session token, because the token is 32 bytes from
  `secrets.token_urlsafe` and no amount of offline work recovers 256 bits of entropy. The
  hash only has to be preimage resistant. Running Argon2id per request would add a quarter
  of a second to every page load for no gain.

Only the hash is stored, so a leaked database file yields no usable session.

Two expiries apply at once: a sliding 7-day idle window and a hard 30-day ceiling that
activity never extends. Whichever comes first ends the session.

Authorization is **deny-by-default, in middleware**. Any path under `/api` outside
`PUBLIC_API_PATHS` requires a session. A `Depends` on each router would be the more
conventional shape and is the wrong one here: forgetting it on one endpoint is the exact
failure mode, and a rule that can be forgotten is a rule that eventually is. A contract
test walks every registered route and asserts `401` without a cookie.

The same middleware enforces, on every non-GET request, a matching `Origin` and a
`Content-Type` of `application/json`. The second is the one that does the work: a
form-encoded POST is the shape an HTML form can send cross-site without a preflight, so
refusing it closes CSRF without a token round-trip. `SameSite=Lax` on the cookie is the
belt to that pair of braces.

See `docs/specs/003-single-user-password-login.md` for the decisions and what they cost.

## Related documents

- `CLAUDE.md` — the working agreement, and the enforcement behind each rule.
- `docs/operations.md` — running the instance: the account, the password hash, sessions.
- `docs/specs/` — the per-issue implementation specs.
