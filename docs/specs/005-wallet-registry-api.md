# 005 — Wallet registry API with offline address validation

Issue: #5
Status: done

## Problem

Nothing in the database knows which addresses to read balances from. `users`, `sessions`
and `assets` exist; there is no `wallets` table, no endpoint to add one, and no way to tell
a real address from a typo.

The addresses come from a Tangem hardware wallet, which exposes no API. What it gives the
user is a public address they copy and paste — so the only defence against a mistyped or
truncated address is a checksum the server can verify **without asking anyone**. An address
accepted with a single wrong character is a wallet that silently reports a zero balance
forever, and the user has no way to tell that from an empty wallet.

## Scope

- `wallets` table and its migration, with `UNIQUE(user_id, chain_key, address_canonical)`.
- `GET`/`POST /api/wallets`, `PATCH /api/wallets/{id}`, `DELETE /api/wallets/{id}`.
- Pure, offline address validation for Bitcoin and Kaspa, in `domain/`.
- Canonical and display forms stored separately.
- Soft archive rather than deletion.
- `address` added to the logging redaction fragments, and nothing logging an address anyway.

## Non-goals

- **Reading balances.** The chain providers are #6–#8; this issue only records what to read.
- **`xpub`/`zpub` derivation** (#24). This is one address per row. The redaction list already
  covers extended keys, and the validators here reject them rather than half-supporting them.
- **A wallets UI** (#11). No frontend code, beyond regenerating the OpenAPI types.
- **Ethereum, or any EIP-55 chain.** Two chains are enough to prove the registry shape, and
  a third with different rules would be guesswork until there is a provider for it.
- **Reassigning a wallet between users.** The product is single-user; `user_id` exists
  because the uniqueness constraint is specified with it, not because sharing is coming.

## Design

### Validation is pure, lives in `domain`, and is the layer's whole point

`domain/addresses.py` holds the codecs; `domain/chains.py` holds the registry that maps a
chain key to its validator. Both are pure: no clock, no network, no ORM. A service asks
`validate_address(chain_key, raw)` and gets back a `ValidatedAddress(canonical, display)`
or a `AddressInvalidError` naming the reason.

The issue calls this "a narrow interface to the chain registry". It lands in `domain` rather
than `providers` deliberately: a provider is an I/O boundary that `services` may call, and
putting a pure function there would make the one rule this issue exists to guarantee —
*validation never costs a network round-trip* — a matter of discipline rather than of
layering. When #6 defines the chain **provider** protocol, it keys off the same `ChainKey`
values; the registries are siblings, not one wrapping the other.

Rejected alternative: validate in a Pydantic field validator on the request schema. It reads
well and it puts the rule in the wrong place — the CLI and any future importer would each
need their own copy, and the error would be a Pydantic message rather than a domain one.
The schema validates *shape* (non-empty, length bounds, known chain key); the domain
validates *correctness*.

### Canonical and display are different strings, and both are stored

| Form | Used for | Bitcoin bech32 | Bitcoin base58 | Kaspa |
|---|---|---|---|---|
| canonical | the unique constraint, provider calls | lowercased | unchanged | lowercased, prefix always present |
| display | what the user sees | as the user typed it | unchanged | as typed |

Bech32 is case-insensitive but must not be *mixed* case, so the canonical form is the
lowercase one while the display form preserves the uppercase rendering some wallets show.
Base58Check is case-*sensitive* — `1A` and `1a` are different addresses — so lowercasing it
would corrupt it. That asymmetry is exactly why two columns exist rather than one plus a
`lower()` call at query time, and why the unique constraint is on the canonical column.

### Checksums, not shapes

- **Bitcoin bech32 / bech32m** (BIP-173, BIP-350): verify the checksum constant against the
  witness version — version 0 uses bech32, versions 1–16 use bech32m, and accepting either
  for both is the classic bug. Enforce the human-readable part (`bc`, `tb`, `bcrt`), the
  program length (20 or 32 bytes for v0), and reject mixed case.
- **Bitcoin Base58Check**: decode, verify the four-byte double-SHA256 checksum, and check the
  version byte against the network. Reject non-base58 characters explicitly.
- **Kaspa**: `kaspa:` / `kaspatest:` / `kaspadev:` prefix plus a CashAddr-style payload with
  an eight-character checksum computed over the prefix and payload.

**The generator constants must be transcribed from the published specification, with the
source named in a comment beside them.** They must not be reconstructed from a sample
address, which is circular. See Risks — this is the part of the change most likely to be
subtly wrong.

### Soft archive, and why a duplicate is always 409

`DELETE /api/wallets/{id}` sets `archived_at` and returns `204`. Nothing is removed, because
balance snapshots (#10) will reference `wallet_id` and a portfolio that forgets its own
history the moment an address is retired is not a portfolio tracker.

An archived row still occupies its slot in `UNIQUE(user_id, chain_key, address_canonical)`.
So `POST` of an address that already exists returns `409` **whether or not it is archived**,
and the problem detail says which. Un-archiving is `PATCH {"archived": false}` — an explicit
act on a row the user can already see, not a side effect of re-adding.

Rejected alternative: make the unique constraint partial, on unarchived rows only. SQLite
supports it, and it would let a re-add silently resurrect a wallet — with its old label and
its old snapshots — while looking to the user like a new one.

`DELETE` on an already-archived wallet is `204`, not `404`: it is idempotent, and the
end state the caller asked for is the end state they get.

### Files

**Created**

```
backend/src/portfolio/domain/addresses.py        bech32/bech32m, base58check, kaspa codecs
backend/src/portfolio/domain/chains.py           ChainKey, the validator registry
backend/src/portfolio/repositories/wallets.py    every query against `wallets`
backend/src/portfolio/services/wallets.py        WalletService, the 409/404 policy
backend/src/portfolio/api/routers/wallets.py     the four routes
backend/src/portfolio/api/schemas/wallets.py     request/response models
backend/src/portfolio/db/migrations/versions/v0003_wallets.py
```

**Changed**

```
backend/src/portfolio/db/models.py     the Wallet mapping
backend/src/portfolio/api/dependencies.py  get_wallet_service
backend/src/portfolio/main.py          router registration
backend/src/portfolio/logging.py       "address" added to SENSITIVE_KEY_FRAGMENTS
frontend/src/api/generated/schema.ts   regenerated; owned by whoever changed the schema
```

## API contract

All four require a session; none is added to `PUBLIC_API_PATHS`. No monetary field appears
anywhere in this change.

| Method | Path | Body | Success | Failures |
|---|---|---|---|---|
| `GET` | `/api/wallets` | — | `200` `{wallets: [...]}` | `401` |
| `POST` | `/api/wallets` | `{chain_key, address, label?}` | `201` wallet | `401`, `409` duplicate, `422` invalid |
| `PATCH` | `/api/wallets/{id}` | `{label?, archived?}` | `200` wallet | `401`, `404`, `422` |
| `DELETE` | `/api/wallets/{id}` | — | `204` | `401`, `404` |

`GET` takes `?include_archived=true` to return archived rows as well; the default is `false`.

Wallet representation:

```json
{
  "id": 1,
  "chain_key": "bitcoin",
  "address": "tb1q...",
  "label": "Cold storage",
  "archived": false,
  "created_at": "2026-09-21T00:00:00Z",
  "updated_at": "2026-09-21T00:00:00Z"
}
```

`address` is the **display** form. The canonical form is an implementation detail of the
uniqueness rule and is not exposed — publishing both invites a client to pick the wrong one.

A `422` carries the existing field-level shape: a problem document plus an `errors` array of
`{loc, msg, type}`. **The `msg` must not echo the address back.** It names the field and the
reason ("checksum does not match", "unknown human-readable prefix"), because the 422 body is
the one response that could carry an address into a log on the client side.

## Data model

```sql
CREATE TABLE wallets (
    id                INTEGER PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chain_key         TEXT    NOT NULL,
    address_canonical TEXT    NOT NULL,
    address_display   TEXT    NOT NULL,
    label             TEXT,
    archived_at       TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    CONSTRAINT uq_wallets_user_chain_address UNIQUE (user_id, chain_key, address_canonical),
    CONSTRAINT ck_wallets_chain_key CHECK (chain_key IN ('bitcoin', 'kaspa'))
);
CREATE INDEX ix_wallets_user_id ON wallets (user_id);
```

The migration is reversible: `downgrade` drops the table and the index.

**The `CHECK` constraint text is duplicated verbatim between `models.py` and the migration,
and Alembic's autogenerate has no check-constraint comparator** — editing one without the
other passes every gate and then rejects inserts in production. `assets.kind` already has
this problem and already has the test that covers it: a test reflects the constraint off a
migrated database and compares its `sqltext` to the constant. `ck_wallets_chain_key` gets
the same treatment.

Migrations run with foreign keys off, gated by the `foreign_key_check` baseline diff. Do not
turn foreign keys back on.

**Correction — this spec was wrong when written.** It said the baseline "has to be updated for
the new table". There is nothing to update: the baseline is computed at run time by
`snapshot_foreign_key_violations` and is a `Counter` of live violations, not a list of tables.

What actually has to be updated is `APPLICATION_TABLES` in `backend/tests/db/test_migration_env.py`
and `test_migrations.py`, and `EXPECTED_NAMES` in `test_base.py` — and those are compared with
`>=` and by key, so they **pass silently** while a new table is missing from them. They cover
less than they claim, and nothing fails to say so. Adding `wallets` to them is part of this
issue; making them exact rather than `>=` is worth doing while the reason is fresh.

## Acceptance criteria

Verbatim from the issue, numbered.

1. `GET/POST /api/wallets`, `PATCH /api/wallets/{id}`, and `DELETE` as a soft archive.
2. Valid addresses are accepted and stored in both canonical and display form.
3. Malformed addresses are rejected with 422 and a field-level `problem+json` error.
4. A duplicate address for the same chain returns 409.
   **Interpretation:** including when the existing row is archived — see Design.
5. `UNIQUE(user_id, chain_key, address_canonical)` is enforced by the database.
6. Archiving preserves historical balance snapshots rather than deleting them.
   **Interpretation:** `balance_snapshots` does not exist yet (#10). What is provable now is
   that the row survives with its id intact, so a later foreign key has something to point
   at. The test asserts the row is still present and still unique after archiving.
7. Addresses never appear in any log record.
8. All tests use testnet addresses only.
9. Unauthenticated access returns 401.

## Test plan

| # | Criterion | Test |
|---|---|---|
| 1 | The four routes round-trip | `tests/api/test_wallets_router.py::test_create_list_patch_archive_round_trip` |
| 1 | `DELETE` archives, does not delete | `test_wallets_router.py::test_delete_soft_archives_the_row` |
| 1 | `DELETE` is idempotent | `test_wallets_router.py::test_delete_twice_returns_204_both_times` |
| 1 | Archived rows are hidden by default | `test_wallets_router.py::test_list_excludes_archived_unless_asked` |
| 2 | Bech32 canonicalises to lowercase, display preserved | `tests/domain/test_addresses.py::test_bech32_uppercase_canonicalises_and_preserves_display` |
| 2 | Base58 is **not** lowercased | `test_addresses.py::test_base58check_is_case_sensitive_and_unchanged` |
| 2 | Both forms reach the database | `tests/db/test_wallets_repository.py::test_stores_canonical_and_display_separately` |
| 3 | Each malformed shape is 422 with a field-level error | `test_wallets_router.py::test_malformed_address_returns_422_with_field_error` |
| 3 | The 422 body does not echo the address | `test_wallets_router.py::test_validation_error_does_not_contain_the_address` |
| 4 | Duplicate is 409 | `test_wallets_router.py::test_duplicate_address_returns_409` |
| 4 | Duplicate of an **archived** row is 409 | `test_wallets_router.py::test_duplicate_of_archived_address_returns_409` |
| 4 | Same address on a different chain is allowed | `test_wallets_router.py::test_same_address_on_another_chain_is_accepted` |
| 5 | The constraint is in the database, not only the service | `tests/db/test_wallets_repository.py::test_unique_constraint_rejects_duplicate_insert` |
| 6 | The row survives archiving with its id | `test_wallets_repository.py::test_archived_row_is_retained_with_its_id` |
| 7 | No log record contains an address | `tests/security/test_address_logging.py::test_no_log_event_contains_an_address` |
| 7 | `address` is a redacted key | `tests/security/test_address_logging.py::test_address_key_is_redacted` |
| 8 | Fixtures are testnet only | `tests/security/test_address_logging.py::test_fixtures_contain_no_mainnet_address` |
| 9 | 401 without a cookie | already covered by the route-walking contract test; assert the new paths appear in it |

### Address vectors

Every codec gets, at minimum:

- one valid testnet address per form: `tb1` (v0 bech32), `tb1p` (v1 bech32m), base58 testnet
  P2PKH and P2SH, `bcrt1`, `kaspatest:`;
- **a single-character corruption of each one, which must be rejected** — this is the test
  that proves a checksum is being verified rather than a shape being matched;
- mixed-case bech32, rejected;
- a bech32 v1 address carrying a bech32 checksum, and a v0 carrying bech32m, both rejected;
- an `xpub`/`tpub`, rejected — it is not an address;
- the empty string, whitespace, and a 200-character string.

### Mutation checks the tester must run

Per the standing lesson: a spec that names a test is not a spec that says what it must assert.

- Replace the checksum verification with `return True`. Every corruption test must fail.
- Swap the bech32 and bech32m constants. The v0/v1 tests must fail.
- Lowercase the base58 address before storing. The case-sensitivity test must fail.
- Drop the `UNIQUE` constraint from the migration but keep the service's duplicate check.
  Criterion 5's repository test must fail while the router's 409 test still passes — if both
  pass, the constraint is not being tested at the database level.
- Make `DELETE` a hard delete. Criterion 6 must fail.
- Remove `address` from the redaction fragments. Criterion 7's redaction test must fail.

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/**`, `backend/pyproject.toml`, `backend/uv.lock`, `frontend/src/api/generated/schema.ts` |
| tester | `backend/tests/**` |
| tech-lead | `docs/**`, `CLAUDE.md`, `backend/.importlinter` |
| reviewer | nothing |

Paths are disjoint. The tester does not edit `pyproject.toml`: a test needing a dependency or
a coverage setting asks the tech lead, who routes it to backend-dev. `schema.ts` is generated
output belonging to whoever changed the backend schema, per 003.

## Risks

- **The Kaspa checksum is the most likely thing here to be subtly wrong.** There is no Kaspa
  node in CI and no third-party vector in this repository, and rule 3 forbids committing a
  mainnet address — so the obvious source of a known-good vector is unavailable. The
  constants must be transcribed from the published specification with the source cited in a
  comment, never reverse-engineered from a sample. If a trustworthy testnet vector cannot be
  established, **say so and stop** rather than generating one with the implementation being
  tested, which proves only that the code agrees with itself. The single-character corruption
  tests are what give this teeth regardless of where the vector came from.
- **Bech32 vs bech32m is a real bug that ships quietly.** Accepting either constant for any
  witness version makes every test pass and lets a v1 address with a v0 checksum through.
- **The `CHECK` constraint duplication is unenforced by Alembic.** See Data model. The
  reflection test is the only thing standing between an edit and a production insert failure.
- **A 422 is the one response that carries user input back.** If a validator's message
  interpolates the address, it lands in the response body and from there into any client-side
  log. The messages name the field and the reason, never the value.
- **`address` as a redaction fragment is a substring match**, so it also covers
  `address_canonical`, `address_display` and `email_address`. That is intended; it is listed
  here because the fragment list is shared with the exchange credentials and widening it
  affects them too.
- **Coverage floor is 99 on the backend and only ratchets.** Codec code is branchy and easy
  to leave half-covered; the tester owns the floor holding and raising it.

## What the spec got wrong

### Criterion 6 was mapped to a test that cannot fail for it

The test plan named only the repository test for "archiving preserves history". That test
cannot see a hard delete, because the mutation lives in the *service* — it survives. What
actually covers the criterion is `test_delete_soft_archives_the_row`, which issues a real
`DELETE` and then reads the `wallets` table directly, because `GET` hides archived rows and
so a soft archive and a hard delete are indistinguishable from the API alone.

The criterion passes. The mapping was wrong, and a mapping that names the wrong test is how a
criterion ends up believed rather than proven.

### The `foreign_key_check` baseline claim was false

Already corrected inline in Data model. The baseline is computed at run time; nothing needed
updating. What did need updating — `APPLICATION_TABLES` and `EXPECTED_NAMES` — was compared
with `>=` and by key, so a missing table failed nothing. Those lists reported coverage they
did not have, and are exact now.

### `addMoney`'s cousin: a second spelling of one address

Not in the test plan, found while building the vectors. Appending one five-bit character to a
Kaspa payload and recomputing the checksum yields a string whose version byte and payload
length both come out **correct** — 54 five-bit values regroup into the same 33 bytes as 53.
The reference decoder's `conv5to8` truncates without checking, so it reads the two strings as
one address.

`address_canonical` is what the unique constraint indexes, so two spellings are two rows: the
same wallet registered twice, and **its balance counted twice in the portfolio total**. The
codec therefore checks the exact charset-character count, which is a stronger rule than the
strict-padding one it replaced.

### The criterion-7 test was blind to the only real criterion-7 violation

Found by adversarial review, after the gate was green and eight mutations were killed.

A duplicate `POST` that loses the race raises `IntegrityError`, which nothing translated. It
reached `handle_unexpected_error`, which calls `_logger.exception` — and SQLAlchemy renders
bound parameters into the exception string, while `redact_sensitive` matches on *key names*,
and the key is `exception`. The production JSON log carried both address columns verbatim,
and the client got a `500` instead of a `409`:

```
{"event": "unhandled_exception", "level": "error",
 "exception": "... [parameters: (1, 'bitcoin', 'tb1q…', 'tb1q…', None, None, ...)]"}
```

The trigger is a double-click on #11's Add-wallet button or a client retry. The same
rendering applied to `OperationalError: database is locked`, which needs no duplicate at all
and gets likelier when #10 adds a second writer.

Fixed in two halves, deliberately: the `IntegrityError` is translated to a conflict — the
database is the authority and the service's pre-check is an optimisation — **and**
`hide_parameters=True` is set on the engine, which is the half that protects every future
table rather than this one. A portfolio database will hold trade rows and balances, and none
of them belong in a log either.

`test_no_log_event_contains_an_address` could not see any of it. Its docstring claimed to
catch an address "carried inside an exception"; `structlog.testing.capture_logs` replaces the
entire processor chain, so `format_exc_info` never ran and the exception string never
existed. The criterion-7 tests now install the production pipeline and read rendered stdout.

Two smaller findings from the same review: `PATCH {"archived": true}` restamped `archived_at`
while `DELETE` deliberately did not — one operation, two answers, neither pinned — and a
Unicode homoglyph (U+212A KELVIN SIGN, which lowercases to `k` but uppercases to itself)
passed the mixed-case guard and was stored as a display form that is not a valid address
anywhere.

## The lesson this issue actually taught

Four failures in this issue are the same mistake wearing different clothes. Each is a
verifier that shares state with the thing it verifies, and can therefore only confirm its own
account:

| Shape | What it was |
|---|---|
| A vector generated by the implementation under test | the risk this spec opened by naming — avoided |
| A test deriving its expectation from the constant it checks | #3's five tests |
| An assertion that only checks an *absence* | a redaction test passing against an empty capture |
| A restore verified against a digest taken by the mutating code | the harness corrupting `addresses.py` and reporting success |
| A log assertion reading a capture that replaced the pipeline | `capture_logs`, which hid a real leak through a green gate |
| A harness reading "pytest could not collect" as "no failures" | two mutations scored as passes |

The last one is worth spelling out, because it nearly shipped a corrupted source file. The
mutation harness snapshotted "originals" inside its own edit loop, so a two-edit mutation
stored half-mutated bytes as the baseline — and then compared the restored file against that
poisoned digest and reported byte-identity. CPython compounded it: a `.pyc` is validated on
source mtime truncated to whole seconds plus size, and swapping two constants of equal length
changes neither, so a revert within one second left restored source running mutated bytecode.
It was caught because the suite went red, not because anyone looked.

**The rule: a restore is verified against a baseline taken outside the process that mutates,
and by behaviour rather than by bytes.** The re-run that settled it here was not a hash — it
was replaying the published vectors and observing identical mutation counts across an edit,
which is a statement a digest cannot make.

And the corollary for a tech lead: when an agent reports a hash as proof, the hash proves
nothing on its own. Re-running an independent check is cheap; this one took two minutes and
was the difference between trusting a report and knowing.

## Deferred deliberately

- **One mutation survives on purpose.** Removing `address` from the redaction fragments leaves
  `test_no_log_event_contains_an_address` green, because nothing in the registry hands an
  address to a logger. Killing it would mean adding code that logs an address in order to
  prove the redaction works. The fragment list is defence in depth here, not the control;
  `test_address_key_is_redacted` is what dies.
- **`test_same_address_on_another_chain_is_accepted` cannot test its literal case.** No string
  is a valid address on both chains, so validation refuses the pair before the constraint is
  consulted. The router asserts the reachable consequence; the literal case is covered at the
  database level.
- **Coverage floor stays at 99.** Measured 99.79 against 99.70 at #3 — 0.09 points is not
  materially above, and 99.5 would leave about five units of headroom across the whole
  backend, which is a floor that fails for reasons unrelated to testing.

**And a fifth shape, which is the cheapest to guard against: never let an assertion be
satisfiable by emptiness.** Two tests in this issue passed against an empty capture — one
because the pipeline was never configured, one because pytest swapped `sys.stdout` between
fixture setup and the call phase, so the buffer the test read was always `''`. Every absence
check in this suite now carries a positive companion proving the thing it searched was real.

A sixth, from the rework: one archive test **passed in a batched run and failed alone**,
because two `datetime.now` reads happened to agree, and a mutant survived on it. A test whose
verdict depends on how fast the host is has no verdict. Those tests now run against an
injected clock that steps one second per call.
