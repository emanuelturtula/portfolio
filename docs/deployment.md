# Deployment

One production instance, on a Raspberry Pi 5 (arm64, Debian 13) on a home network, reached
from CI over Tailscale. Merging to `main` deploys.

Nothing in this document names the host, its address or its user: those live in repository
secrets so GitHub masks them in this public repository's logs. Placeholders below are
written as `<...>`.

## The pipeline

```
push to main
  └─ CI (secret scan, lint, types, tests, coverage, OpenAPI drift, arm64 build)
       └─ compute version from Conventional Commit subjects since the last tag
            └─ build linux/arm64, push ghcr.io/<owner>/portfolio:sha-<commit>
                 └─ join the tailnet (OIDC, ephemeral node, tag:portfolio-ci)
                      └─ ssh to the host, run deploy/deploy.py
                           └─ on success: tag vX.Y.Z and latest, cut a GitHub release
```

The image is always deployed **by digest**, never by tag. The `vX.Y.Z` and `latest` tags are
applied only after production reports healthy, so the tag and the running container can
never disagree.

## What happens on the host

`deploy/deploy.py` is uploaded for each run and deleted afterwards; the host keeps no copy
of the tooling. In order:

1. Take a host-wide lock, so two deployments cannot interleave. A host still on the
   previous layout is migrated here, under the lock; see
   [Migrating from the previous layout](#migrating-from-the-previous-layout).
2. Find the live deployment's compose file, and refuse if it is missing: it is what a
   failure rolls back to.
3. Refuse a workflow run older than the deployed one. Re-running an old workflow from the
   Actions UI would otherwise roll production backwards without anyone noticing.
4. Pull the digest and check the image's `org.opencontainers.image.revision` and `.version`
   labels against what CI claims. A digest that does not correspond to the commit is
   rejected.
5. Stage the candidate in `prod/incoming/`, and back up the live SQLite database into it,
   using sqlite3's backup API from inside the running container and verifying the copy
   with `PRAGMA integrity_check`. This is a consistent snapshot even while the application
   is writing.
6. Bring the candidate up and wait for it, then assert the container is healthy **and**
   running the exact digest requested.
7. On success, make the candidate the live deployment and the previous one the backup. On
   failure, restart the previous deployment against the live database and keep the failed
   attempt's evidence in `prod/failed/`.

Nothing before step 6 changes the running container: the only writes are the migration's
rename, the staging directory and the backup. A refusal at any of those steps leaves the
live deployment running as it was.

## The layout on the host

Everything lives under `~/portfolio-app/`. The environment directory holds the live
deployment and at most one backup:

| Path, under `~/portfolio-app/` | What it holds |
|---|---|
| `deploy.lock` | the host-wide lock |
| `prod/compose.yml` | the live deployment's compose file |
| `prod/current.json` | the live deployment's manifest: the image digest, revision, version and the workflow run that delivered it |
| `prod/secrets.env` | operator-managed credentials, mode 0600, never read by the script |
| `prod/compose.sh` | runs docker compose against the live deployment; see below |
| `prod/last-attempt.json` | the latest attempt's request and outcome, whether it succeeded or not |
| `prod/backup/` | the previous deployment's `compose.yml` and `current.json` and, when there was a database to back up, `database.sqlite3`: the database as it was just before the live deployment replaced it, with `snapshot.json` naming the attempt that took it |
| `prod/failed/` | only after a failed or interrupted deployment: its `compose.yml`, `request.json`, `result.json` and, when there was one, the database snapshot taken before it (or carried forward from the previous `failed/`) with its `snapshot.json`. The next failure replaces it and the next success deletes it |
| `prod/incoming/` | only while a deployment runs: the candidate being staged |

Every path is computed from this layout when it is used, and the manifests `deploy.py`
writes store none, so renaming the root or moving a file between these directories leaves
nothing pointing at the old place. Every JSON file, the compose file and `compose.sh` are written to a temporary
file and renamed into place, so a crash leaves the old file or the new one, never a torn one.

### `compose.sh`

Compose refuses to run this project without four variables that only `deploy.py` knows:
the image digest, the port, the environment name and the path of `secrets.env`. Every
successful deployment rewrites `prod/compose.sh` with those values for what it just
deployed, so it always addresses what is running:

```bash
~/portfolio-app/prod/compose.sh up -d --force-recreate app
~/portfolio-app/prod/compose.sh ps
~/portfolio-app/prod/compose.sh logs --tail 100 app
~/portfolio-app/prod/compose.sh exec app python -m portfolio create-user --username <name>
```

Every deployment also writes it for the live deployment before it does anything else, if it
is missing or names something other than what is live. So a host whose first deployment
after the migration failed still has a working `compose.sh`, pointed at the compose file
the old layout's live deployment runs from.

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

- If `failed/` holds a snapshot of the live deployment, taken by an attempt that failed or
  was interrupted, that snapshot is carried forward. A success makes it the backup, and a
  failure keeps it in its own `failed/`. A failure never changes `current.json`, so the
  snapshot is of the same deployment.
- A deployment interrupted part-way (a dropped connection, a reboot, the process killed for
  memory), perhaps with its candidate already running, leaves `prod/incoming/` behind. The
  next deployment keeps it as `failed/`, with a `result.json` saying it was interrupted,
  when it holds a snapshot, so the database from before that candidate is carried forward
  like any other.
- Otherwise `backup/` stays exactly as it was. The one exception is a host being migrated
  from the previous layout, which has no backup yet; see below.

No older copy is deleted until the copy replacing it has been flushed to disk under its
final name, so a power cut at any moment leaves at least one.

The backup is there for a person to restore by hand. A failed deployment does not restore
it: it restarts the previous image against the live database, as it always has.

This backup is the deployment's own, and it is not the only copy of the database: the
application also takes [scheduled backups](#scheduled-backups) of its own, for the problems
this one cannot cover.

### Migrating from the previous layout

Hosts deployed before #94 keep everything under `~/portfolio-app-deploy`, with one
directory per attempt under `prod/attempts/<id>/`, each holding its own compose file and a
full copy of the database. The first deployment that runs the new `deploy.py` migrates the
host by itself:

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
   database becomes `backup/` instead. That database was taken from the deployment the
   attempt's `previous.json` names, so `previous.json` becomes `backup/current.json`, beside
   the compose file that deployment ran from. If the attempt it was made in has been
   pruned, there is no compose file, and `current.json` carries a `backup_note` saying so.
   Only then is `attempts/` deleted, so the migration never leaves the host without a copy
   it had. If no attempt holds one, the deployment says so in one line of its log.

After the rename, the migration leaves a small **regular file** at `~/portfolio-app-deploy`
saying where the root went. Leave it there. Every earlier version of `deploy.py` defaults to
that path, and a re-run of an old delivery from the Actions UI would otherwise recreate an
empty directory there and deploy into it, with no secrets and no rerun protection, leaving
two roots behind. With the file in the way, the old script fails before it runs any docker
command. The new one does not count a file as a root. If the file goes missing, any regular
file at that path does the same job: `touch ~/portfolio-app-deploy`.

If both `~/portfolio-app-deploy` and `~/portfolio-app` exist as directories, the deployment
refuses and changes nothing, so a person decides which one holds the live deployment. A
refusal after the rename says the root was migrated. Anything of your own that refers to
`~/portfolio-app-deploy`, such as a cron job or a script, needs the new path.

## Scheduled backups

Bitget keeps 90 days of fills. Past that window the SQLite database is the only record of the
owner's trade history, and of everything entered by hand: the wallets and the manual
adjustments. So the application copies its own database on a timer, once a day by default,
while it runs. Each copy is taken with SQLite's backup API, which reads one consistent
snapshot without stopping the application's writes, and is checked with
`PRAGMA integrity_check` before it is kept. Rotation keeps every copy on the 7 most recent
days that have one, and the newest copy of each of the 4 most recent ISO weeks that have one.
[Operations](operations.md), section 17, has the settings, how their state shows, and the
restore procedure. The contract is spec 029.

**A copy holds the owner's complete financial data, as the live database does**: every
imported trade, every wallet address, every manual adjustment, and the owner's account with
its password hash. It is not encrypted, as the live database is not. Treat a copy as you
treat the database, wherever it ends up.

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
noticed a week later, such as a bad import, a wrong manual delete or a corrupted file, has no
copy from before it there. The scheduled copies are for that. Neither replaces the other, and
`deploy.py` neither reads nor changes the scheduled copies.

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

Without this the runner would accept any host key, and anything answering on that address
could receive the registry token.

### 3. Host and user

```bash
gh secret set DEPLOY_HOST --repo <owner>/portfolio   # the tailnet name
gh secret set DEPLOY_USER --repo <owner>/portfolio
```

These are secrets rather than variables only so that GitHub masks them in the build logs.

### 4. Tailscale

In the Tailscale admin console, create an OAuth client with the `auth_keys` scope and
authorize it for the tag `tag:portfolio-ci`, and make sure that tag exists in the ACL and is
permitted to reach the host on port 22.

```bash
gh secret set TS_OAUTH_CLIENT_ID --repo <owner>/portfolio
gh secret set TS_AUDIENCE --repo <owner>/portfolio
```

The runner joins as an ephemeral node using federated identity, so no long-lived Tailscale
key is stored anywhere.

### 5. Application secrets

Exchange API credentials never pass through GitHub. Write them directly on the host:

```bash
install -d -m 700 ~/portfolio-app ~/portfolio-app/prod
install -m 600 /dev/null ~/portfolio-app/prod/secrets.env
$EDITOR ~/portfolio-app/prod/secrets.env
```

The first two commands are only for a host that has never been deployed to. On one that
has, they would empty the file; on one not yet migrated, they would create a second root,
which the next deployment refuses. A host not yet migrated keeps the file under its previous root until its
next deployment moves it; see
[Migrating from the previous layout](#migrating-from-the-previous-layout).

`deploy.py` refuses to run if that file is group- or world-readable. Changing it requires
recreating the container, not restarting it, because `env_file` is read at container
creation:

```bash
~/portfolio-app/prod/compose.sh up -d --force-recreate app
```

Authentication adds two variables to this same file, one of which the application refuses
to start without. See [Operations](operations.md), section 1 — a deployment that lands the
authentication change before that variable is set will roll back.

### 6. Enable deployment

Last, once everything above is in place:

```bash
gh variable set DEPLOY_ENABLED --body true --repo <owner>/portfolio
```

Until then the pipeline builds and pushes the image but skips the deployment, and says so in
the run summary. Setting it back to `false` is the kill switch.

## Rolling back

The deploy script rolls back on its own when a new container fails to become healthy. To go
back deliberately, revert the commit and merge the revert — that produces a new version and
a new deployment through the normal path, with the database backup that a forward deployment
takes.

Do not re-run an old workflow to roll back: `deploy.py` rejects it by design.

## When something fails

Read `~/portfolio-app/prod/failed/`, which the error names as its evidence. It holds the
exact request, the compose file used, the database as it was before the attempt when one
was taken or carried forward, and `result.json` with the failure reason and whether the rollback came back healthy.
`~/portfolio-app/prod/last-attempt.json` always describes the latest attempt, successful or
not.

| Symptom | Likely cause |
|---|---|
| "The OCI revision label does not match" | the image was rebuilt outside the pipeline |
| "must not be group or world readable" | `secrets.env` permissions were loosened |
| "This workflow run is older than" | an old workflow run was re-run; push instead |
| "The deployment host is busy" | a concurrent deployment holds the lock; it will retry |
| "Both ... exist, so this host cannot be migrated safely" | the old and the new root are both directories, usually because the file the migration left at the old path was replaced by one; keep the directory holding the live deployment, move the other away, and put the file back (see the migration section) |
| "The live deployment's compose file ... is missing" | `prod/compose.yml`, or the old layout's `attempts/<id>/compose.yml`, was deleted by hand; there is nothing to roll back to, so the deployment stops there. The live deployment was not touched |
| "[Errno 17] File exists: ..." naming the previous layout's root | a workflow run from before the new layout was re-run. Its `deploy.py` still targets the old root, and the file the migration left there stops it before it runs anything, by design ([Migrating from the previous layout](#migrating-from-the-previous-layout)). Nothing was deployed; push a new commit instead |
| "running and healthy, but recording it failed" | the new version is live, but a file under `prod/` could not be written; the next deployment repairs the files. Until then `compose.sh` may still name the previous image, so `up --force-recreate` through it would bring that image back: deploy again rather than recreating by hand |
| Healthy container, stale behaviour | `secrets.env` changed but the container was restarted rather than recreated |
