# 001 — SQLAlchemy foundation, SQLite pragmas and Alembic migrations

Issue: #1
Status: done

## Problem

The backend has no database layer at all: `portfolio/db/` contains a docstring and nothing
else. Every issue in this milestone — money persistence, authentication, the wallet
registry — needs an async engine, a declarative base and a migration tool before it can
write a single table. Two SQLite defaults also have to be corrected here or they become
invisible bugs later: foreign keys are **off** unless enabled per connection, and SQLite
cannot `ALTER COLUMN`, so any migration that changes a column is impossible without
Alembic's batch mode.

## Scope

- An async engine factory with a `connect` listener that applies the SQLite pragmas.
- A declarative `Base` carrying a `MetaData` naming convention, so every index and
  constraint has a deterministic name that a batch migration can refer to.
- A `UtcDateTime` type decorator that stores timezone-aware UTC and rejects naive values.
- The `users`, `sessions` and `assets` tables, and a seed migration for `assets`.
- Alembic configured for async with `render_as_batch=True`, living **inside** the
  installed package so the Docker image ships it.
- A migrations-versus-models drift check that runs in CI.
- Test fixtures backed by a real file on disk under `tmp_path`.
- Application startup runs `alembic upgrade head` and owns the engine lifecycle.

## Non-goals

- **No repositories, services or routers.** Nothing reads or writes these tables yet.
  `users` and `sessions` are populated by #3, `assets` is consumed from #2 onward.
- **No `NumericText` or `BaseUnits`.** Money persistence is #2 and lands on top of this
  base. No column in this change holds a monetary value, so the question does not arise.
- **No password hashing, no session issuance, no CLI.** All of that is #3. This change
  creates the columns those features will fill, nothing more.
- **No multi-user support.** `sessions` carries a `user_id` foreign key because #5 already
  specifies `UNIQUE(user_id, chain_key, address_canonical)`, but the product is single
  user and nothing here creates or exercises a second row.
- **No connection pool tuning, no backup logic.** The host-side deployment script already
  backs up SQLite before replacing a container.

## Design

### Layout

```
backend/alembic.ini                                  # developer CLI entry point only
backend/src/portfolio/db/
    base.py                 Base, NAMING_CONVENTION
    types.py                UtcDateTime
    models.py               User, Session, Asset
    engine.py               create_database_engine, session factory, pragma listener
    alembic_config.py       build_alembic_config(url) -> alembic.config.Config
    migrations/
        __init__.py
        env.py              async env, render_as_batch=True
        script.py.mako
        versions/
            0001_initial_schema.py
            0002_seed_assets.py
```

Migrations live **under `src/portfolio/`** rather than at `backend/alembic/` because the
Dockerfile copies `backend/src` into the image and nothing else. A migration directory
outside `src/` would simply not exist in production. `backend/alembic.ini` stays at the
backend root purely so `uv run alembic revision --autogenerate` works during development;
its `script_location` points into the package, and it carries **no** database URL —
`env.py` reads the URL from `Settings`, so there is exactly one source of truth.

`alembic_config.py` builds the same `Config` object programmatically for a given URL. The
tests and the startup hook use it; neither has to know where the ini file is or whether
the process's working directory happens to be `backend/`.

*Rejected:* letting `alembic.ini` hold `sqlalchemy.url`. It duplicates the URL, and the
duplicate is the one that would be wrong in production.

### Pragmas

`engine.py` registers a `connect` listener on `engine.sync_engine` — the event fires on
the DBAPI connection, so it is the one place that runs for every pooled connection,
including the ones Alembic opens:

| Pragma | Value | Why |
|---|---|---|
| `journal_mode` | `WAL` | A read during a write otherwise blocks; the UI polls while a sync writes. |
| `foreign_keys` | `ON` | Off by default. Without it every `ForeignKey` in the schema is decorative. |
| `busy_timeout` | `5000` ms | Turns a `database is locked` crash into a wait. |
| `synchronous` | `NORMAL` | The safe pairing with WAL: durable across process crashes, one fsync per checkpoint instead of per commit. |

The listener is guarded on the SQLite dialect so it is inert if the engine is ever pointed
elsewhere. `journal_mode` is a database-level property rather than a connection-level one,
so re-issuing it per connection is redundant but harmless, and it is what brings a brand
new database file up in WAL without a separate bootstrap step.

### Naming convention

