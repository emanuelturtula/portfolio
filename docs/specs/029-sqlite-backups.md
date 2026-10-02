# 029 — Scheduled SQLite backups and a tested restore

Issue: #22
Status: in progress

## Problem

Bitget keeps 90 days of fills. Past that window the SQLite database is the only record of
the owner's trade history, and of everything entered by hand: wallets, manual adjustments.
Today the only copy is the one `deploy.py` takes before a deployment (`docs/deployment.md`,
*One backup, and why*). It exists to undo the deployment that is live. It is replaced by
the next deployment, and nothing is copied while no deployment happens. A problem noticed a
week later, such as a bad import, a wrong manual delete or a corrupted file, has no copy
from before it.

Nothing documents how to restore, and nothing has ever restored one.

## Owner's ruling (2026-10-02)

**The copies stay on the Pi for now.** They live on the same device as the database. That
protects against a bad migration, a corrupted file, a wrong delete and a problem noticed
late. It does **not** protect against losing the storage device. Getting copies off the host
is a later decision. The documentation says so plainly and shows how to copy one off by
hand.

## Scope

- A scheduled backup, taken while the application runs, with the sqlite3 backup API.
- An integrity check on every copy before it is kept.
- Rotation: the newest copy of each of the last 7 days that have one, and the newest of
  each of the last 4 weeks that have one.
- Three CLI commands: take a backup now, list them, and restore one.
- `GET /api/health/detail` (authenticated) with the backup's state. #23 adds the other
  sources to this endpoint later.
- The Health page shows the backup's state, and the dashboard warns when backups fail or
  stop.
- A second named volume in `deploy/compose.yml` and the `Dockerfile`.
- `docs/deployment.md` and `docs/operations.md`: why this matters, where the copies are,
  that they hold the owner's financial data, how to restore, and how to copy one off the
  host.
- **A restore performed end to end**: in the test suite on every run, and once on the Pi
  after the merge, following the documentation (see *Acceptance*).

## Non-goals

