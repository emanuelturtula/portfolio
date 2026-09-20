# 003 — Single-user password login with server-side sessions

Issue: #3
Status: done

## Problem

Every endpoint this project will ever add reads the owner's financial position, and today
the API is open to anything that can reach port 8000 on the home network. The schema from
#1 already has `users` and `sessions` tables, and nothing writes to either of them: there
is no way to create the account, no way to log in, and no code path that would stop an
unauthenticated request.

The wallet registry (#5) is blocked on this, and the login page (#4) has nothing to call.

## Scope

- Argon2id password hashing, with cost parameters read from settings rather than hardcoded.
- `python -m portfolio create-user`, prompting for the password on a TTY, with a
  `--replace` flag (added after the spec was first committed — see Scope additions).
- `python -m portfolio hash-benchmark`, which measures the configured parameters on the
  host it runs on. This is what makes "tuned on the Pi" an action rather than a wish.
- Opaque session tokens, stored as a SHA-256 hash, with a sliding idle expiry and a hard
  absolute expiry.
- The `__Host-psid` cookie.
- Deny-by-default request authentication, as middleware, with an explicit public allowlist.
- Origin and `Content-Type` enforcement on every non-GET request.
- Login throttling, in process.
- `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/session`,
  `POST /api/auth/password`.
- Bootstrap-password validation that refuses to start the application.

## Scope additions

Recorded here rather than folded in silently, so the pull request review can see what grew
and why.

### `create-user --replace`

Writing `docs/operations.md` surfaced a hole this spec left open. There is deliberately no
password reset flow — no email, no recovery question, nothing to attack — and `create-user`
deliberately refuses when a user already exists. Together those mean a forgotten password
bricks the instance: no reset, no way to re-create the account, and no `sqlite3` binary in
the runtime image to go in by hand.

`--replace` deletes the existing user and creates the new one in one transaction. The
delete cascades to that user's sessions through the `ondelete="CASCADE"` already on
`sessions.user_id`, so it revokes everything as a side effect. It still prompts and still
confirms — the flag replaces the account, never the prompt — and it requires an explicit
typed confirmation, either the name of the account being destroyed or `y`, refusing outright
when stdin is not a TTY.

One flag, against an instance that otherwise cannot be recovered.

## Non-goals

- **No migration.** `users` and `sessions` landed in `0001_initial_schema` with exactly the
  columns this needs — `token_hash`, `created_at`, `last_seen_at`, `expires_at`. Revoking
  every session is a `DELETE`, not a `password_changed_at` column. If a reviewer expects a
  migration in this diff, its absence is the thing to check, not a mistake to fix.
- **No frontend.** The login page and the authenticated shell are #4. The only frontend file
  this change touches is `frontend/src/api/generated/schema.ts`, which is a generated
  artifact and must be regenerated or the OpenAPI drift job fails.
- **No registration, no password reset, no email, no second factor, no JWT, no refresh
  tokens.** Single user, one password, opaque server-side sessions.
- **No durable throttle store.** In process, see Design.
- **No session listing and no "log out other devices".** Changing the password revokes
  everything, which is the only revocation the product needs.
- **No expired-session sweeper.** An expired row is rejected on read. A single user
  accumulates a handful of rows a year.

## Design

### Layering

Two new top-level packages, and one contract change in `backend/.importlinter`:

```
portfolio.cli | portfolio.api
portfolio.services
portfolio.repositories | portfolio.providers
portfolio.db
portfolio.domain
```

`portfolio.cli` is a sibling of `portfolio.api`, not above it: they are two entry points
onto the same services, and neither imports the other.

| Module | Holds |
|---|---|
| `domain/auth.py` | Session lifetime arithmetic and token hashing. Pure: the clock is an argument. |
| `domain/passwords.py` | The password policy — minimum length, the default-password deny list. Pure. |
| `services/password_hasher.py` | The Argon2id wrapper. Not pure: it salts from the CSPRNG. |
| `services/auth.py` | Login, logout, session resolution, password change, throttling. |
| `repositories/users.py`, `repositories/sessions.py` | The only modules that touch the ORM. |
| `api/dependencies.py` | Builds an `AuthService` per request and owns the `AsyncSession`. |
| `api/middleware.py` | Origin/content-type enforcement and deny-by-default authentication. |
| `api/routers/auth.py` | Parse, call, serialize. |
| `cli.py`, `__main__.py` | `create-user` and `hash-benchmark`. |

`secrets.token_urlsafe` is a CSPRNG draw and is therefore not `domain`: it lives in
`services/auth.py`. `hash_token` is `hashlib.sha256` over a string — deterministic, no
clock, no I/O — so it is `domain`.

*Routers never see a database session.* The dependency hands the router a fully built
`AuthService`, which is what keeps `portfolio.api.routers` free of `sqlalchemy` and
`portfolio.repositories` without anyone having to remember the rule.

### Why the token hash is SHA-256 and the password hash is Argon2id

Different threat. A password is low entropy and guessable, so the hash must be slow.
A session token is 32 bytes from `secrets.token_urlsafe` — 256 bits — so no amount of
offline work recovers it, and the hash only has to be preimage resistant. Running Argon2id
on every authenticated request would add the configured ~250 ms to every page load.

*Rejected:* HMAC with a server key. It needs a key with a lifecycle, and the property it
would add over a bare digest — an attacker with the database but not the key cannot verify
a guessed token — is worthless against 256 bits of entropy.

### Argon2id parameters

Defaults at the time of this issue: `time_cost=3`, `memory_cost=65536` KiB (64 MiB),
`parallelism=4` — since retuned to 144 MiB against the measurement this issue could not
take; see `docs/operations.md`. Overridable as
`PORTFOLIO_ARGON2_TIME_COST`, `PORTFOLIO_ARGON2_MEMORY_COST`,
`PORTFOLIO_ARGON2_PARALLELISM`.

The issue asks for parameters tuned on the Raspberry Pi to roughly 250 ms. **That
measurement cannot be taken in this change** — there is no Pi in CI and no staging
environment. What this change ships instead is the means to take it, and a floor that stops
the parameters being lowered into uselessness:

- `python -m portfolio hash-benchmark` hashes with the configured parameters and reports the
  median wall time, to be run on the Pi over SSH after deployment.
- In `prod`, a settings validator refuses to start below the OWASP floor
  (`memory_cost >= 19456` KiB, `time_cost >= 2`), so a future "it feels slow" commit — or an
  operator reading KiB as MiB — cannot quietly drop them to argon2-cffi's minimum. Gated on
  `prod` because the test suite runs far below the floor deliberately, to stay fast.
  A separate test asserts the *shipped defaults* meet the floor too.

  Added after review. The spec originally claimed a floor that only a test on the defaults
  enforced, which no environment variable could reach: `PORTFOLIO_ARGON2_MEMORY_COST=64`
  would have started happily in production with a hash a thousandfold weaker than intended,
  while `docs/operations.md` told the operator that could not happen.
- `docs/operations.md` records the procedure and has a table for the measured value.

Flagged in Risks. The alternative — writing a number into the spec and calling it measured —
is worse than saying it was not.

### Cookie

`__Host-psid`, `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, no `Domain`, no `Max-Age`
(a session cookie; the server owns expiry).

**Interpretation, because it departs from the criterion's literal text:** the `__Host-`
prefix is only valid on a `Secure` cookie, and a browser silently drops a `__Host-` cookie
that arrives without it — the failure mode is a login that returns 204 and then does not
work, with nothing in any log. So the name is derived from `session_cookie_secure`:
`__Host-psid` when it is true, `psid` when it is false. `session_cookie_secure` defaults to
true, and `environment=prod` with it set to false refuses to start. Every configuration
this product actually ships therefore uses `__Host-psid`; the bare name exists only for a
developer on plain HTTP who is not on `localhost`.

`SameSite=Lax` rather than `Strict`: `Strict` withholds the cookie on a top-level navigation
from any other site, so following a bookmark from another tab would land on a logged-out
page. The Origin check below is what actually stops cross-site writes.

### Origin and Content-Type enforcement

Middleware, not a dependency, because a dependency only runs on the routes that remember to
declare it. It runs before routing, so it also covers paths that match no route.

For any method outside `{GET, HEAD, OPTIONS}`:

1. `Origin` must be present and exactly equal to `settings.allowed_origin`. A missing
   `Origin` is rejected — every browser sends one on a cross-origin request and on every
   non-GET same-origin request, so the only thing a missing header identifies is a
   non-browser client, which this API does not serve.
2. `Content-Type` must be `application/json`. Parameters such as `; charset=utf-8` are
   allowed, and the media type is compared case-insensitively.

Rule 2 is the one that matters: a form-encoded POST is the shape an HTML form can send
cross-origin without a CORS preflight. Both rejections are `403`.

In production the SPA is served from the same origin as the API, so
`PORTFOLIO_ALLOWED_ORIGIN` must be set to the deployed origin.

**Corrected after the spec was first committed.** The original plan was to make it a
required variable in `deploy/compose.yml`. That is wrong: the host-side script that supplies
those compose variables lives on the Pi, not in this repository — `scripts/remote_deploy.py`
only invokes it over SSH — so a `${VAR:?}` entry nothing sets would fail every deployment at
`docker compose up`.

Instead, `Settings` refuses to start when `environment` is `prod` and `allowed_origin` is
still the development default. The value itself is a hostname, so by rule 3 it lives in the
host-local secrets env file compose already loads through `PORTFOLIO_SECRETS_ENV_FILE`,
alongside `PORTFOLIO_BOOTSTRAP_PASSWORD`.

Refusing to start is also the better failure: an unset origin otherwise yields a container
that reports healthy and then rejects every write with `403`, and the symptom — login works,
nothing else does — does not name its cause. A refusal fails the deployment's health check
and rolls back, which is a path the pipeline already handles and tests.

### Deny-by-default authentication

The same middleware, after the Origin check: any path under `/api` that is not in
`PUBLIC_API_PATHS` requires a valid session, or the response is `401` as a problem
document.

```
PUBLIC_API_PATHS = {"/api/health", "/api/auth/login"}
```

Everything outside `/api` is the SPA bundle, which is static and carries no data, so it is
public — the login page has to load before there is a session.

`/api/docs` and `/api/openapi.json` are **not** public. Swagger UI still works for the
logged-in owner, because the browser sends the cookie.

*Rejected:* a `Depends(current_user)` on each router. Forgetting one is the exact failure
this criterion exists to prevent, and a test that walks the routes would then be checking
that nobody forgot — rather than checking a rule that cannot be forgotten.

The contract test walks `app.routes` and asserts `401` for every `/api` path not in the
allowlist. It is not tautological: it fails when someone adds a path to the allowlist
without saying so in a review, and it fails when a new router is mounted outside `/api`.

### Sessions

On login: `token = secrets.token_urlsafe(32)`; store `sha256(token)` hex with
`created_at = last_seen_at = now` and `expires_at = now + 30 days`.

On each authenticated request the session is valid when **both** hold:

- `now < expires_at` — the hard ceiling, never moved;
- `now - last_seen_at < 7 days` — the sliding idle window.

`last_seen_at` is written back only when it is more than 60 seconds stale, so a page that
polls does not turn every read into a write. The idle window is 7 days; the resolution loss
is irrelevant and the write amplification is not.

Both expiries are configurable (`PORTFOLIO_SESSION_IDLE_DAYS`,
`PORTFOLIO_SESSION_ABSOLUTE_DAYS`) so the tests can drive them without sleeping.

### Uniform failure

Unknown username and wrong password both return `401` with the identical problem document,
and both perform one Argon2id verification. When the user does not exist the service
verifies against a module-level dummy hash generated at import from a random password, so
the two paths do the same work. Timing equality is asserted as a ratio with a generous
bound, because a strict one is a flaky test on a shared CI runner.

### Throttling

In process: a dict of `username -> list[failure timestamps]`, pruned to the 15-minute
window on each check. At `>= 5` failures inside the window the next attempt is rejected
with `429` **before** the password is verified, so the 6th failure within 15 minutes is
refused. A successful login clears the entry.

*Rejected:* a `login_attempts` table. It would add the only migration in this change, plus
unbounded growth and a cleanup job, to protect a single-user application whose container
runs `--workers 1`. In-process state is exact here for the same reason it is usually wrong
elsewhere.

The cost is that a container restart clears the window. An attacker cannot cause a restart;
a deployment can, and a deployment happens on merge. Accepted, and recorded in Risks.

Keyed on the submitted username rather than the client IP: there is one real username, and
an IP key lets an attacker on a home network rotate source addresses. It also means a
sustained attack locks the owner out for 15 minutes — acceptable for an application only
reachable over a private network.

### Bootstrap password

`PORTFOLIO_BOOTSTRAP_PASSWORD` is a `SecretStr | None`. When it is set and no user exists,
the lifespan creates the user named by `PORTFOLIO_BOOTSTRAP_USERNAME` (default `owner`).
When it is set and a user already exists it is ignored, so leaving it in the env file does
not reset the password on every deploy.

Whenever it is set it must satisfy the policy in `domain/passwords.py`, applied by a
Pydantic validator so the failure happens at settings construction and the application
refuses to start:

- at least 12 characters;
- not blank or whitespace only;
- not in the deny list: `changeme`, `password`, `admin`, `portfolio`, `secret`, `letmein`
  and the obvious digit runs, compared case-folded.

`create-user` applies the identical policy, from the same module.

## API contract

All bodies are `application/json`. No endpoint here returns a monetary value.

| Method | Path | Request | Success | Errors |
|---|---|---|---|---|
| POST | `/api/auth/login` | `{"username": str, "password": str}` | `204`, `Set-Cookie` | `401` invalid credentials, `429` too many attempts, `403` origin/content-type, `422` malformed |
| POST | `/api/auth/logout` | empty | `204`, cookie cleared | `401` no session |
| GET | `/api/auth/session` | — | `200 {"username": str}` | `401` no or expired session |
| POST | `/api/auth/password` | `{"current_password": str, "new_password": str}` | `204`, every session revoked, cookie cleared | `401` wrong current password, `422` new password fails policy |

Operation ids: `login`, `logout`, `getSession`, `changePassword`. Errors are RFC 9457
problem documents through the existing `AppError` machinery.

`POST /api/auth/password` revokes the caller's own session too. The frontend treats the
`204` as a logout; that is #4's problem, and the spec for it will say so.

**A requirement #4 must know about, found in review.** `Content-Type: application/json` is
required on *every* non-GET request, including `POST /api/auth/logout`, which has an empty
body. A `fetch` with no body sends no `Content-Type` and gets a `403`, and the generated
client declares `requestBody?: never` for that operation — so the natural call fails. The
middleware is deliberately *not* relaxed for bodyless requests: exempting them would reopen
the gap the content-type rule exists to close, for convenience. #4 sends the header
explicitly on logout.

## Data model

**No new tables, no new columns, no migration.** `0001_initial_schema` already created
everything this needs. `sessions.user_id` is already indexed and `sessions.token_hash` is
already unique, which is the lookup this change performs on every request.

## Acceptance criteria

Verbatim from the issue, numbered.

1. Argon2id hashing via `argon2-cffi`, with parameters tuned on the Raspberry Pi itself
   rather than copied from cloud defaults (target roughly 250ms).
   *Interpretation:* the parameters are configurable, floored at the OWASP minimum, and
   `hash-benchmark` measures them on the host. The measurement on the Pi is an operational
   step recorded in `docs/operations.md`, not something this diff can perform. See Risks.
2. `python -m portfolio create-user` prompts for the password; it is never taken from a
   command-line argument or committed config.
3. Session tokens are `secrets.token_urlsafe(32)`, stored **hashed**, so a database leak
   yields no usable session.
4. Two expiries: sliding idle (7 days) and hard absolute (30 days).
5. Cookie is `__Host-psid`, `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, no `Domain`.
   *Interpretation:* the name degrades to `psid` when `session_cookie_secure` is false,
   which `prod` refuses. See Design.
6. Non-GET requests require a matching `Origin` **and** `Content-Type: application/json`;
   a cross-origin POST and a form-encoded POST are both rejected, each with a test.
7. Login throttling: 6th failure within 15 minutes is rejected.
8. Unknown user and wrong password return the same error and take the same time.
9. Logout revokes server-side: replaying the cookie returns 401.
10. Changing the password revokes every session.
11. A contract test walks every registered route and asserts 401 without a cookie, with an
    explicit public allowlist, so a new unprotected endpoint fails CI.
12. The application refuses to start with an empty or default bootstrap password.

## Test plan

| # | Criterion | Test |
|---|---|---|
| 1 | Argon2id, real parameters | `tests/auth/test_password_hasher.py::test_hash_is_argon2id_with_configured_parameters` |
| 1 | Floor cannot be lowered | `tests/auth/test_password_hasher.py::test_configured_parameters_meet_the_owasp_floor` |
| 1 | Verify rejects a wrong password | `tests/auth/test_password_hasher.py::test_verify_rejects_a_wrong_password` |
| 1 | Rehash on parameter change | `tests/auth/test_password_hasher.py::test_hash_needs_update_when_parameters_rise` |
| 2 | Prompts, never an argument | `tests/cli/test_create_user.py::test_create_user_prompts_and_never_accepts_a_password_argument` |
| 2 | Confirmation must match | `tests/cli/test_create_user.py::test_create_user_rejects_a_mismatched_confirmation` |
| 2 | Policy applies | `tests/cli/test_create_user.py::test_create_user_rejects_a_password_below_policy` |
| 2 | Refuses a second user | `tests/cli/test_create_user.py::test_create_user_refuses_when_a_user_exists` |
| — | `--replace` cascades to sessions | `tests/cli/test_create_user.py::test_create_user_replace_deletes_the_existing_user_and_its_sessions` |
| — | `--replace` still prompts | `tests/cli/test_create_user.py::test_create_user_replace_still_prompts_for_the_password` |
| — | `--replace` needs confirmation | `tests/cli/test_create_user.py::test_create_user_replace_aborts_when_the_confirmation_does_not_match` |
| 3 | Token shape and entropy | `tests/auth/test_sessions.py::test_issued_token_is_url_safe_and_32_bytes` |
| 3 | Plaintext is never stored | `tests/auth/test_sessions.py::test_database_never_contains_the_plaintext_token` |
| 4 | Idle expiry rejects | `tests/auth/test_sessions.py::test_session_expires_after_the_idle_window` |
| 4 | Activity slides the idle window | `tests/auth/test_sessions.py::test_activity_slides_the_idle_window` |
| 4 | Absolute ceiling is not moved | `tests/auth/test_sessions.py::test_activity_cannot_push_past_the_absolute_expiry` |
| 4 | `last_seen_at` write is throttled | `tests/auth/test_sessions.py::test_last_seen_is_not_written_on_every_request` |
| 5 | Every cookie attribute | `tests/auth/test_login.py::test_cookie_carries_every_required_attribute` |
| 5 | Name degrades when insecure | `tests/auth/test_login.py::test_cookie_name_drops_the_host_prefix_when_insecure` |
| 5 | `prod` refuses insecure | `tests/auth/test_startup.py::test_prod_refuses_an_insecure_session_cookie` |
| 6 | `prod` refuses the dev origin | `tests/auth/test_startup.py::test_prod_refuses_the_development_allowed_origin` |
| 6 | Cross-origin POST | `tests/auth/test_request_guards.py::test_cross_origin_post_is_rejected` |
| 6 | Missing Origin | `tests/auth/test_request_guards.py::test_post_without_an_origin_is_rejected` |
| 6 | Form-encoded POST | `tests/auth/test_request_guards.py::test_form_encoded_post_is_rejected` |
| 6 | Charset parameter allowed | `tests/auth/test_request_guards.py::test_json_content_type_with_charset_is_accepted` |
| 6 | GET is exempt | `tests/auth/test_request_guards.py::test_get_is_not_subject_to_the_origin_check` |
| 7 | 6th failure rejected | `tests/auth/test_throttling.py::test_sixth_failure_within_the_window_is_rejected` |
| 7 | Rejected before verification | `tests/auth/test_throttling.py::test_throttled_attempt_does_not_verify_the_password` |
| 7 | Window expires | `tests/auth/test_throttling.py::test_failures_outside_the_window_do_not_count` |
| 7 | Success clears the counter | `tests/auth/test_throttling.py::test_successful_login_clears_the_failure_counter` |
| 8 | Identical problem document | `tests/auth/test_login.py::test_unknown_user_and_wrong_password_are_indistinguishable` |
| 8 | Comparable timing | `tests/auth/test_login.py::test_unknown_user_and_wrong_password_take_similar_time` |
| 9 | Replay returns 401 | `tests/auth/test_login.py::test_logout_revokes_the_session_server_side` |
| 10 | Password change revokes all | `tests/auth/test_password_change.py::test_changing_the_password_revokes_every_session` |
| 10 | Wrong current password | `tests/auth/test_password_change.py::test_wrong_current_password_is_rejected` |
| 10 | New password meets policy | `tests/auth/test_password_change.py::test_new_password_must_meet_the_policy` |
| 11 | Route contract | `tests/auth/test_route_contract.py::test_every_api_route_requires_a_session` |
| 11 | Allowlist is exact | `tests/auth/test_route_contract.py::test_public_allowlist_contains_only_expected_paths` |
| 12 | Empty bootstrap password | `tests/auth/test_startup.py::test_empty_bootstrap_password_refuses_to_start` |
| 12 | Default bootstrap password | `tests/auth/test_startup.py::test_default_bootstrap_password_refuses_to_start` |
| 12 | Bootstrap creates the user once | `tests/auth/test_startup.py::test_bootstrap_creates_the_user_only_when_none_exists` |

Plus, not from a criterion but from the rules: `tests/security/test_no_float.py` already
walks `services/` and `providers/`, and the new modules fall under it automatically.

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/**`, `backend/pyproject.toml`, `backend/uv.lock`, `backend/.importlinter`, `frontend/src/api/generated/schema.ts` |
| tester | `backend/tests/**` |
| tech-lead | `docs/**`, `deploy/compose.yml`, `CLAUDE.md` |
| reviewer | nothing |

`frontend/src/api/generated/schema.ts` is a generated artifact, not frontend work: it is
regenerated by `npm run gen:api` after the backend schema changes, and the OpenAPI drift
job fails without it. It belongs to whoever changed the schema.

The tester does not edit `backend/pyproject.toml`. If a test needs a dependency or a
coverage setting, ask the tech lead, who routes it to backend-dev. Two agents editing that
file is the exact collision this table exists to prevent.

## Risks

- **The Argon2id parameters are not measured on the Pi in this change.** There is no Pi in
  CI and no staging environment, so the defaults are a documented estimate for a Cortex-A76
  and the real number has to be taken after deployment with `hash-benchmark`. If the Pi
  comes in far from 250 ms, the fix is an environment variable and a restart, not a code
  change — which is why the parameters are settings.
- **The throttle window does not survive a restart.** See Design. A deployment clears it.
- **`PORTFOLIO_ALLOWED_ORIGIN` becomes required in production.** It defaults to the Vite dev
  server, so a deployment that does not set it would reject every write with `403`.
  Mitigated by refusing to start instead — see the correction in Design. The first
  deployment after this merges **will** fail and roll back unless the operator adds the
  variable to the host's secrets env file first; `docs/operations.md` carries that step, and
  it is the one manual action this change requires.
- **Making `/api/openapi.json` non-public** could break a tool that fetches it
  unauthenticated. Nothing in this repository does: the drift job dumps the schema in
  process.
- **The timing-equality test is the flakiest thing in this change.** It is written as a
  ratio with a generous bound and a warm-up, and it is the first test to look at if CI goes
  intermittently red.
- **Coverage floor is 98 and only ratchets.** This change adds a lot of branchy security
  code; the tester is responsible for the floor holding, and for raising it if the measured
  number lands materially above it.

## What the spec got wrong

Recorded because this spec was written before any of the code existed, and pretending it
was right would waste the next person's time.

### Two claims that were false as written

- **`PORTFOLIO_ALLOWED_ORIGIN` could not be a required compose variable.** The script that
  supplies those variables lives on the Pi, not in this repository — `remote_deploy.py` only
  invokes it over SSH — so a `${VAR:?}` entry nothing sets would have failed every
  deployment at `docker compose up`. Replaced with a startup refusal, which is also the
  better failure: it fails the health check and rolls back, rather than producing a
  container that reports healthy and rejects every write with a `403`.
- **The Argon2id floor did not exist.** The spec said a test asserts the parameters meet the
  OWASP floor. The test that was written inspects `Settings.model_fields[...].default` — the
  *shipped defaults*, which no environment variable can reach — while `docs/operations.md`
  told the operator that a value below the floor "refuses to start". An operator reading KiB
  as MiB and setting `PORTFOLIO_ARGON2_MEMORY_COST=64` would have run production on a hash
  a thousandfold weaker than intended, with everything green. The floor is now a startup
  refusal, gated on `prod`.

### Where the design was right but the prescription was wrong

- **The dummy hash.** The spec said "a module-level dummy hash generated at import". That is
  wrong on cost — a module constant cannot use the *configured* parameters, so its timing
  would not match a real verification, which defeats its purpose. The implementation used a
  lazy `cached_property` instead, which fixed that and introduced a timing oracle: the first
  unknown-username login in a process paid `hash` **and** `verify` where a wrong password
  paid only `verify`, a 2× signal once per process. Neither the spec's version nor the
  implementation's was right. The answer is a third thing the spec did not consider —
  compute it lazily, but **warm it at startup**, in the lifespan rather than in `create_app`,
  because the image runs `create_app()` as a build-time smoke check.
- **"A contract test walks `app.routes`."** FastAPI 0.141 stopped flattening an included
  router into `app.routes`, so the obvious `isinstance(route, APIRoute)` walk finds only the
  framework's own documentation endpoints — and passes, having checked nothing. The walk is
  duck-typed, and a companion test asserts it finds every route the OpenAPI schema declares.

### What the spec did not think of at all

- **`create-user --replace`.** No password reset by design, plus `create-user` refusing when
  a user exists, meant a forgotten password bricked the instance. Added as a stated scope
  addition.
- **`POST /api/auth/password` needed the throttle more than login did.** The spec put
  throttling on login only. The password-change endpoint is the one where a correct guess is
  *terminal*, since there is no reset flow — so it was the single endpoint most worth brute
  forcing and the only one nothing counted.
- **A per-username counter never fires against an attacker who varies the username.**
  Twenty concurrent logins under twenty names left the throttle untripped, each paying a
  full Argon2id verification on a `--workers 1` container. A second unkeyed counter bounds
  it; unlike a size cap it cannot be evicted.
- **WebSocket routes bypass the middleware entirely.** `BaseHTTPMiddleware` passes any
  non-HTTP scope straight through, and the route walk skips anything without `methods`. Not
  exploitable — there are no websocket routes — but `CLAUDE.md` rule 8 is written
  unconditionally, so the contract test now fails on the existence of one.
- **A production-gated startup refusal breaks the image build.** The Dockerfile sets
  `PORTFOLIO_ENVIRONMENT=prod` and then imports `create_app()` as its last build step, to
  prove the image can serve what it ships — so every refusal gated on production is
  evaluated during `docker build`, with none of the deployment environment present. The
  `PORTFOLIO_ALLOWED_ORIGIN` refusal therefore failed the arm64 build while all twelve
  local checks were green, because the whole suite runs at `environment="dev"`. The smoke
  check now supplies a reserved `.invalid` origin inline, and
  `backend/tests/test_image_configuration.py` parses the Dockerfile and reproduces the
  construction, so the next one fails in milliseconds instead of after a
  multi-architecture build.
- **Making `/api/openapi.json` non-public broke an existing test.** The Risks section
  worried about external tooling and missed the test in this repository.
- **Coverage was measuring the wrong thing**, and had been since the project started using
  async SQLAlchemy. Its asyncio layer runs inside a greenlet, so `services/auth.py` reported
  58% while demonstrably running end to end. Naming a concurrency library then switches off
  the thread tracing that `api/dependencies.py` needs, so both are named. Note the
  direction: the previous numbers were **understated**, not overstated — untraced lines were
  counted as misses — so the 98 floor was being cleared despite the handicap and is not
  comparable to the measurement behind the 97→98 ratchet.

### Test plan, as built

The table above names 41 tests. The suite collects **568**, from 326 test functions of
which **108 are new** — the gap between the two counts is parametrisation, mostly over the
password policy and the money types.

Three of the tests the plan named passed for the wrong reason, each deriving its expectation
from the constant it was checking:

- **Throttling asserted the mechanism, never the numbers.** `LOGIN_FAILURE_LIMIT` could be
  raised from 5 to 50 — turning the throttle off — with a green suite.
- **The 30-day ceiling was unpinned in both directions.** 30 → 3650 and 30 → 1 both passed,
  because the ceiling tests write `expires_at` into the past by hand and never observe what
  a login stores.
- **The timing test could not detect the leak it names.** At the suite's deliberately cheap
  parameters a verification is 1.68% of a request, so deleting the dummy verification
  entirely moved the ratio from 1.04 to 1.02 — well inside the band — and left all 103 auth
  tests green. Replaced by counting Argon2 operations directly.

That is the fourth, fifth and sixth time on this milestone that mutating the implementation
caught something review-by-reading did not. A spec that names a test is not the same as a
spec that says what the test must assert, and the gap between those two is where this kind
of defect lives.
