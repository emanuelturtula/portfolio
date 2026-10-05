# 034 — A rollback restores the database a failed candidate migrated

Issue: #140
Status: implementing

## Problem

The application migrates its database forward at startup (`upgrade_to_head`,
`backend/src/portfolio/main.py:171`) and never backwards. An image that does not know the
database's Alembic revision refuses to start, with `Can't locate revision identified by ...`.

Suppose a candidate migrates the database and then fails its health check or the digest
check. `deploy/deploy.py` then starts the previous image on the migrated database. That image
cannot start, the result is `rollback=failed`, and production stays down until a person
restores by hand.

The automatic rollback is the reason there is no staging environment, and today it does not
cover the one change most likely to break a deployment: a migration.

## Scope

- **A revision check before the rollback starts the previous image.** It reads the live
  database's Alembic revision and compares it with the revision the attempt's own snapshot
  holds. It never guesses.
- **A restore when the two differ.** The rollback puts the attempt's own snapshot back as the
  live database, then starts the previous image as it does today.
- **The outcome in the result.** `result.json` records it, and so does the one-line error the
  run log shows.
- **Documentation.** `docs/deployment.md` is updated: "Neither path undoes a migration" and
  the troubleshooting rows.

## Non-goals

- **A revert of a change that added a migration still fails to deploy.** The reverted image
  does not know the database's revision. Its own rollback is healthy, because the database
  was never changed. Making a revert deployable needs downgrade migrations, or a policy of
  backward-compatible migrations. Neither belongs in `deploy.py`. The deployment document
  keeps saying "fix forward".
- **Restoring from a carried snapshot** (`carried_snapshot`), or when the attempt took none.
  See R6.
- **Migrating in a separate step before the swap.** That would change how the application
  starts, which is a larger decision than this fix needs.
- **The first deployment on a host** (`rollback=no_previous_deployment`). There is no
  previous image to start. The database a failed first candidate created stays in the data
  volume, as today.

## Rulings

- **R1. The revision is read, not inferred.**
  - The snapshot's revision is read from the copy itself, in the same `docker exec` that
    checks its integrity. It is stored on the attempt as `database_revision`.
  - The live revision is read after the candidate is stopped, from the database in the data
    volume.
  - The restore runs only when the two differ, so a candidate that failed without migrating
    loses nothing.
  - Comparing the two images' migration heads was rejected. It says whether the candidate
    *could* have migrated, not whether it did.
- **R2. The candidate is stopped before anything reads the database.**
  - The rollback first stops the candidate (`compose stop app`), so no writer remains while
    the revision is read or the database is replaced.
  - `restart: unless-stopped` does not restart a container stopped by compose.
  - The rollback then starts the previous image with the same `up` it uses today, which
    replaces the stopped container.
- **R3. The live revision is read by a one-off container of the previous image.**
  - Command: `compose run --rm --no-deps -T app python -c <script>`, with the previous
    manifest and compose file. That gives the same volumes, the same user (`app`, uid 1000)
    and the same image the rollback will start, and publishes no port.
  - The script opens the database **read-write**, reads `alembic_version` and closes. A
    read-write connection that closes last recovers a `-wal` an unclean stop left behind.
    That is what spec 029's `release_wal` does, and it is what lets step 1 of
    `restore-backup` proceed instead of refusing with "the database is open".
  - It prints the one revision, or `absent` when there is no database file.
- **R4. The restore is the previous image's own `restore-backup`.**
  - First, the snapshot is streamed through stdin into the backups volume under a copy
    name. The name follows `BACKUP_NAME_PATTERN`: `portfolio-<UTC stamp>.sqlite3`, stamped
    when the restore starts.
  - Then `python -m portfolio restore-backup <name>` runs in a one-off container of the
    previous image.
  - Why reuse it: spec 029's procedure already handles the hard parts and is tested.
    - It refuses a copy with a `-wal` that holds frames.
    - It checks integrity, and checks that the copy's revision is one the restoring image
      knows. That image is exactly the one the rollback will start.
    - It takes a safety copy of the live (migrated) database first, so what the candidate
      wrote is kept, not destroyed.
    - It writes through the backup API, so no stale `-wal` is replayed, and it verifies row
      counts.
  - Writing a second restore into `deploy.py` would be a second way to do the same thing.
  - The streaming script writes `.portfolio-<stamp>.partial` and fsyncs it, renames it to
    the final name and fsyncs the directory, the same shape as a scheduled copy.
  - **The snapshot is made a self-contained file.** `backup()` sets
    `PRAGMA journal_mode=DELETE` on its copy before the integrity check, as spec 029 step 3
    does for every copy. Without it, the copy's header says WAL.
- **R5. Nothing the restore prints leaves the host.**
  - `restore-backup` prints rows per table. Spec 029 keeps those counts out of every log,
    and `deploy.py`'s error message reaches the GitHub Actions log of a public repository.
  - So the result and the message carry only:
    - the state: `database = "unchanged" | "restored" | "not_restored" | "restore_failed"`;
    - the two revisions;
    - the restored copy's name;
    - the safety copy's name, extracted with the copy-name pattern from the one line that
      names it.
  - They never carry the command's output.
  - On a restore failure, `run()`'s existing redacted diagnostic is used. The CLI prints no
    counts on failure.
- **R6. Only the attempt's own snapshot is restored.**
  - A carried snapshot exists only when the live deployment could not be snapshotted, and
    it may be older than writes made since. Restoring it automatically could replace days
    of data without anyone deciding to.
  - With no own snapshot and a migrated database, the state is `not_restored`. The rollback
    still tries the previous image, which fails as today, but now with a message that says
    why.
- **R7. The streamed copy and the safety copy stay in the backups volume** as ordinary
  copies. They are listed by `list-backups` and rotated by the scheduled retention like any
  other. The snapshot also stays in `failed/`, as today.
- **R8. `run()` gains an optional `input_file: Path | None`**, passed to the subprocess as
  stdin. Every docker call still goes through `run()`, so its timeout, redaction and
  argument-free error messages apply to the new calls too.
- **R9. The in-container scripts take their paths as arguments.** This covers the snapshot
  script, the revision reader and the stream writer. Tests then run the real scripts with
  the host's Python against real SQLite files, including a database left with a `-wal` by an
  unclean close. A fake Docker proves the choreography; it cannot prove the scripts.
- **R10. The restore needs a previous image that has `restore-backup`**, which is v0.29.0
  and later. The live image is newer, and a rollback target is always the live image, so
  this holds for every rollback from now on. It is stated in the docstring, not checked at
  run time.

## Design

All of it is in `deploy/deploy.py`.

**On the success path.**
- `backup()` returns the snapshot's revision along with whether there was one. The script
  prints `ok <revision>` after `journal_mode=DELETE` and `integrity_check`.
- `deploy_locked` stores the revision as `candidate["database_revision"]`.
  `current.json` therefore records the revision each deployment started from.

**On the failure path, when there is a previous deployment.**
1. Stop the candidate: `compose(candidate, candidate_compose, secrets, "stop", "app")`.
2. Read the live revision (R3).
3. Decide:
   - equal → `database = "unchanged"`;
   - different, and the attempt has its own snapshot → stream and restore (R4), and
     `database = "restored"`;
   - different, with no own snapshot → `database = "not_restored"` (R6).
4. Start the previous image and verify it, exactly as today.
5. Any exception in steps 1-4 → `rollback = "failed"` with `rollback_error`, as today. When
   the restore itself raised, `database = "restore_failed"`.

**The result and the error message.**
- `result` gains `database`, `database_revision` (before), `database_revision_live`
  (after), `database_restored_from` and `database_safety_copy` (R5). A field is present only
  when it is known.
- The error message becomes
  `Deployment failed; rollback=<...>; database=<...>; evidence=<...>`. `database=` appears
  only when the rollback got as far as reading the revision.

**Rejected alternatives.**
- A restore implemented in `deploy.py`: rejected in R4.
- A bind mount of the host snapshot: the container user cannot read a 0600 file owned by
  the deploy user, and running the container as root would make every file it writes
  root-owned.
- `docker cp` into the stopped candidate: the copy lands owned by root.

## Acceptance criteria

From the issue, with this spec's reading in italics.

1. A deployment whose candidate migrates and then fails its health check ends with the
   previous version running and healthy, and `rollback=ok`. *`deploy.py` has always written
   `rollback=healthy` for this outcome, and that spelling is kept.*
2. The database the previous version runs on is the one it left, and the result records
   that it was restored.
3. Tested in `tests/deploy/` the way the existing rollback paths are, not assumed.
4. `docs/deployment.md` "Neither path undoes a migration" is updated to match.

Added by this spec:

5. A candidate that fails without migrating leaves the database untouched:
   `database=unchanged`, and no restore command runs.
6. A failed restore ends `rollback=failed`, with `database=restore_failed`, and the snapshot
   kept in `failed/`.
7. With no own snapshot and a migrated database, the state is `database=not_restored`, and
   nothing is restored.
8. The real scripts work against real SQLite files:
   - the snapshot script yields a DELETE-mode copy and its revision;
   - the revision reader recovers a `-wal` left by an unclean close and reports the
     revision;
   - the stream writer produces a file whose bytes equal its input, under a name the
     application's `BACKUP_NAME_PATTERN` accepts.
9. No row count and no output of `restore-backup` appears in the error message, in
   `result.json` or in `last-attempt.json`. The secrets sentinel still appears nowhere.

## Test plan

| # | Test (in `tests/deploy/`) |
|---|---|
| 1, 2 | `test_deploy_rotation.py` (or a new `test_deploy_migration_rollback.py`): the fake candidate migrates the live database and then fails health. Asserts, in order: the candidate is stopped; the revision read; the stream; `restore-backup`; the previous image's `up`. Also asserts `rollback=healthy` and `database=restored`, that the live database afterwards is the attempt's snapshot, and that every field is in `failed/result.json` |
| 1 | The same with a candidate that fails the digest check rather than health |
| 5 | The candidate fails without migrating: `database=unchanged`, with no stream and no `restore-backup` call |
| 6 | `restore-backup` fails: `rollback=failed`, `database=restore_failed`, `failed/database.sqlite3` present, and the previous image not started on the migrated database |
| 7 | A carried snapshot only (the previous deployment unhealthy at snapshot time) and a migrated database: `not_restored`, with no restore |
| 8 | The real scripts, executed with `sys.executable` on SQLite files under a temporary directory. The copy-name check reads `BACKUP_NAME_PATTERN` from `backend/src/portfolio/domain/backups.py` with `ast`, because `tests/deploy` is standard-library only |
| 9 | The fake `restore-backup` prints rows-per-table lines with a sentinel count. The test asserts that the sentinel appears in no message, no JSON and no captured stdout or stderr. The existing secrets-sentinel checks run on the new paths |
| 4 | `test_deploy_docs.py`: the section names the restore and the `database=` states |
| — | Every existing test in `tests/deploy/` still passes. The ones that pin the old rollback sequence change only to follow the new steps, never to weaken what they check |

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `deploy/deploy.py`, `docs/deployment.md` (the rolling-back section and the troubleshooting rows) |
| tester | `tests/deploy/**`, including `deploy_harness.py`'s `FakeDocker` (`compose stop`, `compose run`, stdin, and revisions on the fake database) |
| tech lead | this spec |
| reviewer | nothing |

`FakeDocker` is the tester's. The backend-dev writes the implementation against the contract
in this spec, and runs the existing suite and the tester's new tests as they land. Changing
`run()`'s signature (R8) means `FakeDocker.__call__` must accept `input_file`, so the tester
does that first.

## Risks

- **This runs against production, but only on a failed deployment.** The success path changes
  only the snapshot script: `journal_mode=DELETE` and printing the revision. A mistake there
  fails the backup step, which refuses the deployment before the running service is touched.
- **`compose run` against real Docker is not exercised in CI.** The scripts are proven for
  real (criterion 8), and the choreography by the fake. The command shapes follow Docker
  Compose's documented `run` options. If a Docker engine is available locally, the tester
  rehearses the stream and the revision read against a real container.
