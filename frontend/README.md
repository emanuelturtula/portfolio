# Portfolio frontend

React + TypeScript + Vite frontend for the crypto portfolio tracker.

Signed in, there are three pages: the dashboard at `/`, showing the total portfolio
value, one row per held asset and one row per wallet with its freshness;
`/wallets`, where an address is registered, archived or restored; and
`/exchanges`, showing each configured venue's sync status, a banner where a retention
window has truncated its history, a manual **Sync now** trigger, and the run log.
`/health` is still the reference page for the four-state rendering pattern below.

## Requirements

Node 24 or newer (the repository is developed on Node 24.14.0 with npm 11).

## Scripts

| Script                  | What it does                                             |
| ----------------------- | -------------------------------------------------------- |
| `npm run dev`           | Vite dev server, proxying `/api` to the local backend.   |
| `npm run build`         | Type-checks the project, then builds it into `dist/`.    |
| `npm run preview`       | Serves the production build locally.                     |
| `npm run lint`          | ESLint, flat config, type-aware rules.                   |
| `npm run format:check`  | Prettier check (no writes).                              |
| `npm run typecheck`     | TypeScript only, no emit.                                |
| `npm test`              | Vitest, single run.                                      |
| `npm run test:coverage` | Vitest with V8 coverage and the configured thresholds.   |
| `npm run gen:api`       | Regenerates API types from the backend OpenAPI document. |

## API types

`npm run gen:api` reads the OpenAPI document from `../openapi.json` - a file, not a running
server - and writes `src/api/generated/schema.ts`. That file is written by running
`uv run python ../scripts/dump_openapi.py` from `backend/`, which imports the FastAPI app
and dumps its schema without binding a socket at all. `openapi.json` is gitignored at the
repository root, because it is a build artifact and would otherwise drift from the backend
that actually ships. CI does the same two steps in order, so generated types are always
produced from the real schema, never hand-edited and never read from a checked-in copy.

## Conventions this skeleton sets

- **Money is never a JavaScript number.** Monetary values arrive as JSON strings
  and stay strings until a decimal-aware helper handles them. ESLint blocks
  `parseFloat`, `parseInt` and `Number()` in `src/features/**` and `src/lib/**`
  to keep that true; see the comment in `eslint.config.js`.
- **Every data-driven view renders four states.** Loading, empty, error and
  success are each rendered explicitly, with an accessible live region for the
  wait and `role="alert"` for the failure - and empty and error are kept
  visibly distinct, because "you have not added anything yet" and "the sync
  failed" mean opposite things. `src/pages/HealthPage.tsx` is the three-state
  (no empty case) reference; the dashboard and the wallets page are the
  four-state ones.
- **A missing value is never rendered as zero.** An unread wallet, an unpriced
  asset or a stale price is stated in text - never silently shown as `0`. See
  `docs/specs/011-wallets-page-value-dashboard.md`.
- **Errors are problem documents.** The backend speaks
  `application/problem+json` (RFC 9457) and `src/api/client.ts` maps it onto a
  typed `ApiError`, including a defensive parse of a 422's field-level
  `errors[]`.
- **Coverage thresholds only ratchet upward.** They start at 60% because the
  skeleton is small; never lower them to make a build pass.