```python
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

This is load-bearing, not cosmetic. Batch mode rebuilds a table by copying it, and an
anonymous `CHECK` or `UNIQUE` constraint cannot be dropped or re-created by name. The
`assets.kind` check constraint in this change exists partly to prove the convention is
actually applied.

### `UtcDateTime`

A `TypeDecorator` over `DateTime(timezone=True)`. On bind it raises `TypeError` for a
non-datetime, raises `ValueError` for a naive datetime, and converts anything else to UTC.
On result it attaches UTC when the driver hands back a naive value, which SQLite always
does. Ruff's `DTZ` rules already ban `datetime.now()` without a timezone; this closes the
remaining hole, which is a naive value arriving from parsed input rather than from a clock.

*Rejected:* storing an integer epoch. It is unreadable in `sqlite3` at three in the
morning, and it loses sub-second precision unless you pick a unit and remember it forever.

### Startup

`create_app` gains a `lifespan` that runs `alembic upgrade head`, then creates the engine
and stores it and the session factory on `app.state`, disposing the engine on shutdown.
The upgrade runs through `anyio.to_thread.run_sync`: Alembic's async `env.py` calls
`asyncio.run`, which raises if a loop is already running in the same thread.

This is an interpretation of the issue's "Alembic wired for async" — the issue's criteria
do not mention startup. Without it the production container comes up with an empty
database file and #3 has to add the wiring as a side effect of building login. See Risks.

ASGI transport in the existing tests does not run lifespan, and the Dockerfile's build-time
smoke check calls `create_app()` without entering it, so neither is affected.

### Configuration

`Settings.database_url` changes from `sqlite:///./data/portfolio.db` to
`sqlite+aiosqlite:///./data/portfolio.db`. `deploy/compose.yml` already sets
`PORTFOLIO_DATABASE_URL: sqlite+aiosqlite:////app/data/portfolio.db`, so production is
already expecting the async driver and only the default is out of step.

### Dependencies

Added to `backend/pyproject.toml` and locked in `uv.lock`: `sqlalchemy[asyncio]>=2.0`,
`aiosqlite`, `alembic`. `anyio` is already present transitively through Starlette.

## API contract

None. This change adds no endpoint and does not alter `/api/health`, so
`frontend/src/api/generated/schema.ts` must come out of the OpenAPI drift job unchanged.

## Data model

All three tables are created by `0001_initial_schema.py`, which is reversible: its
`downgrade` drops the three tables in dependency order. `0002_seed_assets.py` inserts the
seed rows and its `downgrade` deletes exactly those symbols.

### `users`

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER` | primary key |
| `username` | `TEXT` | not null, unique |
| `password_hash` | `TEXT` | not null; the Argon2id encoded hash, written by #3 |
| `created_at` | `UtcDateTime` | not null |

### `sessions`

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER` | primary key |
| `user_id` | `INTEGER` | not null, FK → `users.id`, `ON DELETE CASCADE`, indexed |
| `token_hash` | `TEXT` | not null, unique; never the token itself |
| `created_at` | `UtcDateTime` | not null |
| `last_seen_at` | `UtcDateTime` | not null; #3 derives the sliding idle expiry from this |
| `expires_at` | `UtcDateTime` | not null; the hard absolute expiry |

