# 018 — One live deployment at the root, one backup, and commands that work

Issue: #94
Status: implementing

## Problem

The owner edited `secrets.env` on the Pi and could not apply it.
- Every `docker compose` command in `docs/operations.md` points at
  `<deploy-root>/compose.yml`, a file that does not exist. Each deployment's compose file
  lives in `prod/attempts/<id>/`.
- Even with the right file, compose refuses to run without four variables that only
  `deploy.py` sets.
- `attempts/` holds ten directories, each with **a full copy of the database**, and reads as
  a pile of replicas.
- The root's name, `portfolio-app-deploy`, sits awkwardly beside the legacy
  `portfolio-deploy`.

## Scope

- **The root becomes `~/portfolio-app`.** A host on `~/portfolio-app-deploy` is migrated by
  the next deployment.
- **The environment directory holds the live deployment and exactly one backup:**

  ```
  ~/portfolio-app/
    deploy.lock
    prod/
      compose.yml         the live deployment's compose file
      current.json        the live deployment's manifest
      secrets.env         operator-managed, 0600, never read by deploy.py
      compose.sh          docker compose against the live deployment
      last-attempt.json   the latest attempt's request and outcome
      backup/             the previous deployment, and the database as it was before the live one
        compose.yml
        current.json
        database.sqlite3
      failed/             only after a failed deployment: its evidence, overwritten by the next failure
        compose.yml  request.json  result.json  database.sqlite3
  ```
- `compose.sh`, rewritten by every successful deployment.
- `docs/deployment.md` and `docs/operations.md`: the layout, and every command made to work.

## Non-goals

- **Restoring the database on rollback.** Today's behaviour is kept: a failed deployment
  restarts the previous image against the live database. The backup is there for a person to
  restore by hand, as before.
- **More than one backup.** The owner decided: one is enough, and fewer copies of the
  owner's data on disk is the point.
- **The legacy `portfolio-deploy` application.** It is untouched.
- **Anything that changes what is deployed or how it is verified.** Rerun protection, the
  provenance labels, the integrity-checked backup and the healthy-and-exact-digest check stay
  exactly as they are.

## Design

### Paths come from the layout, never from a stored absolute path

Manifests today store absolute paths (`compose`, `secrets_env_file`, `attempt`), and a
rename of the root makes every one of them wrong. So:
- **Every path is computed from the root and the layout at the moment it is used.** A
  manifest's stored paths are kept as a record but are never read back.
- **Legacy manifests are the one exception**, because their compose file is wherever the
  attempt put it. Their `compose` path is rebased from the old root onto the new one.
- `compose()` takes the compose file and the secrets file as arguments, not from the
  manifest.

### The migration, under the lock

`deploy()` resolves the roots and takes the lock before it looks at anything else:

1. If `~/portfolio-app` does not exist and `~/portfolio-app-deploy` does, the lock is taken
   **in the old root**. Then the old root is renamed to the new one, with a single
   `os.rename` on one filesystem. The lock's open file moves with the directory, so the
   lock stays held.
2. Otherwise the lock is taken in the new root, which is created if absent.
3. **A process that acquires the lock re-resolves the root.** If it waited on the old root's
   lock and that root is now gone, it continues in the new one. Its lock file is the same
   inode, so serialisation holds.
4. If both roots exist, it is refused with an error naming both, so a person decides. It is
   never merged silently.

### A deployment, step by step

1. Validate, as today.
2. Take the lock and migrate, as above.
3. **Read the previous deployment**, `prod/current.json`, if it exists, and find its compose
   file:
   - new layout: `prod/compose.yml`;
   - legacy layout: the `attempts/<id>/compose.yml` its manifest names, rebased onto the new
     root.

   If that file is missing, refuse before touching anything: the rollback target must exist.
4. Rerun protection, pull, and provenance labels, **unchanged**.
5. **Stage the candidate in `prod/incoming/`**, clearing any stale one first: the compose
   file and `request.json`. Check the secrets file, as today.
