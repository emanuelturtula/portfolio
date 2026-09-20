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

1. Take a host-wide lock, so two deployments cannot interleave.
2. Refuse a workflow run older than the deployed one. Re-running an old workflow from the
   Actions UI would otherwise roll production backwards without anyone noticing.
3. Pull the digest and check the image's `org.opencontainers.image.revision` and `.version`
   labels against what CI claims. A digest that does not correspond to the commit is
   rejected.
4. Back up the live SQLite database using sqlite3's backup API from inside the running
   container, and verify it with `PRAGMA integrity_check`. This is a consistent snapshot
   even while the application is writing.
5. `docker compose up --wait`, then assert the container is healthy **and** running the
   exact digest requested.
6. On failure, restore the previous deployment and record the evidence.

State lives under `<deploy root>/prod/`:

| Path | What it holds |
|---|---|
| `current.json` | the last healthy deployment, written atomically |
| `secrets.env` | operator-managed credentials, mode 0600, never read by the script |
| `attempts/<id>/` | the compose file, the request, the previous manifest, the database backup and the result for each attempt (last 10 kept) |

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
install -m 600 /dev/null ~/portfolio-app-deploy/prod/secrets.env
$EDITOR ~/portfolio-app-deploy/prod/secrets.env
```

`deploy.py` refuses to run if that file is group- or world-readable. Changing it requires
recreating the container, not restarting it — `docker compose up --force-recreate app` —
because `env_file` is read at container creation.

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

Read the attempt directory named in the error. It contains the exact request, the previous
manifest, the compose file used, the database backup and the failure reason.

| Symptom | Likely cause |
|---|---|
| "The OCI revision label does not match" | the image was rebuilt outside the pipeline |
| "must not be group or world readable" | `secrets.env` permissions were loosened |
| "This workflow run is older than" | an old workflow run was re-run; push instead |
| "The deployment host is busy" | a concurrent deployment holds the lock; it will retry |
| Healthy container, stale behaviour | `secrets.env` changed but the container was restarted rather than recreated |
