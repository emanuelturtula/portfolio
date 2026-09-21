# 004 — Login page and authenticated application shell

Issue: #4
Status: draft

## Problem

#3 shipped a complete authentication backend that nothing can reach. The frontend is still
the walking skeleton: one route, one unauthenticated page, and a `fetch` wrapper that cannot
read a `204 No Content` response — which is what every authentication endpoint answers with.
There is no way to sign in, no route that requires a session, and no place for a later page
to put an error or an empty state.

This change builds the shell every subsequent frontend issue hangs off: a login page, a route
guard, the three shared state primitives, and the money value object whose only job today is
to exist before anyone is tempted to write `parseFloat`.

## Scope

- A `POST /api/auth/login` form at `/login`, with invalid, throttled and unreachable states.
- Session bootstrap from `GET /api/auth/session`, and a route guard over every other route.
- A recovery path when a session dies mid-use: any `401` from any query returns the user to
  the login page without a crash or a blank screen.
- Logout from the application header.
- `ErrorState`, `EmptyState` and `Skeleton` in `src/components/`, used by the pages added here.
- `src/lib/money.ts` on `decimal.js`, a `<Money>` component, and the ESLint ban widened to
  cover unary `+` and the directories the money code actually lives in.
- The `apiFetch` gaps this exposes: `204` responses, and the `Content-Type` header on a
  bodyless write.

## Non-goals

- **The real dashboard.** `/` gets a placeholder that renders `EmptyState` and nothing more.
  Wallets and portfolio value are #11; invested-per-asset is #20.
- **A password-change screen.** The endpoint exists and revokes every session including the
  caller's, which makes it a flow with a forced re-login, not a form. It belongs with the
  settings page, not here.
- **A design system.** `index.css` stays hand-written and small. The project has no visual
  design yet and inventing one here would be a decision taken in the wrong place.