6. **Back up** the running database into `prod/incoming/database.sqlite3`. The backup
   function and its integrity check are unchanged; only the destination moves.
7. `up` the candidate from `prod/incoming/compose.yml`, and verify it, as today.
8. **On failure**, roll back, exactly as today, using the previous deployment's compose file.
   Then:
   - write `incoming/result.json`;
   - replace `prod/failed/` with `prod/incoming/`;
   - write `prod/last-attempt.json`;
   - raise.

   `prod/compose.yml`, `current.json` and `backup/` are untouched, so they still describe what
   is running.
9. **On success**, rotate:
   1. Build `prod/backup.new/` from the previous deployment's compose file, the previous
      manifest (as `current.json`) and `incoming/database.sqlite3`. Skip whatever the first
      deployment on a host does not have.
   2. Replace `prod/compose.yml` with the candidate's, atomically.
   3. Replace `prod/current.json` with the candidate's manifest, atomically.
   4. Swap `backup.new` in for `backup`: rename the old one aside, rename the new one in,
      then delete the old one.
   5. Write `prod/compose.sh` atomically, mode 0700.
   6. Write `prod/last-attempt.json`.
   7. Delete `prod/incoming/`, `prod/failed/` and the legacy `prod/attempts/`, if present. A
      success supersedes a failure's evidence. On a migrated host, this is the step that
      removes the ten old database copies.

**After a successful deployment exactly one database backup exists on the host**, in
`prod/backup/`. During a failed deployment's aftermath there can be two: the backup, and the
failure's own snapshot in `failed/`. The failure's snapshot is the pre-deployment database,
which is the copy that matters if a migration went wrong.

**Every JSON file and `compose.sh` are written atomically**: a temporary file, then
`replace`. A crash mid-rotation leaves either the old or the new file, never a torn one. The
order above means a crash leaves at worst `current.json` describing the previous deployment
while the candidate runs. The next deployment then finds the running container does not
match that manifest, skips its backup (existing behaviour), and deploys normally.

### `compose.sh`

```sh
#!/bin/sh
# Written by deploy.py for the live deployment: docker compose against it.
#   ./compose.sh up -d --force-recreate app   apply a secrets.env change
#   ./compose.sh ps
#   ./compose.sh exec app python -m portfolio create-user --username <name>
set -eu
cd "$(dirname "$0")"
export PORTFOLIO_IMAGE='ghcr.io/emanuelturtula/portfolio@sha256:<64 hex>'
export PORTFOLIO_PORT='8083'
export PORTFOLIO_ENVIRONMENT='prod'
export PORTFOLIO_SECRETS_ENV_FILE="$PWD/secrets.env"
exec docker compose --project-name portfolio-app-prod --file "$PWD/compose.yml" "$@"
```

- It embeds only values `validate()` has already accepted: a digest matching `IMAGE`, the port
  from `ENVIRONMENTS`, and an environment name from `ENVIRONMENTS`. No quoting problem can
  arise.
- Paths are relative to the script, so a future rename of the root does not break it.
- It carries no secret.

### Docs

- `docs/deployment.md`: the layout above, the migration, and "one backup" with the reason.
  `~/portfolio-app/prod/secrets.env` everywhere.
- `docs/operations.md`:
  - every `docker compose … -f <deploy-root>/compose.yml …` becomes
    `~/portfolio-app/prod/compose.sh …`;
  - `<deploy-root>` is redefined;
  - the recreate instruction reads `~/portfolio-app/prod/compose.sh up -d --force-recreate app`.
- The `deploy.py` module docstring describes the new layout.

## API contract

None.

## Data model

None. The data volume is a Docker named volume, so the rename does not touch it.

## Acceptance criteria

These are from the issue:

1. A fresh host, a host on the old root and old layout, and a host already on the new layout
   each deploy correctly; the migrations are tested.
2. A failed deployment rolls back to the deployment in `prod/`, whether or not that deployment
   was made under the old layout.
