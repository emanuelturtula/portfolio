# 013 — `create-user --replace` replaces the credential, not the owner

Issue: #69
Status: implementing

## Problem

`portfolio create-user --replace` is the only recovery path for a forgotten password (#3). It
is implemented as `DELETE FROM users` followed by an insert. Every row that belongs to the
owner hangs off `users.id`:

- `wallets.user_id` is `ON DELETE CASCADE`, and `balance_snapshots.wallet_id` cascades from
  `wallets`. So **today, recovering a password silently deletes every registered wallet and
  its whole balance history.** `docs/operations.md` says "no portfolio data is lost", and has
  been wrong since #5.
- `exchange_accounts.user_id` is `CASCADE` and `exchange_fills.exchange_account_id` is
  `RESTRICT` (#12). So **once #15 writes the first fill, `--replace` fails** with
  `sqlite3.IntegrityError: FOREIGN KEY constraint failed`, and a forgotten password becomes a
  lost instance again, which is the failure `--replace` exists to prevent.

The `RESTRICT` is correct and stays. A venue keeps fills for a limited time, so a fill deleted
along with its account may be gone for good. The defect is that `--replace` deletes the owner
when all it needs is to change the owner's credential.

## Scope

- `AuthService.create_user(..., replace=True)` updates the existing owner row in place: the new
  username and the new password hash, then revokes every session that user holds, in one
  transaction. The row's `id` and `created_at` do not change, and no row outside `users` and
  `sessions` is touched.
- `UserRepository` gains the two queries this needs and loses `delete_all`, which nothing else
  uses.
- The CLI's confirmation prompt and `docs/operations.md` say what the command now does.

## Non-goals

- **No migration and no change to any foreign key.** The schema is right; the command was
  wrong.
- **No password reset flow.** Still none, by design (#3).
- **No change to `POST /api/auth/password`**, which already updates in place and revokes
  sessions. This change makes the recovery path do the same thing.
- **Specs 003 and 012 are not edited.** They record what was decided at the time, and this spec
  is where the change is recorded.

## Design

### Four cases, decided in the service

| Accounts in `users` | `replace=False` | `replace=True` |
|---|---|---|
| 0 | create | create (unchanged: pointing the command at a fresh volume is ordinary) |
| 1 | `UserExistsError` (unchanged) | **update that row in place, and revoke its sessions** |
| more than 1 | `UserExistsError` (unchanged) | **refuse with `UserExistsError`**, changing nothing |

The last row is new. Nothing in the application can create a second account: `bootstrap_user`
and `create_user` both refuse once one exists. A second account can only come from hand-written
SQL. The old code handled it by deleting all of them. Picking one to keep would be a guess about
which owner the operator means, and deleting the others would take their wallets with them. So
the command refuses, says why, and changes nothing. The message says how many accounts exist and
names none of them.

### Why update in place instead of deleting inside a savepoint

Rejected: detaching the owner's rows, deleting the user, inserting a new one and re-attaching
them. That is five statements to reach the same end state as one `UPDATE`, and every table added
later that references `users.id` would need adding to the list, or it silently falls back into
the cascade. An update in place is correct for any table referencing `users.id`, including tables
that do not exist yet.

Rejected: switching `wallets.user_id` and `exchange_accounts.user_id` to `RESTRICT`. The command
would then fail today instead of after #15, and it would still be wrong.

### Sessions

Revoked with the existing `SessionRepository.delete_for_user(user.id)`, which is what a password
change already does. The old code relied on the cascade from deleting the user; the new code
does not delete the user, so it has to revoke **explicitly**. Leaving the revoke out would be
the most dangerous mutation of this change: a stolen cookie would outlive the recovery meant to
defeat it. The tests below kill it.

### Modules

| Path | Change |
|---|---|
| `backend/src/portfolio/services/auth.py` | `create_user` implements the table above; its docstring says what is kept and what is revoked |
| `backend/src/portfolio/repositories/users.py` | add `list_all()` (or an equivalent that can tell 0, 1 and more than 1 apart without a second query) and `set_credentials(user, *, username, password_hash)`; delete `delete_all`; fix the module docstring, which still describes delete-then-insert |
| `backend/src/portfolio/cli.py` | the confirmation text: `--replace` sets a new password (and username) on the existing account and signs out every session; wallets, balances and exchange history are kept |
| `docs/operations.md` | section 4's recovery paragraph, which currently says the row is deleted and that "nothing else in the schema references the user" |

## API contract

None. No endpoint changes.

## Data model

None. No migration.

## Acceptance criteria

From the issue, numbered:

1. `--replace` updates the existing owner row in place: the new username and password hash, and
   every session for that user deleted, in the same transaction. It deletes no row outside
   `users` and `sessions`.
2. A test registers a wallet, a balance snapshot, an exchange account and a fill, then runs
   `create-user --replace`. It asserts all four still exist and that the old session no longer
   authenticates, with a positive companion showing the new password signs in.
3. `UserRepository.delete_all` is deleted if nothing else uses it. *Reading:* nothing else uses
   it, so it is deleted.

Added by this spec:

4. With more than one account present, `--replace` refuses and changes nothing.
5. The CLI's confirmation text and `docs/operations.md` describe what the command does.

## Test plan

| # | Test | Must assert |
|---|---|---|
| 1, 2 | `tests/cli/test_create_user.py::test_create_user_replace_keeps_every_row_that_belongs_to_the_owner` | Run through `cli.main`. Before: one each of wallet, balance snapshot (with its sync run), exchange account and fill, inserted through the sync engine. After: all four rows still present, byte-identical where the schema allows (compare full rows, not counts), and `users[0].id` unchanged. **Must fail against the old implementation**: run it with delete-then-insert restored and record the result in the tester's report. |
| 1 | `...::test_create_user_replace_replaces_the_credential_and_revokes_its_sessions` (renamed from `..._deletes_the_existing_user_and_its_sessions`) | New hash verifies, old does not; session rows for the user are gone; `id` and `created_at` unchanged |
| 1 | `...::test_create_user_replace_can_rename_the_owner` | `--replace --username <new>` leaves one row, with the new username and the same `id` |
| 2 | `tests/auth/...::test_after_replace_the_old_session_is_refused_and_the_new_password_signs_in` | At the service level, in the application's own flow: sign in with the old password to get a token; `create_user(..., replace=True)`; `authenticate(old_token)` is refused; `login(new)` succeeds; `login(old)` fails. Each half is the other's positive companion. |
| 4 | `tests/cli/test_create_user.py::test_create_user_replace_refuses_when_more_than_one_account_exists` | Two users inserted by hand. The command exits non-zero, both rows and their sessions are unchanged, and the message names neither username |
| 3 | none: `delete_all` is gone, so `mypy` and the import fail if anything still calls it | the reviewer confirms no reference remains |
| 5 | `...::test_create_user_replace_says_what_it_keeps` (or the existing confirmation test extended) | the prompt no longer says "deletes"; it says sessions are signed out and data is kept |
| -- | the existing `--replace` tests (prompt, confirmation, no terminal) | unchanged and still green |

**Mutations the verification must kill:**
- restore delete-then-insert;
- drop the explicit session revoke;
- update the password but not the username;
- with more than one account, update the first one instead of refusing;
- revoke sessions for a user id other than the updated row's.

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/services/auth.py`, `backend/src/portfolio/repositories/users.py`, `backend/src/portfolio/cli.py`, `docs/operations.md`, and in `docs/providers.md` only the "Not done yet" bullet #12 left about `--replace` and the fills' `RESTRICT` |
| tester | `backend/tests/**` |
| tech-lead | `docs/specs/013-*.md`, `backend/pyproject.toml` |
| reviewer | nothing |

## Risks

- **The operator's mental model.** Anyone who read the old `docs/operations.md` expects
  `--replace` to start from a clean account. The new confirmation text says in plain words that
  data is kept.
- **The running application holds no cached principal that outlives this.** Sessions are looked
  up per request, by token hash, so deleting the rows is the whole of the revocation. The login
  throttle lives in the application's own process and the CLI cannot reach it. That was already
  true of the old implementation, and it is harmless: throttling a username the operator just
  reset errs on the safe side.