### `assets`

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER` | primary key |
| `symbol` | `TEXT` | not null, unique |
| `name` | `TEXT` | not null |
| `decimals` | `INTEGER` | not null; base-unit exponent, used by #2's `BaseUnits` |
| `kind` | `TEXT` | not null, `CHECK (kind IN ('crypto', 'fiat'))` |
| `created_at` | `UtcDateTime` | not null |

Seed rows: `BTC` (Bitcoin, 8, crypto), `KAS` (Kaspa, 8, crypto), `USDT` (Tether, 6,
crypto). Those are the two chains V1 reads and the quote asset both exchanges price spot
pairs in. No fiat row is seeded: the display currency is M2's decision and guessing it here
would plant a row someone has to migrate away.

## Acceptance criteria

Copied from the issue.

1. `alembic upgrade head` builds the schema from an empty database.
2. `alembic downgrade base` succeeds.
3. A `connect` listener applies `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout` and
   `synchronous=NORMAL`, and a test asserts each one is actually in effect.
4. A test proves a foreign key violation is rejected.
5. A `UtcDateTime` type stores timezone-aware UTC and rejects naive datetimes.
6. A CI check fails when the models and the migrations have drifted apart.
7. The repository test fixture uses a file-backed temporary database, not `:memory:`.

**Interpretation of 3.** "In effect" means read back with `PRAGMA <name>` over a connection
obtained from the application's own engine factory, not asserted against the source of the
listener. A test that greps the code proves nothing.

**Interpretation of 6.** The drift check is a pytest test using
`alembic.autogenerate.compare_metadata`, which runs inside the existing required
`Backend tests` CI job. No new workflow job is added: a new job name is not a required
status check in the `main` ruleset, so it could go red without blocking a merge, which is
the opposite of what this criterion asks for.

**Interpretation of 7.** The fixture is `tmp_path`-based, and a test asserts the engine's
URL resolves to a real file on disk, so the `:memory:` regression is caught mechanically
rather than by review.

## Test plan

| # | Criterion | Test |
|---|---|---|
| 1 | `upgrade head` from empty | `backend/tests/db/test_migrations.py::test_upgrade_head_creates_every_table` |
| 2 | `downgrade base` | `backend/tests/db/test_migrations.py::test_downgrade_base_leaves_no_application_tables` |
| 2 | round trip is repeatable | `backend/tests/db/test_migrations.py::test_upgrade_downgrade_upgrade_round_trip` |
| 3 | pragmas in effect | `backend/tests/db/test_engine.py::test_sqlite_pragmas_are_in_effect` (parametrized over all four) |
| 3 | pragmas on every pooled connection | `backend/tests/db/test_engine.py::test_pragmas_apply_to_a_second_connection` |
| 4 | FK violation rejected | `backend/tests/db/test_engine.py::test_foreign_key_violation_is_rejected` |
| 4 | FK cascade actually cascades | `backend/tests/db/test_engine.py::test_deleting_a_user_cascades_to_sessions` |
| 5 | aware datetime round-trips as UTC | `backend/tests/db/test_types.py::test_utcdatetime_round_trips_as_utc` |
| 5 | non-UTC offset is normalized | `backend/tests/db/test_types.py::test_utcdatetime_normalizes_a_non_utc_offset` |
| 5 | naive datetime rejected | `backend/tests/db/test_types.py::test_utcdatetime_rejects_a_naive_datetime` |
| 5 | non-datetime rejected | `backend/tests/db/test_types.py::test_utcdatetime_rejects_a_non_datetime` |
| 6 | no model/migration drift | `backend/tests/db/test_migrations.py::test_models_and_migrations_have_not_drifted` |
| 7 | fixture is file-backed | `backend/tests/db/test_fixtures.py::test_database_fixture_is_file_backed` |
| — | seed rows present and correct | `backend/tests/db/test_migrations.py::test_asset_seed_rows` |
| — | seed downgrade removes the rows | `backend/tests/db/test_migrations.py::test_asset_seed_downgrade_removes_the_rows` |
| — | naming convention applied | `backend/tests/db/test_base.py::test_constraints_are_named_by_the_convention` |
| — | batch mode configured | `backend/tests/db/test_migrations.py::test_env_runs_with_render_as_batch` |
| — | startup upgrades and disposes | `backend/tests/db/test_lifespan.py::test_lifespan_migrates_and_exposes_a_session_factory` |
| — | health endpoint unchanged | existing `backend/tests/api/test_health.py`, `test_openapi.py` still green |

Failure cases are first-class here: criteria 4 and 5 are *only* meaningful as rejections,
and the drift test is a failure detector by construction.

## File ownership

Disjoint. Nobody writes a path outside their row.

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/db/**`, `backend/src/portfolio/config.py`, `backend/src/portfolio/main.py`, `backend/alembic.ini`, `backend/pyproject.toml`, `backend/uv.lock` |
| tester | `backend/tests/**` |
| reviewer | nothing |
| tech lead | `docs/specs/001-sqlalchemy-foundation-migrations.md` |

`backend/tests/conftest.py` belongs to the tester. backend-dev must not add fixtures to it;
if an implementation needs one, ask.

## Risks

- **Startup migrations are an addition to the issue.** If the intent was for the container
  to stay schema-less until #3, this is the part to cut, and it is deliberately isolated in
  `main.py` plus one test so cutting it is a small revert.
- **`compare_metadata` can report false drift.** Custom `TypeDecorator`s, server defaults
  and SQLite's loose typing all produce spurious diffs. If the drift test proves flaky
  rather than useful, the fix is a narrow `include_object`/`compare_type` filter with a
  comment saying exactly what is being ignored and why — not deleting the test.
- **`PRAGMA journal_mode=WAL` is silently ignored for `:memory:`.** Criteria 3 and 7
  protect each other: an in-memory fixture would make the WAL assertion pass or fail for
  the wrong reason.