3. After a successful deployment, exactly one database backup exists on the host.
4. `compose.sh` works for `up --force-recreate`, `ps` and `exec`, and embeds nothing but
   validated values.
5. Every `docker compose` command in the docs is replaced by one that works.
6. Rerun protection, provenance checks and the healthy-and-exact-digest check are unchanged.

## Test plan

The existing tests exercise pure logic only, on the grounds that a mock of Docker proves
nothing about Docker. That still holds: whether compose works is proven by the real
deployment that merging this triggers. The file choreography, though, is ours, and a fake
Docker **does** prove it. So the tester adds a fake `run()` that records every command and
answers `ps`, `inspect`, `exec` (the backup), `cp`, `pull` and `up` from a scripted host
state, over a temporary directory standing in for `$HOME`.

| # | Test (in `tests/deploy/`) | Must assert |
|---|---|---|
| 1 | fresh host | creates `~/portfolio-app/prod/`, no backup directory contents (no previous), `compose.yml`, `current.json`, `compose.sh`, `last-attempt.json`; no `incoming/` left |
| 1 | old root, old layout | `~/portfolio-app-deploy` renamed; `secrets.env` preserved **byte for byte and still 0600**; the rollback target is the rebased `attempts/<id>/compose.yml`; after success `attempts/` is gone and `backup/` holds the legacy compose file, the legacy manifest and the new snapshot |
| 1 | new layout, second deploy | `backup/` replaced; its `database.sqlite3` is the new snapshot, not the old |
| 1 | both roots exist | refused, nothing renamed, nothing run |
| 1 | the lock survives the rename | the lock is taken in the old root, then renamed; a second process waiting on the old path re-resolves to the new root (simulated by calling the resolution step with the old path after the rename) |
| 2 | failure on each layout | rollback `up` uses the previous deployment's compose file (legacy and new); `failed/` holds compose, request, result and the snapshot; `compose.yml`, `current.json` and `backup/` are byte-identical to before |
| 2 | a missing rollback target | refused before `pull` |
| 3 | one backup | after two successful deployments, exactly one `*.sqlite3` under the root |
| 4 | `compose.sh` | its exact text for a fixed digest; mode 0700; it contains no `secrets.env` content (the file is filled with a sentinel); running it with a fake `docker` on `PATH` receives the project name, the file, the four variables and the arguments as given (`up -d --force-recreate app`, `ps`, `exec app python -m portfolio create-user --username x`) |
| 5 | docs | `tests/deploy/` or a doc test: no `-f <deploy-root>/compose.yml` and no `portfolio-app-deploy` left in `docs/`, except in the migration paragraph; every command names `compose.sh` |
| 6 | unchanged guards | the existing validate, rerun-order tests pass unchanged; a digest mismatch after `up` still rolls back |
| — | atomic writes | a failure injected between each rotation step leaves every JSON file parseable, and `compose.yml` / `current.json` a matching pair or the previous pair |

`test_deploy.py`'s pruning tests go, with `prune_attempts`.

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `deploy/deploy.py`, `deploy/compose.yml` (comments only), `docs/deployment.md`, `docs/operations.md` |
| tester | `tests/deploy/**` |
| tech lead | `docs/specs/018-deploy-layout.md` |
| reviewer | nothing |

## Risks

- **This runs against production on merge, and the first run migrates.** Everything before
  the candidate's `up` is either read-only or a rename that leaves the running container
  untouched. A failure there leaves the old container running and an error in the run log.
- **Anything else on the Pi referring to `~/portfolio-app-deploy`**, such as the owner's own
  scripts or cron, would break with the rename. Nothing in this repository does besides
  `deploy.py`'s default. The owner is asked.
- **One backup** means a problem noticed two deployments late has no pre-problem copy. This
  is the owner's decision, and it is recorded here.
- **A crash mid-rotation** can leave `current.json` one deployment behind the running
  container. That is handled by existing behaviour, with one backup skipped, and described
  above.
