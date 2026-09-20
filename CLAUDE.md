# Working agreement

A self-hosted crypto investment portfolio tracker. It reads balances from on-chain
addresses, imports spot trade executions from exchanges, and reports total value, value per
wallet, and how much has been invested in each asset.

Python 3.12 + FastAPI, React + TypeScript + Vite, SQLite, one Docker image, one production
instance on a Raspberry Pi 5 (arm64).

## The rules

These are not style preferences. Each one has a mechanical enforcement, listed beside it,
because a rule that only lives in a document is a rule that erodes.

### 1. Everything in English

Code, comments, docstrings, identifiers, documentation, commit messages, pull request
descriptions and UI strings. The repository is public and is read by people who do not
speak Spanish.

### 2. Money is never a float

`float` is banned in `domain/`, `services/` and `providers/`. Monetary values are:

| Where | Representation |
|---|---|
| Python | `decimal.Decimal` |
| SQLite | `TEXT`, via the `NumericText` type decorator |
| On-chain quantities | `INTEGER` base units (satoshis, sompi) plus a `decimals` column |
| Over the wire | a JSON **string** |
| TypeScript | a `string`, formatted with `decimal.js` |

`sqlalchemy.Numeric` round-trips through float on SQLite and will silently destroy
precision, so it is forbidden. Never aggregate money in SQL: `SUM()`, `ORDER BY` and
comparisons on a `TEXT` money column coerce to float. Aggregate in Python.

*Enforced by:* an AST test in `backend/tests/security/`, and an ESLint rule banning
`parseFloat`/`Number()` on money in the frontend.

### 3. Nothing sensitive in the repository

No API keys, no wallet addresses, no extended public keys, no private IP addresses, no
hostnames, no infrastructure usernames. Wallet addresses are runtime user data: they live
in SQLite on the Pi and are entered through the UI.

Test fixtures use **testnet** addresses only — `tb1`, `bcrt1`, `kaspatest:`, `tpub`. The
gitleaks rules are written to permit exactly those and reject their mainnet equivalents.

Credentials are read from environment variables into `SecretStr`, are never persisted to the
database, are never returned by any endpoint (only `configured: bool` and a status), and are
never logged. One exchange signs its requests in the query string, so the shared HTTP client
logs URLs with the query removed.

*Enforced by:* `.gitleaks.toml` + `scripts/secret_scan.py` (fails closed), pre-commit and
pre-push hooks, the `Secrets scan` CI job over full history, GitHub push protection, and a
`PreToolUse` hook that stops an agent writing one in the first place.

### 4. Business logic is never in a router

```
{ api.routers , cli } -> services -> { repositories , providers } -> db -> domain
domain                -> nothing
```

Routers parse, call a service, and serialize. They may not import `repositories`,
`providers`, `sqlalchemy` or `httpx`. Services may not import `fastapi`. `domain` is pure:
no I/O, no clock, no network, no ORM.

`cli` is a sibling of `api`, not a layer above it: they are two entry points onto the same
services, and neither imports the other. A dependency hands a router a fully built service
rather than a database session, which is what keeps `sqlalchemy` out of `api.routers`
without anyone having to remember the rule.

`db` sits *above* `domain` rather than beside it: a column type has to round money by the
same rule the domain defines, and two copies of a rounding rule is how they drift apart.
The direction that matters is the one that has not changed — `domain` imports nothing.

*Enforced by:* `import-linter` contracts in `backend/.importlinter`, run in CI.

### 5. Coverage thresholds only ratchet upward

They start deliberately low for the skeleton and rise as the code becomes worth testing.
Lowering one requires an explicit justification in the pull request description.

### 6. Every change lands via a pull request

Branch `feature/<issue>-<slug>`, squash merge, Conventional Commit title. The squash subject
is what `scripts/next_version.py` reads to mint the version, so `feat:` means a minor bump
and `fix:`/`chore:` means a patch. Direct commits to `main` are blocked.

### 7. No `pull_request_target`, ever

It is the one trigger that runs fork-authored code with access to repository secrets, and it
is how public repositories get compromised. Nothing here needs it.

### 8. Endpoints are authenticated unless they are on the allowlist

Authentication is deny-by-default, in middleware, not a `Depends` on each router.
Forgetting a dependency is the failure this exists to prevent, so the rule is written to be
unforgettable rather than merely documented: any path under `/api` that is not in
`PUBLIC_API_PATHS` requires a valid session.

Adding an endpoint therefore protects it. Making one public is an edit to a named constant,
which is a visible line in a diff and a deliberate act.

*Enforced by:* a contract test that walks every registered route and asserts `401` without
a cookie, plus a second test pinning the allowlist's exact contents.

## Running things

```bash
python scripts/check.py              # the full gate: lint, types, layering, tests, secrets
python scripts/check.py --fast       # quick version, used by the agent stop hook
python scripts/check.py --backend    # one side only
```

```bash
cd backend  && uv sync && uv run uvicorn portfolio.main:create_app --factory --reload
cd frontend && npm install && npm run dev
```

The frontend dev server proxies `/api` to the backend on port 8000.

## Deployment

Merging to `main` builds an arm64 image, pushes it to GHCR by digest, deploys it to the Pi
over Tailscale, verifies the container is healthy, and only then tags the image `vX.Y.Z` and
`latest` and cuts a release. The host-side script backs up SQLite before replacing anything
and rolls back if the new container does not become healthy.

There is no staging environment. Nothing proves the artifact runs on real hardware before
merge, which is why the rollback path exists and why it is tested rather than assumed.

## Working on an issue

`/work-issue <N>` in an interactive session. The tech lead reads the issue, writes a spec to
`docs/specs/`, spawns the teammates the issue's labels call for, and drives through to an
open pull request. See `.claude/agents/` for the roles and `.claude/skills/` for each step.

Two agents editing one file overwrite each other, so the tech lead assigns disjoint file
ownership before any implementation starts.