- **Alembic's async `env.py` calls `asyncio.run`.** Called from inside a running loop it
  raises `RuntimeError`. The lifespan hop through a worker thread is the mitigation; a
  future caller that forgets it will fail loudly, not silently.
- Nothing here depends on an external API, so there is nothing to verify against vendor
  documentation.

## What the implementation found that this plan did not

Recorded because the plan was wrong about one thing that mattered, and the next migration
author needs to know why the code looks the way it does.

### Turning foreign keys on made batch migrations destructive

The plan treated `foreign_keys=ON` as unambiguously good. It is, at runtime — but Alembic's
batch mode rebuilds a table by `CREATE _alembic_tmp_x`, `INSERT ... SELECT`, **`DROP TABLE
x`**, rename, and SQLite's `DROP TABLE` performs an implicit `DELETE FROM` that fires
`ON DELETE` actions. Reproduced against this schema: a batch rebuild of `users` emptied
`sessions`, reported success, and left the container healthy. With a `NO ACTION` reference
it aborts the deploy instead.

The fix could not live in the migration that needs it — `PRAGMA foreign_keys` is a no-op
inside a transaction, so `op.execute("PRAGMA foreign_keys=OFF")` in a revision is a
statement that succeeds and changes nothing. It lives in `db/migration_guards.py`, applied
by `env.py` to the migration connection only:

- enforcement is switched off under `AUTOCOMMIT`, and **read back** rather than assumed;
- `PRAGMA foreign_key_check` gates the commit;
- the runtime engine is untouched, which is the point of criterion 3.

### The integrity check needed a baseline

A whole-database `foreign_key_check` cannot tell damage this run caused from damage the
database arrived with. Without a baseline, a restored backup containing one orphan wedged
the deploy permanently: the container never became healthy, `deploy.py` rolled back, and
the previous image contained the same check and died identically.

`assert_no_dangling_foreign_keys` now takes a snapshot before any revision runs and fails
only on the difference. Identity is `(child table, parent table, fk index, the child row's
foreign key column values)` — **not rowid**, because a batch rebuild renumbers rows unless
the primary key aliases the rowid, which would make every pre-existing orphan in a rebuilt
table look newly introduced. The comparison is a multiset, not a total, so a run that
repairs one orphan and introduces another is still refused. A pre-existing violation is
logged at warning level with the table and the count, and no column value.

### DDL is not transactional under pysqlite by default

The first attempt at the above wrapped the run in `connection.begin()`, which does not
bracket DDL: pysqlite emits `BEGIN` for DML only. A refused migration rolled back the
orphan row and `alembic_version` but **left the created table**, and the next
`upgrade head` died with "table already exists" — unrecoverable by retrying, and a
developer running it locally has no backup. `create_migration_engine` applies SQLAlchemy's
pysqlite recipe (`isolation_level=None`, explicit `BEGIN` on the `begin` event) to the
migration engine alone.

The two mechanisms are coupled: the foreign-key guard **must** use `AUTOCOMMIT`, and the
`begin` listener **must not** emit `BEGIN` while it does, or the pragma lands back inside a
transaction and silently does nothing. Both halves are documented in place.

### Smaller corrections

- **The drift check cannot see CHECK constraints.** Alembic's autogenerate has no
  check-constraint comparator, so the duplicated `kind IN (...)` text was unguarded. A test
  reflecting `ck_assets_kind`'s `sqltext` covers it, with a tripwire for the day
  autogenerate grows one.
- **`versions/` was invisible to `import-linter`.** grimp prunes a directory with no
  `__init__.py`, so a data migration could have imported upward with the contract still
  reporting "3 kept, 0 broken". Revision files were renamed `v0001_*` / `v0002_*` (revision
  **ids** unchanged) so the directory can be a package.
- **Coverage never measured the migration modules.** `source = ["portfolio"]` matches on a
  module's `__name__`, and Alembic loads `env.py` and every revision script by path.
  `source = ["src/portfolio"]` measures them; `env.py` went from a reported 0% to 100%.
- **`compare_type=True` has been Alembic's default since 1.12.** Declaring it is still
  right — it pins behaviour against a future flip, and a test asserts that — but the
  original rationale overstated the case.

### Test plan, as built

The table above names 19 tests. The suite ships **156**, of which 88 are new under
`backend/tests/db/`. The additions are concentrated in `test_migration_guards.py` and
`test_migration_safety.py`, which did not exist when this plan was written because the
hazards they cover had not been found yet.
