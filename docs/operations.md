# Operations

Day-two tasks on the running instance: creating the account, tuning the password hash to the
hardware, changing the password, and understanding when a session ends.

`docs/deployment.md` covers getting the image onto the host. This covers living with it.

Throughout, `<deploy-root>` is the deployment root from `docs/deployment.md`
(`~/portfolio-app-deploy/prod`), and `<origin>` is the scheme and host the browser actually
shows when you open the application — for example `https://portfolio.example`. Neither the
real host name nor any credential belongs in this repository, so both stay as placeholders
here and as real values only in the host-local `secrets.env`.

## 1. Required before the first deployment carrying authentication

**This is the one manual step the authentication change needs, and skipping it makes the
deployment fail.** The application now refuses to start in `prod` when
`PORTFOLIO_ALLOWED_ORIGIN` is still the development default, so the container never becomes
healthy and `deploy.py` rolls back.

Add both variables to the host-local secrets file — the same file exchange credentials go
in, at mode 0600, never through GitHub:

```bash
$EDITOR <deploy-root>/secrets.env
```

```
PORTFOLIO_ALLOWED_ORIGIN=https://portfolio.example
PORTFOLIO_BOOTSTRAP_PASSWORD=<a long random password from a password manager>
```

`PORTFOLIO_ALLOWED_ORIGIN` must match the origin the browser sends: scheme and host, no
trailing slash, no path. A mismatch is not a startup failure — it is a `403` on every write
while reads keep working, which is a confusing symptom, so check it against the address bar
rather than against memory.

`PORTFOLIO_BOOTSTRAP_PASSWORD` is used once, to create the account on first start, and is
ignored on every later start. It must be at least 12 characters and must not be one of the
obvious defaults, or the application refuses to start. Delete the line once the account
exists.

`env_file` is read at container **creation**, so after editing this file:

```bash
docker compose -p portfolio-app-prod -f <deploy-root>/compose.yml up --force-recreate app
```

A plain restart silently keeps the old values. That is already the last row of the
troubleshooting table in `docs/deployment.md`, and it catches people here too.

## 2. Creating the account without a bootstrap password

If you would rather not put the password in a file at all, leave
`PORTFOLIO_BOOTSTRAP_PASSWORD` unset and create the account interactively in the running
container:

```bash
docker compose -p portfolio-app-prod -f <deploy-root>/compose.yml exec app python -m portfolio create-user
```

It prompts for the username and then for the password twice, with no echo. The password is
never accepted as a command-line argument and never read from the environment — a shell
argument lands in the shell history and in `ps` output for every user on the host.

This is the recommended route. The bootstrap variable exists for an unattended first boot;
this is the one that leaves no copy of the password anywhere.

## 3. Tuning the Argon2id parameters to this hardware

The defaults are an estimate for a Cortex-A76, not a measurement. Cost parameters copied
between machines are the usual reason a password hash ends up either uselessly fast or slow
enough to be a denial-of-service vector against the login endpoint, so take the measurement
on the Pi itself.

```bash
docker compose -p portfolio-app-prod -f <deploy-root>/compose.yml exec app python -m portfolio hash-benchmark
```

It reports the median wall time of a hash with the parameters currently configured.

**Target: roughly 250 ms.** Below about 100 ms the hash is doing too little work to be worth
its complexity; above about 500 ms the login endpoint becomes a cheap way to pin a core.

To adjust, set any of these in `secrets.env` and recreate the container as in section 1:

| Variable | Default | Effect |
|---|---|---|
| `PORTFOLIO_ARGON2_MEMORY_COST` | `65536` (KiB, = 64 MiB) | The main dial. Memory is what makes the hash expensive to attack in parallel on a GPU, so raise this before anything else. |
| `PORTFOLIO_ARGON2_TIME_COST` | `3` | Number of passes. Raise only once memory is as high as the host can spare. |
| `PORTFOLIO_ARGON2_PARALLELISM` | `4` | Lanes. The Pi 5 has four cores; going above that buys nothing. |

Floors are enforced in code (`memory_cost >= 19456` KiB, `time_cost >= 2`, the OWASP
minimum). A value below them refuses to start rather than quietly weakening the hash.

Changing these does **not** invalidate the existing password: the parameters are encoded in
each stored hash, so an old hash still verifies, and it is transparently re-hashed with the
new parameters on the next successful login.

### Measured on this instance

Fill this in after running the benchmark. An empty row means nobody has measured it and the
defaults are still a guess.

| Date | Host | memory_cost | time_cost | parallelism | Median |
|---|---|---|---|---|---|
| | Raspberry Pi 5 | 65536 | 3 | 4 | _not yet measured_ |

## 4. Changing the password

Through the application, which is the only route that checks the current password:
`POST /api/auth/password`, exposed in the UI from #4 onward.

Changing the password **revokes every session**, including the one that made the request.
Every browser gets a login page immediately afterwards. That is deliberate — a password
change is what you do when you think someone else may have a session.

If the password is lost entirely there is no reset flow, by design: no email, no recovery
question, nothing to attack. Recover by deleting the row and creating the account again:

```bash
docker compose -p portfolio-app-prod -f <deploy-root>/compose.yml exec app python -m portfolio create-user --replace
```

Deleting the user cascades to that user's sessions. Nothing else in the schema references
the user, so no portfolio data is lost.

## 5. Sessions

| | |
|---|---|
| Cookie | `__Host-psid` — `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, no `Domain` |
| Idle expiry | 7 days since the last request (`PORTFOLIO_SESSION_IDLE_DAYS`) |
| Absolute expiry | 30 days since login, never extended (`PORTFOLIO_SESSION_ABSOLUTE_DAYS`) |
| Stored as | a SHA-256 hash of the token, so a leaked database file yields no usable session |

Both expiries apply; whichever comes first ends the session. Logging out revokes the session
server-side, so replaying a captured cookie afterwards returns `401` rather than working
until it expires.

Expired rows are rejected on read and are not swept. One user produces a handful of rows a
year; a cleanup job would be more moving parts than the problem deserves.

Sessions live in the database, so restarting or recreating the container does not end them.
Changing the password is the only way to revoke every session at once.

## 6. Login throttling

Five failed attempts for a username inside 15 minutes; the sixth is rejected with `429`
before the password is even verified. A successful login clears the counter.

The counter is held in the application process, not the database, so a deployment or a
restart clears it. That is an accepted trade-off for a single-user application on a private
network — see `docs/specs/003-single-user-password-login.md`.

If you lock yourself out, wait 15 minutes or recreate the container.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Container never becomes healthy after the auth deployment, deployment rolls back | `PORTFOLIO_ALLOWED_ORIGIN` not set in `secrets.env` — section 1 |
| Container refuses to start, log names the bootstrap password | It is blank, under 12 characters, or a deny-listed default |
| Login returns 204 but the app still shows the login page | The cookie was dropped. `__Host-` requires `Secure`, which requires HTTPS — check the origin is not plain HTTP on a non-`localhost` host |
| Reads work, every write returns 403 | `PORTFOLIO_ALLOWED_ORIGIN` does not match the address bar exactly |
| Login returns 429 | Throttled — section 6 |
| Logged out roughly weekly | Working as intended: the 7-day idle window |
| Logged out roughly monthly despite daily use | Working as intended: the 30-day absolute ceiling, which activity does not extend |
| Edited `secrets.env`, nothing changed | `env_file` is read at container creation — recreate, do not restart |
