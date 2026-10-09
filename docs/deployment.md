# Deployment

One production instance, on a Raspberry Pi 5 (arm64, Debian 13) on a home network, reached
from CI over Tailscale. Merging to `main` deploys.

Nothing in this document names the host, its address or its user: those live in repository
secrets so GitHub masks them in this public repository's logs. Placeholders below are
written as `<...>`.

## The pipeline

`.github/workflows/delivery.yml` runs on every push to `main`:

```
push to main
  └─ CI: the same jobs that gate a pull request (ci.yml)
       └─ compute the version from the commit messages since the last vX.Y.Z tag
            └─ build linux/arm64, push ghcr.io/<owner>/portfolio:sha-<commit>
                 └─ join the tailnet (OIDC, ephemeral node, tag:portfolio-ci)
                      └─ copy the deploy kit to the host over ssh, run deploy/deploy.py
                           └─ on success: tag the digest vX.Y.Z and latest, cut a GitHub release
```

The last three steps run only while the `DEPLOY_ENABLED` variable is `true`
([step 6](#6-enable-deployment)).

The image is always deployed **by digest**, never by tag. The `vX.Y.Z` and `latest` tags are
applied only after production reports healthy, so the tag and the running container can
never disagree.

- **Only this workflow can deploy, and only from `main`.** Before it connects,
  `scripts/remote_deploy.py` checks the run's own GitHub variables: a push, to this
  repository, running `delivery.yml`, on `refs/heads/main`. A workflow added by a pull
  request cannot reach the host. It also checks the shape of every input, and names one that
  fails (`Invalid host`) without printing its value, which may be a secret.
- **One delivery at a time.** Runs share a concurrency group and are never cancelled in
  flight. A merge that lands during a delivery waits for it. GitHub keeps only the newest
  waiting run, so after several quick merges only the last is delivered, carrying the
  others' commits.

### Versions

`scripts/next_version.py` reads every commit message since the newest `vX.Y.Z` tag the
commit can reach:

| The messages include | The next version is |
|---|---|
| a breaking change: `type!:` in a subject, or a `BREAKING CHANGE:` footer | major, demoted to minor while the version is `0.x` |
| a `feat` subject | minor |
| neither | patch |

- With no tag yet, the version is `v0.1.0`.
- A commit that already carries a version tag keeps it, so re-running a delivery never mints
  a second version for one commit.
- Merges into `main` are squash-only, so each merge is one commit, and its subject is the
  Conventional Commit that counts.
- The git tag is created by the release, after the deployment. A run that does not deploy
  creates none, and the next run counts its commits too.

### Jobs and their time bounds

Every job sets `timeout-minutes`, so a hang fails in minutes rather than at GitHub's default
of six hours. `tests/deploy/test_workflow_timeouts.py` fails any job without one, or with one
above 30. Each bound is at least five times the slowest measured run of its job, rounded up
to five minutes, except the two the table explains. The measurements sit beside each number
in the workflow files.

| Job | Workflow | Bound |
|---|---|---|
| Secrets scan | `ci.yml` | 5 min |
| Lint & types | `ci.yml` | 5 min |
| Backend tests | `ci.yml` | 30 min: the guard's ceiling, about twice the slowest run (14.1 min) rather than five times. #131 raised it from 15 after a Delivery run went past 15 and was cancelled before it deployed. The job also runs the deployment guardrail tests in `tests/deploy/` |
| Frontend tests | `ci.yml` | 10 min |
| OpenAPI drift | `ci.yml` | 15 min |
| Docker build (arm64) | `ci.yml` | 10 min |
| Compute version | `delivery.yml` | 5 min |
| Build arm64 image | `delivery.yml` | 10 min |
| Deploy prod | `remote-deploy.yml` | 20 min: not sized from a measurement. It must outlast the 17-minute SSH call it wraps, so that the script's own timeout fires first and its cleanup still runs |
| Publish release | `delivery.yml` | 5 min |
| Deploy disabled | `delivery.yml` | 5 min |

The CI job names are the required status checks of the `main` ruleset: renaming one silently
removes a merge gate. Delivery's `validate` and `Deploy production (8083)` call the reusable
workflows above, and GitHub accepts no bound on such a call; the jobs inside carry their own.

Inside the deploy job, the remote calls are bounded too. The SSH connection gets 20 seconds,
the upload 60, and each SSH call 17 minutes. On the host, the lock wait is four minutes, each
docker command 15, and the candidate's wait to become healthy three.

## What happens on the host

`scripts/remote_deploy.py` copies `deploy/deploy.py` and `deploy/compose.yml` into a new
directory with a random name under `~/.cache/portfolio-delivery/`. It logs in to GHCR there,
with the job's own token passed on stdin, and runs `deploy.py`. Afterwards it removes the
directory, registry login included, whether the deployment succeeded or not. The host keeps
no copy of the tooling.

`deploy.py` validates every argument before any command runs. Then, in order:

1. **Take a host-wide lock**, so two deployments cannot interleave. It waits up to four
   minutes for a deployment that holds it. A host still on the previous layout is migrated
   here, under the lock; see
   [Migrating from the previous layout](#migrating-from-the-previous-layout).
2. **Find the live deployment's compose file**, and refuse if it is missing: it is what a
   failure rolls back to. If `compose.sh` is missing or names something other than the live
   deployment, it is rewritten now.
3. **Refuse a workflow run older than the deployed one**: a lower run number, or the same run
   number with a different image or commit. Re-running an old workflow from the Actions UI
   would otherwise roll production backwards without anyone noticing. The deployed run may
   run again only with the same image and commit.
4. **Pull the digest and check its labels.** The image's
   `org.opencontainers.image.revision` and `.version` labels must match what CI claims. A
   digest that does not correspond to the commit is rejected.
5. **Check `secrets.env`.** An empty one is created at mode 0600 if there is none. One that
   is group- or world-readable stops the deployment.
6. **Stage the candidate in `prod/incoming/`, and back up the live SQLite database into it.**
   - First, whatever an interrupted deployment left is settled: a half-done backup swap is
     finished or undone, and a `prod/incoming/` holding a snapshot is kept as `prod/failed/`.
   - The snapshot uses sqlite3's backup API from inside the running container and is
     verified with `PRAGMA integrity_check`. It is consistent even while the application is
     writing.
   - It is taken only when the live container is healthy and runs the digest `current.json`
     names. Otherwise there is none; see [One backup, and why](#one-backup-and-why).
   - If taking it fails, the deployment stops here, with its evidence in `prod/failed/`.
7. **Bring the candidate up and wait up to three minutes for it**, then assert the container
   is healthy **and** running the exact digest requested. Healthy is the compose file's
   health check: `/api/health` answers `ok`, and the single-page application is served.
8. **On success**, make the candidate the live deployment and the previous one the backup.
   **On failure**, stop the candidate, and if it migrated the database, restore the snapshot
   taken in step 6. Then start the previous deployment again, unless the restore failed, and
   keep the failed attempt's evidence in `prod/failed/`. [Rolling back](#rolling-back) has
   the details. A first deployment, with nothing to go back to, takes the candidate down and
   keeps the data volume.

Nothing before step 7 stops or replaces the running container. The writes before it are:

- the migration's rename, and the file it leaves at the old path;
- `compose.sh`, and an empty `secrets.env` on a new host;
- settling what an interrupted deployment left;
- the staging directory and the backup. The backup passes through a temporary file in the
  live data volume, which is removed once it has been copied out.

A refusal at any of those steps leaves the live deployment running as it was.

## The layout on the host

The deployment's state lives under `~/portfolio-app/`. The database and the scheduled backups
live in two named Docker volumes, `data` and `backups`, of the compose project
`portfolio-app-prod`. The environment directory holds the live deployment and at most one
backup:

| Path, under `~/portfolio-app/` | What it holds |
|---|---|
| `deploy.lock` | the host-wide lock |
| `prod/compose.yml` | the live deployment's compose file |
| `prod/current.json` | the live deployment's manifest: the image digest, revision, version, the attempt that made it and the workflow run that delivered it |
| `prod/secrets.env` | operator-managed credentials, mode 0600, never read by the script |
| `prod/compose.sh` | runs docker compose against the live deployment; see below |
| `prod/last-attempt.json` | the latest attempt's request and outcome, whether it succeeded or not |
| `prod/backup/` | the previous deployment's `compose.yml` and `current.json` and, when there was a database to back up, `database.sqlite3`: the database as it was just before the live deployment replaced it, with `snapshot.json` naming the attempt that took it |
| `prod/failed/` | only after a failed or interrupted deployment: its `compose.yml`, `request.json`, `result.json` and, when there was one, the database snapshot taken before it (or carried forward from the previous `failed/`) with its `snapshot.json`. The next failure replaces it and the next success deletes it |
| `prod/incoming/` | only while a deployment runs: the candidate being staged |
| `prod/backup.new/`, `prod/backup.old/` | only while the backup is being swapped. A crash can leave one behind, and the next deployment finishes the swap or undoes it |

Every path is computed from this layout when it is used, and the manifests `deploy.py`
writes store none. So renaming the root, or moving a file between these directories, leaves
nothing pointing at the old place.

Every JSON file, the compose file and `compose.sh` are written to a temporary file and
renamed into place, so a crash leaves the old file or the new one, never a torn one.

### `compose.sh`

Compose refuses to run this project without three variables that only `deploy.py` knows: the
image digest, the port and the environment name. A fourth, the path of `secrets.env`, falls
back to `/dev/null`, so a container created without it gets none of its secrets. Every
successful deployment rewrites `prod/compose.sh` with all four for what it just deployed, so
it always addresses what is running:

```bash
~/portfolio-app/prod/compose.sh up -d --force-recreate app
~/portfolio-app/prod/compose.sh ps
~/portfolio-app/prod/compose.sh logs --tail 100 app
~/portfolio-app/prod/compose.sh exec app python -m portfolio create-user --username <name>
```

Every deployment also writes it for the live deployment as soon as it has found it, if it is
missing or names something other than what is live. So a host whose first deployment after
the migration failed still has a working `compose.sh`, pointed at the compose file the old
layout's live deployment runs from.

Every argument is passed to compose unchanged. The script holds no secret: it names
`secrets.env`, never its contents, and embeds only the three values `deploy.py` validated
before running anything, plus the live compose file's name. Its paths are relative to the
script itself, which it finds through any symlink, so it works from any directory.
[Operations](operations.md) uses it for every command against the running container.

### One backup, and why

After a successful deployment the host keeps at most one copy of the database, in
`prod/backup/`: exactly one once any deployment has had a database to back up, and none
before that. That is the owner's decision: every copy is one more place the owner's data
sits on disk, and one is enough to undo the deployment that is live. The previous layout
kept ten.

What that costs, accepted: a problem noticed two deployments late has no copy from before
it.

During a failed deployment's aftermath there can be two: `backup/`, and the snapshot in
`failed/`. The second is the database as it was just before the failed attempt, which is
the copy that matters if a migration went wrong.

A deployment that cannot take a snapshot of its own, because the live container is not
healthy, is not the one `current.json` names, or has no database yet, never throws a copy
away:

- If `failed/` holds a snapshot taken by an attempt that failed or was interrupted, that
  snapshot is carried forward, unless `backup/` already holds one at least as new. A success
  makes it the backup, and a failure keeps it in its own `failed/`. Which copy is newer is
  read from the attempt that took each, which its `snapshot.json` names: attempt ids begin
  with their start time, and never go backwards on a host.
- A deployment interrupted part-way (a dropped connection, a reboot, the process killed for
  memory), perhaps with its candidate already running, leaves `prod/incoming/` behind. The
  next deployment keeps it as `failed/`, with a `result.json` saying it was interrupted,
  when it holds a snapshot, so the database from before that candidate is carried forward
  like any other.
- Otherwise `backup/` stays exactly as it was. The one exception is a host being migrated
  from the previous layout, which has no backup yet; see below.

No older copy is deleted until the copy replacing it has been flushed to disk under its
final name, so a power cut at any moment leaves at least one.

The backup is there for a person to restore by hand. A failed deployment never restores it.
What a failed deployment can restore is its own snapshot, the one it took just before it,
and only when its candidate migrated the database; see [Rolling back](#rolling-back).

This backup is the deployment's own, and it is not the only copy of the database: the
application also takes [scheduled backups](#scheduled-backups) of its own, for the problems
this one cannot cover.

### Migrating from the previous layout

Hosts deployed before #94 keep everything under `~/portfolio-app-deploy`, with one
directory per attempt under `prod/attempts/<id>/`, each holding its own compose file and a
full copy of the database. The production host was migrated when #94 deployed. The code
still migrates any host it finds on that layout, the first time the new `deploy.py` runs
there:

1. It takes the lock in `~/portfolio-app-deploy`, then renames that directory to
   `~/portfolio-app` with a single rename. The lock is held on the open file, so it moves
   with the directory, and a deployment already waiting on the old path carries on in the
   new one. `secrets.env` moves with it, byte for byte and still mode 0600. The running
   container is not touched: it mounts nothing from this directory, and its data lives in
   a named Docker volume.
2. The old manifest names its compose file under `attempts/<id>/`. That path is rebased
   onto the new root, and it is what a failure rolls back to.
3. On success, `backup/` receives the old deployment's compose file and manifest and the
   new snapshot, and `attempts/` is deleted, ten database copies with it. A failure leaves
   `attempts/` in place until a deployment succeeds.
4. If that successful deployment could not take a snapshot, because the old container was
   not healthy or not the one its manifest names, the newest attempt that still holds a
   database becomes `backup/` instead.
   - That database was taken from the deployment the attempt's `previous.json` names, so
     `previous.json` becomes `backup/current.json`, beside the compose file that deployment
     ran from.
   - If the attempt it was made in has been pruned, there is no compose file, and
     `current.json` carries a `backup_note` saying so.
   - Only then is `attempts/` deleted, so the migration never leaves the host without a copy
     it had. If no attempt holds one, the deployment says so in one line of its log.

After the rename, the migration leaves a small **regular file** at `~/portfolio-app-deploy`
saying where the root went. Leave it there:

- Every earlier version of `deploy.py` defaults to that path. A re-run of an old delivery
  from the Actions UI would otherwise recreate an empty directory there and deploy into it,
  with no secrets and no rerun protection, leaving two roots behind.
- With the file in the way, the old script fails before it runs any docker command. The new
  one does not count a file as a root.
- If the file goes missing, any regular file at that path does the same job:
  `touch ~/portfolio-app-deploy`.

If both `~/portfolio-app-deploy` and `~/portfolio-app` exist as directories, the deployment
refuses and changes nothing, so a person decides which one holds the live deployment. A
refusal after the rename says the root was migrated. Anything of your own that refers to
`~/portfolio-app-deploy`, such as a cron job or a script, needs the new path.

## Scheduled backups

The SQLite database is the only record of everything entered by hand -- the wallets and the
extended public keys -- and of the balance history read from them, which no chain index
gives back as it was. So the application copies its own database on a timer, once a day by
default, while it runs. Each copy is taken with SQLite's backup API, which reads one consistent
snapshot without stopping the application's writes, and is checked with
`PRAGMA integrity_check` before it is kept. Rotation keeps every copy on the 7 most recent
days that have one, and the newest copy of each of the 4 most recent ISO weeks that have one.
[Operations](operations.md), section 17, has the settings, how their state shows, and the
restore procedure. The contract is spec 029.

**A copy holds the owner's complete financial data, as the live database does**: every
wallet address and extended public key, the balance history, and the owner's account with
its password hash. It is not encrypted, as the live database is not. Treat a copy as you
treat the database, wherever it ends up.

A copy taken before migration `0012_drop_exchanges_accounting` (spec 036) holds more: the
exchange fills imported from Bitget and BingX, and the manual adjustments. That migration
deleted them from the live database, and Bitget keeps only 90 days of fills, so for anything
older those copies, and the deployment backup taken just before that release, are the only
record left. [Operations](operations.md), section 12, says what to keep.

### Where they are

In a named Docker volume of their own, `backups`, mounted in the container at
`/app/backups`, which the compose file sets as `PORTFOLIO_BACKUP_DIR`. Like the data volume,
it lives where Docker keeps volumes rather than under `~/portfolio-app/`, and only the
container's user and root can read it. It is a separate volume so that removing the data
volume does not remove the copies, and so that a copy can be restored onto a new, empty data
volume.

### Not the deployment's backup, and why both exist

`prod/backup/` ([One backup, and why](#one-backup-and-why)) is the database as it was just
before the live deployment replaced it. It exists to undo the deployment that is live, the
next deployment replaces it, and nothing is copied while no deployment happens. A problem
noticed a week later, such as a wrong manual delete or a corrupted file, has no copy from
before it there. The scheduled copies are for that. Neither replaces the other.

`deploy.py` never reads or deletes a scheduled copy. A rollback that restores adds two copies
to the backups volume: the snapshot it restored and the safety copy `restore-backup` took
first ([Rolling back](#rolling-back)). Rotation treats them like any other copy. Every copy is
kept while its day is one of the 7 most recent days that have a copy, and after that only
the newest copy of each of the 4 most recent ISO weeks is, with the default settings. So they
age out like the rest, and on a day the timer has not copied yet, they count as that day's
copies.

**Do not merge to `main` while a restore is in progress.** A merge deploys, and the deployment
starts a new container on the database the restore is writing. Operations, section 17, has
the restore procedure; finish it, including the check, before merging anything.

### What they do not protect against

**The copies are on the same device as the database, so they do not protect against losing
the storage device.** A failed SD card or SSD, or a Pi that is stolen or destroyed, takes the
copies with the database. That is the owner's ruling for now (spec 029): the copies protect
against a bad migration, a corrupted file, a wrong delete and a problem noticed late, and
getting copies off the host is a later decision. Until then, copy one off the host by hand
from time to time. Operations, section 17, shows how.

## One-time setup

What the workflows read, all set at repository level. No GitHub environment is involved:
`DEPLOY_ENABLED` and the checks in `remote_deploy.py` are the gate.

| Name | Kind | Holds |
|---|---|---|
| `DEPLOY_SSH_KEY` | secret | the private half of the deploy key ([step 1](#1-a-dedicated-ssh-key)) |
| `DEPLOY_KNOWN_HOSTS` | secret | the host's pinned public key ([step 2](#2-pin-the-host-key)) |
| `DEPLOY_HOST` | secret | the host's name on the tailnet ([step 3](#3-host-and-user)) |
| `DEPLOY_USER` | secret | the user the deployment logs in as ([step 3](#3-host-and-user)) |
| `TS_OAUTH_CLIENT_ID` | secret | the Tailscale federated identity's client ID ([step 4](#4-tailscale)) |
| `TS_AUDIENCE` | secret | that identity's audience ([step 4](#4-tailscale)) |
| `DEPLOY_ENABLED` | variable | `true` to deploy ([step 6](#6-enable-deployment)) |

The registry login needs nothing set: each job uses its own short-lived `GITHUB_TOKEN`.

The host needs:

- Docker with the Compose plugin;
- Python 3.11 or later, for `deploy.py`, which uses the standard library only;
- a user that can run `docker` without `sudo`, which in practice means the `docker` group.
  That group is root-equivalent on the host, one more reason the key below is used by
  nothing else;
- Tailscale, and host port 8083 free.

### 1. A dedicated SSH key

Generate a keypair used by nothing else, so it can be revoked without affecting anything
else:

```bash
ssh-keygen -t ed25519 -C "portfolio-actions" -f ~/.ssh/portfolio-actions -N ""
```

Append the public key to the host's `~/.ssh/authorized_keys`, then set the private key as a
repository secret:

```bash
gh secret set DEPLOY_SSH_KEY --repo <owner>/portfolio < ~/.ssh/portfolio-actions
```

### 2. Pin the host key

```bash
ssh-keyscan -t ed25519 <host> | gh secret set DEPLOY_KNOWN_HOSTS --repo <owner>/portfolio
```

Scan the same name you will set as `DEPLOY_HOST`: ssh looks the pin up by that name.

The deployment connects with strict host key checking, against this pin alone. Without it the
runner cannot connect at all. With it, nothing else answering on that name can receive the
registry token. If the host's key changes, as after a reinstall, pin it again.

### 3. Host and user

```bash
gh secret set DEPLOY_HOST --repo <owner>/portfolio   # the host's name on the tailnet
gh secret set DEPLOY_USER --repo <owner>/portfolio
```

These are secrets rather than variables only so that GitHub masks them in the build logs.
The tailnet join also pings `DEPLOY_HOST` before the deployment connects, because a new node
is refused for a few seconds after it joins.

### 4. Tailscale

The runner joins the tailnet with a federated identity, not an OAuth client with a secret.
In the Tailscale admin console, create a trust credential of that kind for GitHub Actions:

- **subject**: this repository's pushes to `main`. GitHub may issue the subject with numeric
  ids, as `repo:<owner>@<owner_id>/portfolio@<repo_id>:ref:refs/heads/main`, so read its
  form rather than composing it from names:
  `gh api repos/<owner>/portfolio/actions/oidc/customization/sub`;
- **scope**: `auth_keys`, writable, for the tag `tag:portfolio-ci`.

Make sure that tag exists in the tailnet policy and is permitted to reach the host on port 22.
Then store the credential's client ID and audience:

```bash
gh secret set TS_OAUTH_CLIENT_ID --repo <owner>/portfolio
gh secret set TS_AUDIENCE --repo <owner>/portfolio
```

The runner joins as an ephemeral node using GitHub's OIDC token, so no long-lived Tailscale
key is stored anywhere.

### 5. Application secrets

Application credentials -- the bootstrap password, and the CoinGecko key if you use one --
never pass through GitHub. Write them directly on the host:

```bash
install -d -m 700 ~/portfolio-app ~/portfolio-app/prod
install -m 600 /dev/null ~/portfolio-app/prod/secrets.env
$EDITOR ~/portfolio-app/prod/secrets.env
```

The first two commands are only for a host that has never been deployed to. On one that
has, they would empty the file. On one not yet migrated, they would create a second root,
which the next deployment refuses. A host not yet migrated keeps the file under its previous
root until its next deployment moves it; see
[Migrating from the previous layout](#migrating-from-the-previous-layout).

`deploy.py` creates the file empty if it is missing, and refuses to run if it is group- or
world-readable. Changing it requires recreating the container, not restarting it, because
`env_file` is read at container creation:

```bash
~/portfolio-app/prod/compose.sh up -d --force-recreate app
```

One of the variables in this file is authentication's: `PORTFOLIO_BOOTSTRAP_PASSWORD`, which
is optional. See [Operations](operations.md), section 1.

### 6. Enable deployment

Last, once everything above is in place:

```bash
gh variable set DEPLOY_ENABLED --body true --repo <owner>/portfolio
```

Until then the pipeline builds the image and pushes it under its `sha-` tag only. It skips
the deployment, and with it the version tags and the release, and says so in the run summary.
Any value other than `true` is the kill switch.

## Rolling back

The deploy script rolls back on its own when a new container fails to become healthy, or is
not running the digest requested. To go back deliberately, revert the commit and merge the
revert — that produces a new version and a new deployment through the normal path, with the
database backup that a forward deployment takes.

Do not re-run an old workflow to roll back: `deploy.py` rejects it by design.

**The automatic rollback undoes a migration; a revert does not.** The application migrates
its database forward when it starts, and never backwards. An image that does not know the
revision the database is at refuses to start, with `Can't locate revision identified by ...`
in its log. A candidate that migrated the database and then failed would leave the previous
image nothing it can start on.

If the previous deployment is still running, healthy and on its own digest, the candidate's
container never replaced it, so the candidate never ran and cannot have migrated anything.
The rollback then goes straight to step 4, with `database=unchanged`. Otherwise, before it
starts the previous image again, it:

1. **Stops the candidate**, so that nothing writes to the database while it is read or
   replaced.
2. **Reads the database's schema revision**, in a one-off container of the previous image,
   and compares it with the revision of the snapshot this attempt took before it replaced
   anything. That read also recovers what an unclean stop left in a `-wal`.
3. **Restores the snapshot when the two differ**, with the previous image's own
   `restore-backup`, the command Operations, section 17, describes. The snapshot is first
   streamed into the backups volume under a copy's name. `restore-backup` checks it, takes
   a safety copy of the migrated database before it writes, and only then puts the snapshot
   back. Nothing written since the snapshot is deleted, whether by the candidate or by the
   previous version before the candidate started: it is in that safety copy, or, for a live
   database too damaged to copy, in the file `restore-backup` moved it aside to.
4. **Starts the previous image** and checks it is healthy, as it always has.

Only the attempt's own snapshot is ever restored, and only when its revision could be read.
One carried forward from an earlier failure can be older than writes made since, and
replacing those is a person's decision.

Only a failed restore stops the rollback before step 4. If stopping the candidate or reading
the revision fails, nothing has been restored or changed, so the rollback goes on to start
the previous image as it always has.

The error line says what happened as `database=`, after `rollback=`:

| `database=` | What happened |
|---|---|
| `unchanged` | The revision had not moved, or the previous deployment was still running because the candidate never replaced it. Nothing was restored. |
| `restored` | The candidate had migrated the database. The snapshot was restored, and the previous version runs on the database it left. With `database_error` set, `restore-backup` reported a completed restore and then failed; the restore stands, and the previous image was started all the same. |
| `not_restored` | A database is there, but this attempt has no snapshot of its own with a revision to compare with: the live container was not healthy, was not the one `current.json` names, or had no database yet, or the snapshot held no single revision. Nothing was restored, and the previous image was started anyway. If it came up, `rollback=healthy`, the candidate had not migrated. If it did not, `rollback=failed`, it most likely had: restore by hand. |
| `restore_failed` | The candidate had migrated the database, and putting the snapshot back failed. The previous image was **not** started on a database it cannot read, so `rollback=failed` and production is down. If the restore got as far as its safety copy, `database_safety_copy` names it. |
| `unread` | Stopping the candidate or reading the revision failed, so nothing was compared or restored. The previous image was started anyway, as before this step existed. `database_error` says what failed. |
| absent | There was no previous deployment, so there was no rollback to check the database for. |

The line carries the state and nothing else, because it reaches the public Actions log.
`failed/result.json`, on the host, has the details:

- `database_revision`: the snapshot's revision, the one the previous version ran on. It is
  null when there was no snapshot, or the snapshot held no single revision. A successful
  deployment records it in `current.json` too, as the revision it started from.
- `database_revision_live`: the revision the candidate left, null when there was no
  database file. Absent when it was not read: the previous deployment was still running, or
  the read failed.
- `database_restored_from`: the snapshot's name in the backups volume, set once it is there.
- `database_safety_copy`: the safety copy `restore-backup` took of the live database, which
  holds everything written since the snapshot. Set with `restored`, and with
  `restore_failed` when the restore got as far as taking it.
- `database_error`: with `unread`, why stopping the candidate or reading the revision
  failed. With `restored`, a fixed note that `restore-backup` reported a completed restore
  and then failed; its output is not recorded.

Both copies are in the backups volume, `/app/backups` in the container, beside the scheduled
copies. `list-backups` shows them, and the scheduled rotation removes them like any other
copy ([Not the deployment's backup](#not-the-deployments-backup-and-why-both-exist)), so keep
one elsewhere if you need it for longer. The snapshot also stays in
`prod/failed/database.sqlite3`. Of what `restore-backup` prints, only the safety copy's name
is recorded, and, when it fails, its error in `rollback_error`, on the host. Its rows per
table stay out of every log, as for any restore.

**A revert of a change that added a migration still fails to deploy.** The reverted image
does not know the revision the database is at, so it does not start. Its rollback is healthy,
with `database=unchanged`, because that attempt never changed the database: the version you
meant to leave keeps running. Fix forward instead. Going back past a migration means
restoring a copy from before it, by hand, which loses everything written since.

Restoring by hand is a step for a person. Operations, section 17, has the restore procedure,
and its *Bringing a copy back onto the host* shows how a file on the host, such as
`prod/failed/database.sqlite3`, gets into the backups volume.

## When something fails

Read `~/portfolio-app/prod/failed/`, which the error names as its evidence. It holds:

- the exact request and the compose file used;
- the database as it was before the attempt, when one was taken or carried forward;
- `result.json`, with the failure reason in `error` and the rollback's outcome in
  `rollback`: `healthy`, `failed` (with `rollback_error`), or `no_previous_deployment`.
  What the rollback did to the database is in `database` and the fields beside it; see
  [Rolling back](#rolling-back). A failure while taking the backup has `stage` set to
  `backup` instead, and no rollback, because nothing was replaced. An interrupted attempt's
  has `status` set to `interrupted`.

`~/portfolio-app/prod/last-attempt.json` always describes the latest attempt, successful or
not.

| Symptom | Likely cause |
|---|---|
| "Deployment skipped" in the run summary | `DEPLOY_ENABLED` is not `true`. The image was pushed under its `sha-` tag only, with no version tag and no release |
| The tailnet join fails with "Unauthorized" | the federated identity's subject does not match the one GitHub issues for this repository, or `TS_AUDIENCE` is not its audience ([step 4](#4-tailscale)) |
| "Invalid host" or "Invalid user" | `DEPLOY_HOST` or `DEPLOY_USER` is unset or not a bare name. Only the input's name is printed, never its value |
| "Deployments require a push to this repository's delivery workflow" | `remote-deploy.yml` was called by something other than `delivery.yml` on a push. By design |
| "Host key verification failed" | `DEPLOY_KNOWN_HOSTS` is unset, was scanned under a different name than `DEPLOY_HOST`, or the host's key changed. Pin it again ([step 2](#2-pin-the-host-key)) |
| "Permission denied (publickey)" | the public half of `DEPLOY_SSH_KEY` is not in `DEPLOY_USER`'s `~/.ssh/authorized_keys` |
| The tailnet ping, or ssh, times out | the host is off the tailnet, or the tailnet policy does not let `tag:portfolio-ci` reach it on port 22 |
| "The deployment host is busy" | another deployment held the host-wide lock for four minutes, and this one gave up without changing anything. Re-run the job once that one finishes |
| "Both ... exist, so this host cannot be migrated safely" | the old and the new root are both directories, usually because the file the migration left at the old path was replaced by one; keep the directory holding the live deployment, move the other away, and put the file back (see the migration section) |
| "The live deployment's compose file ... is missing" | `prod/compose.yml`, or the old layout's `attempts/<id>/compose.yml`, was deleted by hand; there is nothing to roll back to, so the deployment stops there. The live deployment was not touched |
| "This workflow run is older than, or conflicts with, the deployed one" | an old workflow run was re-run, or the deployed run was re-run and rebuilt its image under a new digest; push instead |
| "The OCI revision label does not match" or "The OCI version label does not match" | the image was rebuilt outside the pipeline |
| "... did not finish within 900 seconds" | a docker command hung for 15 minutes, most often the image pull. A pull that hangs has changed nothing |
| "must not be group or world readable" | `secrets.env` permissions were loosened |
| "The backup failed before the service was replaced" | the live database could not be copied, or its copy failed the integrity check. The live deployment was not touched; `failed/result.json` has the reason |
| "Deployment failed; rollback=healthy; database=unchanged" | the candidate did not become healthy within three minutes, or was not running the digest requested, and the previous deployment is running on the database as it was: again, or still, if the candidate's container never replaced it. The rollback replaced any candidate container, so its logs went with it; `failed/result.json` has the error compose reported |
| "Deployment failed; rollback=healthy; database=restored" | the same, but the candidate had migrated the database before it failed. The snapshot from before it was restored, and the previous version runs on it. Anything written since the snapshot is only in the safety copy `database_safety_copy` names, in the backups volume ([Rolling back](#rolling-back)). A `database_error` here says `restore-backup` failed after it had finished the restore, which stands |
| "Deployment failed; rollback=healthy; database=not_restored" | the candidate failed, the attempt had no snapshot of its own with a revision to compare with, and the previous version started on the database as the candidate left it, so the candidate had not migrated it. Nothing to do about the database |
| "Deployment failed; rollback=healthy; database=unread" | the candidate failed, and stopping it or reading the database's revision failed too, so nothing was compared or restored. The previous version started anyway, so the database is at a revision it knows. `database_error` in `failed/result.json` says what failed: a compose file compose rejects fails here, for one |
| "Deployment failed; rollback=no_previous_deployment" | the first deployment on this host failed, for example on a bootstrap password the policy refuses. The candidate was taken down and the data volume kept |
| "Deployment failed; rollback=failed; database=restore_failed" | production is down. The candidate migrated the database, and putting the snapshot back failed, so the previous image was not started on a database it cannot read. `failed/result.json` has `rollback_error`, with what `restore-backup` refused or failed on. The snapshot is `failed/database.sqlite3`, and in the backups volume as well when `database_restored_from` names it. If the restore got as far as its safety copy, `database_safety_copy` names the copy holding the migrated database. Fix the cause and restore by hand, Operations, section 17, then start the application ([Rolling back](#rolling-back)) |
| "Deployment failed; rollback=failed; database=not_restored" | production is down. The candidate most likely migrated the database, and the attempt had no snapshot of its own with a revision to put back, so the previous image cannot start on it. The newest copy from before it may be the one in `failed/database.sqlite3`, or a scheduled copy (`list-backups`): choose, and restore it by hand ([Rolling back](#rolling-back)) |
| "Deployment failed; rollback=failed; database=unread" | production is down. Stopping the candidate or reading the database's revision failed (`database_error`), so nothing was restored, and the previous image did not come up either (`rollback_error`). If its log says `Can't locate revision identified by ...`, the candidate migrated the database: restore a copy from before it by hand, such as `failed/database.sqlite3` when there is one ([Rolling back](#rolling-back)) |
| "Deployment failed; rollback=failed" with `database=unchanged` or `restored`, or none | production is down. `failed/result.json` has `rollback_error`. With `database=` set, the previous image was given the database it left and still did not come up, so the cause is elsewhere. With none, this was the first deployment on the host, and taking the candidate down failed |
| "[Errno 17] File exists: ..." naming the previous layout's root | a workflow run from before the new layout was re-run. Its `deploy.py` still targets the old root, and the file the migration left there stops it before it runs anything, by design ([Migrating from the previous layout](#migrating-from-the-previous-layout)). Nothing was deployed; push a new commit instead |
| "running and healthy, but recording it failed" | the new version is live, but a file under `prod/` could not be written; the next deployment repairs the files. Until then `compose.sh` may still name the previous image, so `up --force-recreate` through it would bring that image back: deploy again rather than recreating by hand |
| The deploy job hit its 20-minute bound, or warned it "could not remove the temporary delivery directory" | the cleanup did not run, so a directory under `~/.cache/portfolio-delivery/` on the host still holds the registry login; remove it. The token in it expired with the job. A deployment cut off part-way is picked up by the next one ([One backup, and why](#one-backup-and-why)) |
| "Publish release" failed | production runs the new digest without its version tags. Re-run that job: it retags the same digest and skips a release that already exists |
| Healthy container, stale behaviour | `secrets.env` changed but the container was restarted rather than recreated |
