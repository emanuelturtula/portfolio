# Portfolio frontend

React + TypeScript + Vite walking skeleton for the crypto portfolio tracker. It
renders one page, which calls `GET /api/health` on the backend and displays the
result. There is no authentication, no dashboard and no other data fetching yet:
the point is to prove the toolchain end to end.

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

`npm run gen:api` reads the OpenAPI document over HTTP from a backend listening
on the loopback interface and writes `src/api/generated/schema.ts`. The schema is
never read from a checked-in file in the backend folder, because that file drifts
from the running service. CI does the same thing: it starts the backend (or
serves a schema the backend dumped itself) on the loopback address and then runs
the script, so generated types are always produced from a real schema.

## Conventions this skeleton sets

- **Money is never a JavaScript number.** Monetary values arrive as JSON strings
  and stay strings until a decimal-aware helper handles them. ESLint blocks
  `parseFloat`, `parseInt` and `Number()` in `src/features/**` and `src/lib/**`
  to keep that true; see the comment in `eslint.config.js`.
- **Every query renders three states.** Pending, error and success are each
  rendered explicitly, with an accessible live region for the wait and
  `role="alert"` for the failure. `src/pages/HealthPage.tsx` is the reference.
- **Errors are problem documents.** The backend speaks
  `application/problem+json` (RFC 9457) and `src/api/client.ts` maps it onto a
  typed `ApiError`.
- **Coverage thresholds only ratchet upward.** They start at 60% because the
  skeleton is small; never lower them to make a build pass.