- **Security headers and a CSP** (#38). A CSP has to be written against the real bundle, so
  it is the natural follow-up to this change, but it is a backend change and a separate diff.
- **Refresh tokens or silent renewal.** A `401` means sign in again; the backend has a
  sliding 7-day idle window and no refresh token, and the frontend must not pretend otherwise.

## Design

### The session is one query, and it never rejects on 401

`useSession()` wraps `GET /api/auth/session` with a `queryFn` that catches `ApiError` with
status `401` and resolves to `null` instead of throwing. Every other failure — a network
error, a `500` — still rejects.

That single decision is what keeps the guard readable, because it collapses "signed out" and
"signed in" into `data`, and leaves `error` meaning only "we could not find out":

| State | Guard renders |
|---|---|
| `isPending` | `<Skeleton>` |
| `isError` | `<ErrorState>` with a retry button |
| `data === null` | `<Navigate to="/login" replace>`, carrying the attempted location |
| `data` | the protected route |

Rejected alternative: keep the `401` as an error and branch on `error.status` in the guard.
It works, but every future caller of `useSession` has to remember the same branch, and the
one that forgets renders an error page to a user who is merely signed out.

### A mid-session 401 is handled once, in the query cache

`createQueryClient` gains a `QueryCache` and a `MutationCache` whose `onError` checks for
`ApiError` with status `401` and, when it sees one, writes `null` into the session query's
cache entry. The guard is already watching that entry, so the redirect happens by the same
path as a cold start. Nothing calls `navigate` from outside the router, and no component has
to know about the rule.

`createQueryClient` therefore has to take the client it is configuring. It is built inside
the `QueryCache` callbacks via a closure over a `let`, which is the documented TanStack
pattern for a cache callback that needs the client.

Rejected alternative: an interceptor inside `apiFetch`. It would catch calls that bypass
TanStack Query too, but it puts routing state into the transport layer and makes `apiFetch`
untestable without a router.

### `apiFetch` cannot currently talk to the auth endpoints

Two real defects, both of which this change has to fix rather than work around:

1. **A `204` is treated as malformed.** `apiFetch` demands a JSON body and throws
   `ApiError('Malformed response')` when parsing yields nothing. Three of the four auth
   endpoints answer `204`. A new `apiSend()` returns `void` and does not read a body.
2. **A bodyless `POST` sends no `Content-Type` and is refused with `403`.** The header is set
   only when `options.body !== undefined`, and `POST /api/auth/logout` has no body. The
   backend's write guard requires `application/json` on every non-safe method and is
   deliberately not relaxed for bodyless requests, so the natural call fails.

The fix is at the transport, once: **any method outside `GET`/`HEAD`/`OPTIONS` sets
`Content-Type: application/json`, whether or not there is a body.** Fixing it at the call
site instead would mean every future write is one forgotten header away from a `403`, which
is exactly the trap #3 recorded and this is the chance to close.

Both `apiFetch` and `apiSend` delegate to one internal `request()` so the two cannot drift.

### Money exists before there is money to display

`src/lib/money.ts` wraps `decimal.js` behind a nominal type:

```ts
export type Money = string & { readonly __brand: 'Money' };
export function money(value: string): Money;       // validates, throws on junk
export function formatMoney(value: Money, options?): string;
export function addMoney(a: Money, b: Money): Money;
```

`Decimal.set({ precision: 40 })` at module load: the rule is an 18-decimal string round-trips
with no loss, and the default precision of 20 significant digits cannot hold one.

`<Money>` renders into a `<data>` element whose `value` attribute carries the unformatted
string, so the exact figure survives in the DOM even when the visible text is grouped and
truncated. The test asserts on that attribute, which is what makes "no precision loss"
checkable rather than asserted.

Nothing in this change displays money. That is the point: the helper and the lint rule land
before the first feature that would otherwise reach for `parseFloat`.

### The ESLint ban gets the two things it is currently missing

- **Unary `+`.** Named in the acceptance criteria, absent from the config. Added as a
  `no-restricted-syntax` selector, `UnaryExpression[operator="+"]`.
- **The directories money code will actually live in.** The ban covers `src/features/**` and
  `src/lib/**`. `<Money>` is a component, and every page that shows a balance is a page, so
  `src/components/**` and `src/pages/**` join the list. The original narrow scoping was
  justified by legitimate non-monetary parsing elsewhere; `src/api/**`, `src/test/**` and the
  config files stay outside the ban, and that is where such parsing belongs.

Test files are exempt — a test proving the rule fires has to be able to write the violation.

### Routes

| Path | Guard | Component |
|---|---|---|
| `/login` | public; redirects to `/` when a session already exists | `LoginPage` |
| `/` | protected | `DashboardPage` (placeholder) |
| `/health` | protected | `HealthPage`, moved off `/` |
| anything else | protected | `NotFoundPage` |

`LoginPage` redirecting away when already signed in is what stops the back button landing on
a login form the user cannot leave.

The guard preserves the attempted location in `Navigate` state and `LoginPage` returns there
after a successful sign-in, defaulting to `/`. Only same-origin paths are honoured: the value
must start with a single `/`, or it is discarded in favour of `/`. An open redirect through
router state is cheap to prevent and expensive to notice later.

### Files

**Created**

```
frontend/src/api/session.ts          useSession, login, logout, the session query key
frontend/src/components/ErrorState.tsx
frontend/src/components/EmptyState.tsx
frontend/src/components/Skeleton.tsx
frontend/src/components/Money.tsx
frontend/src/components/RequireSession.tsx
frontend/src/lib/money.ts
frontend/src/pages/LoginPage.tsx
frontend/src/pages/DashboardPage.tsx
frontend/src/pages/NotFoundPage.tsx
```

**Changed**

```
frontend/src/api/client.ts           apiSend, the Content-Type fix, one shared request()
frontend/src/lib/queryClient.ts      the 401 cache rule
frontend/src/App.tsx                 route table, header, logout
frontend/src/index.css               form, primitives, header layout
frontend/eslint.config.js            unary +, widened scope
frontend/package.json                decimal.js
frontend/vite.config.ts              coverage thresholds
```

## API contract

No endpoint is added or changed. Consumed as built in #3:

| Method | Path | Request | Success | Failures |
|---|---|---|---|---|
| `POST` | `/api/auth/login` | `{username, password}` | `204` + `__Host-psid` | `401` invalid, `429` throttled |
| `POST` | `/api/auth/logout` | none, **but `Content-Type: application/json`** | `204` | `401` |
| `GET` | `/api/auth/session` | — | `200 {username}` | `401` |

No monetary fields anywhere in this change.

**The issue names `GET /api/auth/me`. That endpoint does not exist.** The route registered in
`backend/src/portfolio/api/routers/auth.py` is `GET /api/auth/session`, operation `getSession`,
and the generated schema agrees. The spec follows the code; the issue text is wrong and the
pull request says so.

Every non-GET request also needs an `Origin` matching the backend's `allowed_origin`. In
development the browser supplies `http://localhost:5173`, which is the default, so nothing is
needed in the client — but it is why tests must not invent a different origin.

## Data model

None. No migration.

## Acceptance criteria

Verbatim from the issue, numbered, with interpretations marked.

1. Visiting any protected route while unauthenticated redirects to login.
2. A successful login lands on the dashboard; session is bootstrapped from `/api/auth/me`.
   **Interpretation:** from `GET /api/auth/session` — see API contract. "Lands on the
   dashboard" is read as: on the attempted location when the guard captured one, `/` otherwise.
3. A 401 mid-session redirects to login without crashing or blanking the page.
4. Shared `ErrorState`, `EmptyState` and `Skeleton` primitives exist and are used.
   **Interpretation:** "used" means used in shipped code, not only in tests — `Skeleton` by the
   guard, `ErrorState` by the guard and the login page, `EmptyState` by the dashboard placeholder.
5. `src/lib/money.ts` is backed by `decimal.js`; a `<Money>` component renders an 18-decimal
   string with no precision loss, proven by a test.
6. ESLint rejects `parseFloat`, `Number()` and unary `+` on money.
7. Tests cover invalid credentials, the throttled response and a network error.

## Test plan

Every test is a Vitest file beside its subject, driven through `@testing-library/react` with
MSW handlers. The tests render real components against a real `QueryClient` from
`createQueryClient()` — the shipped factory, not a test-only one.

| # | Criterion | Test |
|---|---|---|
| 1 | Unauthenticated → login | `src/components/RequireSession.test.tsx::redirects to the login page when the session query resolves to null` |
| 1 | Guard shows a skeleton first | `RequireSession.test.tsx::renders the skeleton while the session is pending` |
| 1 | Guard survives an unreachable backend | `RequireSession.test.tsx::renders an error state, not a redirect, when the session cannot be read` |
| 2 | Login lands on the dashboard | `src/pages/LoginPage.test.tsx::lands on the dashboard after a successful sign-in` |
| 2 | Returns to the attempted route | `LoginPage.test.tsx::returns to the route that triggered the redirect` |
| 2 | Open redirect is refused | `LoginPage.test.tsx::ignores a non-local return path` |
| 2 | Bootstrap reads the session endpoint | `src/api/session.test.ts::reads the signed-in username from /api/auth/session` |
| 2 | Already signed in skips the form | `LoginPage.test.tsx::redirects away from the login form when a session already exists` |
| 3 | Mid-session 401 | `src/lib/queryClient.test.ts::invalidates the cached session when any query fails with 401` |
| 3 | And the UI recovers rather than blanking | `src/App.test.tsx::returns to the login page when a protected query answers 401` |
| 4 | Primitives render and are accessible | `src/components/ErrorState.test.tsx`, `EmptyState.test.tsx`, `Skeleton.test.tsx` |
| 5 | 18 decimals survive | `src/lib/money.test.ts::keeps all eighteen decimals of a base-unit amount` |
| 5 | `<Money>` keeps the exact value in the DOM | `src/components/Money.test.tsx::carries the unformatted amount in the data value attribute` |
| 5 | Junk input is refused | `money.test.ts::rejects a value that is not a decimal number` |
| 6 | The lint rule fires | `src/lib/money.eslint.test.ts` — runs the real `eslint.config.js` over fixture source via the ESLint Node API and asserts an error for each of `parseFloat`, `parseInt`, `Number(x)` and `+x`, and **no** error for `Number.isFinite` |
| 7 | Invalid credentials | `LoginPage.test.tsx::shows the server's message when the credentials are refused` |
| 7 | Throttled | `LoginPage.test.tsx::shows the retry-later message on 429` |
| 7 | Network error | `LoginPage.test.tsx::shows an unreachable-backend message when the request fails outright` |
| 7 | The form does not submit twice | `LoginPage.test.tsx::disables the submit button while the request is in flight` |
| — | Logout | `App.test.tsx::signs out and returns to the login page` |
| — | The `204` fix | `src/api/client.test.ts::resolves without a body on 204` |
| — | The `Content-Type` fix | `client.test.ts::sends application/json on a write with no body` |

Criterion 6's test is the one to get right. It must assert the **absence** of an error for a
legitimate construct as well as its presence for a violation; a rule that fires on everything
passes a presence-only test and breaks the codebase.

### Mutation checks the tester must run

Per the standing lesson from #3 — a spec that names a test is not a spec that says what it
must assert. Each of these changes must turn a test red:

- Delete the `401 → null` catch in the session `queryFn`. Criterion 1's redirect test must fail.
- Delete the `onError` rule in `createQueryClient`. Criterion 3 must fail.
- Change the local-path check to accept `//evil.example`. The open-redirect test must fail.
- Drop `Decimal.set({ precision: 40 })`. The 18-decimal test must fail.
- Remove the unary `+` selector from `eslint.config.js`. The lint test must fail.
- Remove the unconditional `Content-Type` on writes. The logout test must fail.

## File ownership

| Agent | Owns |
|---|---|
| frontend-dev | `frontend/src/**` **except** `*.test.ts`, `*.test.tsx` and `src/test/**`; `frontend/package.json`; `frontend/package-lock.json`; `frontend/eslint.config.js` |
| tester | `frontend/src/**/*.test.ts`, `frontend/src/**/*.test.tsx`, `frontend/src/test/**`, `frontend/vite.config.ts` |
| tech-lead | `docs/**` |
| reviewer | nothing |

`frontend/vite.config.ts` goes to the tester because the only thing this change touches in it
is the coverage threshold, which is the tester's call. The tester does not edit
`package.json`: a test that needs a dependency asks the tech lead, who routes it to
frontend-dev. `eslint.config.js` belongs to frontend-dev because the rule is implementation;
the test that proves it fires belongs to the tester.

## Risks

- **`Decimal.set` is global process state.** A later module setting a different precision
  changes results everywhere, and the failure is silent. Mitigated by doing it in exactly one
  module and asserting the 18-decimal round-trip; a second `Decimal.set` anywhere in the
  codebase is a review finding.
- **The lint test runs ESLint in process.** It is the slowest test in the suite and it breaks
  when the config's shape changes rather than its meaning. Accepted: an untested lint rule is
  one that silently stops firing, which is the failure mode this project keeps finding.
- **MSW cannot reproduce the backend's `Origin` and `Content-Type` guard.** The tests assert
  the request the client *sends*; they cannot prove the backend accepts it. The end-to-end
  proof is a manual sign-in against the real API, which the verification step must actually
  perform rather than assume.
- **`jsdom` has no real cookie jar for `__Host-` prefixed, `Secure` cookies.** Tests therefore
  model session state through MSW handlers, not through cookies. This is a known blind spot:
  a cookie attribute regression cannot fail a frontend test, and #3's backend tests are what
  cover it.
- **Coverage floor is 60 on the frontend and only ratchets.** This change multiplies the
  frontend's size; the tester raises the floor to the measured value, and a measured number
  below 90 is a signal that the test plan above was not finished.
- **`react-router-dom` v7 with `StrictMode`** double-invokes effects in development. Anything
  written as an effect-driven redirect will fire twice; the guard is therefore declarative
  (`<Navigate>`), not an effect.
