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
deployment and exactly one backup:

| Path, under `~/portfolio-app/` | What it holds |
|---|---|
| `deploy.lock` | the host-wide lock |
| `prod/compose.yml` | the live deployment's compose file |
| `prod/current.json` | the live deployment's manifest: the image digest, revision, version and the workflow run that delivered it |
| `prod/secrets.env` | operator-managed credentials, mode 0600, never read by the script |
| `prod/compose.sh` | runs docker compose against the live deployment; see below |
| `prod/last-attempt.json` | the latest attempt's request and outcome, whether it succeeded or not |
| `prod/backup/` | the previous deployment's `compose.yml` and `current.json`, and `database.sqlite3`: the database as it was just before the live deployment replaced it |
| `prod/failed/` | only after a failed deployment: its `compose.yml`, `request.json`, `result.json` and the database snapshot taken before it. The next failure replaces it and the next success deletes it |
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

Every argument is passed to compose unchanged. The script holds no secret: it names
`secrets.env`, never its contents, and embeds only the three values `deploy.py` validated
before running anything. Its paths are relative to the script itself, so it works from any
directory. [Operations](operations.md) uses it for every command against the running
container.

### One backup, and why

After a successful deployment exactly one copy of the database exists on the host, in
`prod/backup/`. That is the owner's decision: every copy is one more place the owner's data
sits on disk, and one is enough to undo the deployment that is live. The previous layout
kept ten.

What that costs, accepted: a problem noticed two deployments late has no copy from before
it.

During a failed deployment's aftermath there can be two: `backup/`, and the snapshot in
`failed/`. The second is the database as it was just before the failed attempt, which is
the copy that matters if a migration went wrong.

A deployment that could not take a snapshot, because the previous container was not
healthy or had no database yet, leaves `backup/` exactly as it was rather than replacing
the only copy with nothing. The one exception is a host being migrated from the previous
layout, which has no backup yet; see below.

The backup is there for a person to restore by hand. A failed deployment does not restore
it: it restarts the previous image against the live database, as it always has.

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
   database becomes `backup/` instead: its database, its compose file, and its manifest
   (its result if that records a healthy deployment, its request otherwise). Only then is
   `attempts/` deleted, so the migration never leaves the host without a copy it had. If no
   attempt holds one, the deployment says so in one line of its log.

If both `~/portfolio-app-deploy` and `~/portfolio-app` exist, the deployment refuses and
changes nothing, so a person decides which one holds the live deployment. Anything of your
own that refers to `~/portfolio-app-deploy`, such as a cron job or a script, needs the new
path. The legacy application's `~/portfolio-deploy` is a different directory, and nothing
here touches it.

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
install -m 600 /dev/null ~/portfolio-app/prod/secrets.env
$EDITOR ~/portfolio-app/prod/secrets.env
```

The first command is for a host that has never been deployed to; on any other it would
empty the file. A host not yet migrated keeps the file under its previous root until its
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
always takes.

Do not re-run an old workflow to roll back: `deploy.py` rejects it by design.

## When something fails

Read `~/portfolio-app/prod/failed/`, which the error names as its evidence. It holds the
exact request, the compose file used, the database as it was before the attempt, and
`result.json` with the failure reason and whether the rollback came back healthy.
`~/portfolio-app/prod/last-attempt.json` always describes the latest attempt, successful or
not.

| Symptom | Likely cause |
|---|---|
| "The OCI revision label does not match" | the image was rebuilt outside the pipeline |
| "must not be group or world readable" | `secrets.env` permissions were loosened |
| "This workflow run is older than" | an old workflow run was re-run; push instead |
| "The deployment host is busy" | a concurrent deployment holds the lock; it will retry |
| "Both ... exist, so this host cannot be migrated safely" | the old and the new root both exist; keep the one holding the live deployment and move the other away |
| "The live deployment's compose file ... is missing" | `prod/compose.yml`, or the old layout's `attempts/<id>/compose.yml`, was deleted by hand; there is nothing to roll back to, so nothing was changed |
| "running and healthy, but recording it failed" | the new version is live, but a file under `prod/` could not be written; the next deployment repairs the files |
| Healthy container, stale behaviour | `secrets.env` changed but the container was restarted rather than recreated |