- Copies off the host (owner's ruling). No upload, no sync and no network target.
- Encryption of the copies. They sit beside the live database, which is not encrypted
  either, and in a volume only the container's user and root can read.
- Changing the deployment's own backup (`prod/backup/`), which keeps its purpose.
- A restore button in the UI. A restore stops the application, which is an operator's act.
- A history of backup attempts in the database. See *Where the state lives*.

## Design: backend

### Settings (`config.py`)

| Variable | Default | Meaning |
|---|---|---|
| `PORTFOLIO_BACKUP_ENABLED` | `true` | the timer only; the CLI commands work either way |
| `PORTFOLIO_BACKUP_INTERVAL_MINUTES` | `1440` | whole minutes, validated like the other intervals |
| `PORTFOLIO_BACKUP_DIR` | `./data/backups` | where copies are written; compose sets `/app/backups` |
| `PORTFOLIO_BACKUP_KEEP_DAILY` | `7` | at least 1, so the newest copy is always kept |
| `PORTFOLIO_BACKUP_KEEP_WEEKLY` | `4` | at least 0 |

The database file is the one `PORTFOLIO_DATABASE_URL` names. The backup code derives its
path from that URL. An in-memory or non-file URL fails with a clear error when a backup is
attempted, not at startup, because tests run on such URLs.

### A copy's name

`portfolio-YYYYMMDDTHHMMSSffffffZ.sqlite3`: the UTC instant the copy was **started**, to the
microsecond, so two copies never share a name. The name is the only record of when a copy
was taken. **Only files that match this pattern exactly are ever listed, rotated or
restored.** Anything else in the directory is ignored and never deleted, except this
code's own temporary files (below).

### Taking a copy (`db/backup.py`)

A synchronous function, run in a worker thread by the caller:

1. Delete any temporary file (`.portfolio-*.partial`) an earlier crash left. Then open the
   live database with the stdlib `sqlite3`, **read-only** (`file:...?mode=ro`, `uri=True`).
2. Copy it with `Connection.backup` in **one step** (`pages=-1`) into a temporary file in
   the backup directory. In WAL mode a reader does not block writers, and the one step
   copies one consistent snapshot.
3. On the copy, set `PRAGMA journal_mode=DELETE`, so the copy is one self-contained file
   with no `-wal` file. Close it.
4. Reopen the copy and run `PRAGMA integrity_check`. Anything but a single `ok` row is
   `integrity_failed`. Check that `alembic_version` holds exactly one row.
5. `fsync` the file, rename it to its final name, `fsync` the directory.

On any failure the temporary file is removed and the error is raised as a `BackupError`
carrying one `error_kind`:

| `error_kind` | When |
|---|---|
| `database_error` | `sqlite3.Error` opening or reading the live database |
| `integrity_failed` | the copy does not pass step 4 |
| `storage_error` | `OSError` writing, syncing or renaming in the backup directory (a full disk, permissions) |

The message never holds row data. Paths inside the container may appear in it.

### Rotation (`domain/backups.py`, pure)

`backups_to_keep(instants, *, keep_daily, keep_weekly) -> frozenset[datetime]`:

- **Daily:** group the instants by UTC calendar date, take the `keep_daily` most recent
  dates **that have a copy**, and keep the newest copy of each.
- **Weekly:** group by ISO year and week, take the `keep_weekly` most recent weeks that
  have a copy, and keep the newest copy of each.
- The answer is the union. "Most recent dates that have a copy" rather than "the last N
  calendar days" means that a pause in backups does not empty the daily set when they
  resume.

Rotation runs **only after a successful copy**, so a failing backup never deletes anything.
It deletes the files not kept, ignoring one that is already gone. Nothing else is deleted.

### The service (`services/backup.py`)

`BackupService` with:

- `take() -> BackupResult` (name, size in bytes, duration): takes a copy, then rotates.
  Runs the copy in a worker thread.
- `list_backups() -> tuple[BackupFile, ...]` (name, instant, size), newest first. Not
  `list`, which ruff's `A` rules refuse as a builtin's name.
- `restore(name) -> RestoreResult`. See *Restoring*.
- `status(now) -> BackupStatus`. See *Where the state lives*.

It logs `backup_completed` (name, `bytes`, `duration_ms`, `deleted`) and `backup_failed`
(`error_kind`, the exception's type). It never logs row data.

### Where the state lives

**Successful copies are the files.** The newest file's instant is the last success, and it
survives a restart. **A failed attempt is held in memory**: the instant and `error_kind` of
the most recent attempt by this process's timer. It is lost on restart, as `last_recompute`
is (spec 021). After a restart, a backup that keeps failing shows up as `stale` once the
newest copy is old enough.

`BackupStatus.state` is the first of these that applies:

| `state` | When |
|---|---|
| `disabled` | `PORTFOLIO_BACKUP_ENABLED` is false |
| `failed` | this process's most recent attempt failed |
| `stale` | the newest copy is older than **two intervals**, or there is none, and an attempt has finished since startup |
| `pending` | there is no copy and no attempt has finished yet |
| `ok` | otherwise |

`stale` with no copy at all can only follow an attempt that finished, so a fresh
installation shows `pending` for the seconds its first backup takes, and not a warning.

### The timer (`main.py`)

A third `IntervalScheduler`, built only when `backup_enabled`, with `last_run_at` the
newest copy's instant (or `None`). It follows the scheduler's first-run rule: a fresh volume
or a copy older than one interval gets a backup at startup, and otherwise the timer sleeps
what is left of the interval. The tick calls `take()` and records the outcome for
`status`. A failure is logged and recorded, and the loop goes on.

### Restoring

`restore(name)`:

1. **Refuse while the database is open.** In WAL mode the `-wal` file exists while any
   connection is open, and SQLite deletes it when the last one closes cleanly. So a
   `-wal` file beside the live database means the application is running, or stopped
   without closing it. The message says to stop the application. In the second case it
   says to start and stop it once, which lets SQLite recover the file. No `--force`.
2. Refuse a name that does not match the pattern or is not in the directory.
3. Check the copy: `PRAGMA integrity_check`, and that its `alembic_version` is a revision
   this image's migrations know. A copy taken by a **newer** version than the one running
   is refused with both revision ids, because this version cannot migrate it.
4. **Take a copy of the live database first**, with the same code as a scheduled one, so
   the restore can itself be undone with the same command.
5. Copy the chosen backup **into** the live database with `Connection.backup`, the copy
   as the source and the live file as the destination. No file is renamed or deleted, so
   no stale `-wal` file can be replayed over the result.
6. Run `PRAGMA integrity_check` on the live database. Return the name restored, the name
   of the copy taken in step 4, and the number of rows per table in the live database
   after the restore, which must equal the backup's.

Every connection this code opens is closed before the next step. A restore leaves no
`-wal` file behind it, so the application can start, and a test pins that. Whether a
read-only connection to a closed WAL database creates and leaves `-wal` and `-shm` files is
checked by experiment, not assumed.

The row counts are `SELECT COUNT(*)` per table. That counts rows, which is not money, so
the rule against aggregating in SQL does not apply.

The application migrates a restored copy to head when it starts, as it does any database.

### CLI (`cli.py`)

| Command | Does |
|---|---|
| `python -m portfolio backup` | takes a copy now and prints its name and size |
| `python -m portfolio list-backups` | prints each copy's name, UTC instant and size, newest first |
| `python -m portfolio restore-backup NAME` | restores, and prints the safety copy's name and the per-table row counts |

Each command exits non-zero with a one-line message on a `BackupError`, as the other commands
do. Row counts go to the operator's terminal only, and never into a log record.

### `GET /api/health/detail`

Authenticated: it is **not** added to `PUBLIC_API_PATHS`. `GET /api/health` stays the cheap,
public liveness check the container's health check calls.

```json
{
  "backup": {
    "state": "ok",
    "latest_at": "2026-10-02T03:00:00.123456Z",
    "count": 9,
    "last_attempt_at": "2026-10-02T03:00:00.123456Z",
    "last_error_kind": null
  }
}
```

- `latest_at`: the newest copy's instant, or `null`.
- `count`: the number of copies.
- `last_attempt_at` and `last_error_kind`: the timer's most recent attempt in this process,
  or `null` before one. `last_error_kind` is `null` after a success.
- **No configuration value is served**: not the directory, the interval or the retention.
  #23 adds the other sources beside `backup`.

The router calls the service. It does not touch the file system itself.

## Design: frontend

- **Health page** (`pages/HealthPage.tsx`): a "Backups" section under the existing details,
  from `GET /api/health/detail`. It shows the state in words, the newest copy's date and
  time (or "none yet") and the number of copies. A failed attempt also names its kind in
  words. It has its own pending and error states, like the page's.
- **Dashboard**: one `role="alert"` paragraph above the Value section, **only** for
  `failed` and `stale`:
  - `failed`: "The last scheduled backup failed. The newest backup is from {date}." With
    no copy, the second sentence is "There is no backup yet."
  - `stale`: "The newest backup is from {date}. Scheduled backups have not completed
    since." With no copy: "There is no backup yet."
  - Both end with a link to the Health page.
  - A failed request for the detail shows nothing on the dashboard, because the Health page
    is where that is reported.
- Dates are shown with the formatter the dashboard already uses for an instant.

## Design: deployment

- `Dockerfile`: create `/app/backups` owned by `app`, and declare it a `VOLUME` beside
  `/app/data`. Docker gives a new named volume the image directory's owner, so the
  container's non-root user can write to it.
- `deploy/compose.yml`: a second named volume `backups` mounted at `/app/backups`, and
  `PORTFOLIO_BACKUP_DIR: /app/backups`. A separate volume, so removing the data volume does
  not remove the copies.
- `deploy.py` is not changed.

## Documentation

`docs/deployment.md`:

- A section on scheduled backups: why they matter (the 90-day window, things entered by
  hand), that they are not the deployment's backup and why both exist, the owner's
  ruling, and **that a copy holds the owner's complete financial data**, as the live
  database does.
- What they do **not** protect against: losing the device.

`docs/operations.md`, a new section:

- Where the copies are, the five settings, and how the state shows (Health page, dashboard,
  `GET /api/health/detail`, the two log events).
- Taking one by hand, and listing them.
- **Restoring**, step by step through `compose.sh`: `stop app`, then
  `run --rm --no-deps app python -m portfolio restore-backup NAME`, then `start app`, then
  check. It also covers what to do when the restore refuses.
- Copying one off the host: `compose.sh cp app:/app/backups/NAME .` with the application
  running, then moving it off the host. **The copy holds financial data.**
- A troubleshooting row for each `error_kind` and for each refusal.

## Acceptance criteria

1. A scheduled backup produces a copy while the application runs and writes to the
   database. The copy is consistent and passes `PRAGMA integrity_check`. It is
   self-contained (no `-wal` file needed), and its rows equal the live database's at one
   instant.
2. A copy that fails the check is not kept, and nothing is rotated after a failure.
3. Rotation keeps exactly the newest copy of each of the 7 most recent days that have one,
   and of each of the 4 most recent ISO weeks that have one. It never deletes a file that
   does not match the name pattern.
4. The timer follows the first-run rule, survives a failed tick, and is not built when
   disabled.
5. `GET /api/health/detail` answers `401` without a session, serves the five fields with
   every `state` reachable, and serves no configuration value.
6. `restore-backup` refuses while the database is open, refuses an unknown name and a copy
   from a newer schema, and takes a safety copy before it writes. Its result equals the
   chosen copy, row for row.
7. **End-to-end restore in the test suite**: with a real application on a temporary file
   database, data is created through the API, a backup is taken, the data is changed and
   deleted, the application stops, `restore-backup` runs through the CLI's `main`, a new
   application starts on the same file, and the API serves exactly the data as it was at
   the backup.
8. Failures are logged with their `error_kind` and no row data, and show as `failed` on the
   endpoint, the Health page and the dashboard.
9. The Health page and the dashboard render every state as designed. The dashboard shows
   nothing for `ok`, `pending`, `disabled` or a failed request.
10. The `Dockerfile` and `compose.yml` carry the volume and the variable. The deployment
    guardrail tests pass.
11. The documentation states why, where, that the copies hold financial data, what they do
    not protect against, and the restore procedure.
12. No float, no SQL aggregation on money, the layering contracts hold, nothing is added to
    `PUBLIC_API_PATHS`, and the OpenAPI types are regenerated with no drift.
13. The full gate passes with the coverage floors unchanged: backend 99.7% total, domain
    100% lines and branches as measured, frontend 100% on all four metrics.
14. **After the merge, on the Pi** (owner's allowance needed for the SSH session): the
    documentation's restore procedure is followed against the live deployment, restoring a
    copy taken with the application stopped, and the row counts before and after are
    equal. The result is recorded in this spec without any owner data.

## File ownership

| Agent | Files |
|---|---|
| `backend-dev-22` | `backend/src/portfolio/config.py`, `domain/backups.py`, `db/backup.py`, `services/backup.py`, `main.py`, `cli.py`, `api/routers/health.py`, a new `api/schemas/health.py`, `api/dependencies.py`, `Dockerfile`, `deploy/compose.yml`, `docs/deployment.md`, `docs/operations.md`, and the regenerated `frontend/src/api/generated/schema.ts` |
| `frontend-dev-22` | `frontend/src/pages/HealthPage.tsx`, `frontend/src/pages/DashboardPage.tsx`, a new `frontend/src/pages/dashboard/BackupNotice.tsx`, a new `frontend/src/api/health.ts`, `frontend/src/lib/backups.ts`, `frontend/src/index.css` if needed |
| `tester-22` | every test file on both sides, `frontend/src/test/**`, `tests/deploy/**`, and the gate. Sole gate owner |

The tech lead owns this spec and does the browser check at 1280 px and 375 px.
