# Operations

Day-two tasks on the running instance: creating the account, tuning the password hash to the
hardware, changing the password, understanding when a session ends, and pointing the
application at the chain index it reads balances from.

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
docker compose -p portfolio-app-prod -f <deploy-root>/compose.yml exec app python -m portfolio create-user --username <name>
```

It prompts for the password twice, with no echo, and for nothing else. The account name is
**not** prompted for: it comes from `--username`, or from `PORTFOLIO_BOOTSTRAP_USERNAME`, or
from `owner` if neither is set. Pass `--username` explicitly unless you want `owner` — an
account silently created under a name you did not choose is a confusing way to fail to sign
in.

The password is never accepted as a command-line argument and never read from the
environment: a shell argument lands in the shell history and in `ps` output for every user
on the host.

This is the recommended route. The bootstrap variable exists for an unattended first boot;
this is the one that leaves no copy of the password anywhere.

## 3. Tuning the Argon2id parameters to this hardware

The shipped defaults are tuned to the Raspberry Pi 5 from a real measurement — see the log
below. Cost parameters copied between machines are the usual reason a password hash ends up
either uselessly fast or slow enough to be a denial-of-service vector against the login
endpoint, so re-measure on any host that is not that one, and after any hardware change.

```bash
docker compose -p portfolio-app-prod -f <deploy-root>/compose.yml exec app python -m portfolio hash-benchmark
```

It reports the median wall time of a hash with the parameters currently configured.

**Target: roughly 250 ms.** Below about 100 ms the hash is doing too little work to be worth
its complexity; above about 500 ms the login endpoint becomes a cheap way to pin a core.

To adjust, set any of these in `secrets.env` and recreate the container as in section 1:

| Variable | Default | Effect |
|---|---|---|
| `PORTFOLIO_ARGON2_MEMORY_COST` | `147456` (KiB, = 144 MiB) | The main dial. Memory is what makes the hash expensive to attack in parallel on a GPU, so raise this before anything else. |
| `PORTFOLIO_ARGON2_TIME_COST` | `3` | Number of passes. Raise only once memory is as high as the host can spare. |
| `PORTFOLIO_ARGON2_PARALLELISM` | `4` | Lanes. The Pi 5 has four cores; going above that buys nothing. |

In `prod`, both are floored in code at the OWASP minimum — `memory_cost >= 19456` KiB and
`time_cost >= 2` — and a value below either refuses to start rather than quietly weakening
the hash. The check is gated on `PORTFOLIO_ENVIRONMENT=prod`, which the image sets at build
time, because the test suite deliberately runs far below the floor to keep 500 tests fast.

Misreading KiB as MiB is the mistake this catches: `PORTFOLIO_ARGON2_MEMORY_COST=64` looks
like 64 MiB and is 64 KiB, a thousandfold weaker than intended.

Changing these does **not** invalidate the existing password: the parameters are encoded in
each stored hash, so an old hash still verifies, and it is transparently re-hashed with the
new parameters on the next successful login.

### Measured on this instance

Every row is a `hash-benchmark` run on the host named. Add a row rather than editing one:
the history is what tells the next person whether a slowdown is the hardware or the code.

| Date | Host | memory_cost | Median | Notes |
|---|---|---|---|---|
| 2026-09-20 | Raspberry Pi 5 | 65536 | 113.1 ms | idle host |
| 2026-09-20 | Raspberry Pi 5 | 65536 | 174.0 ms | **taken during a deployment — do not use** |
| 2026-09-20 | Raspberry Pi 5 | **147456** | **271 ms** | the shipped default; three runs on an idle host: 271.4, 270.8, 332.6 |

`time_cost=3` and `parallelism=4` throughout.

**Measure on an idle host, and measure more than once.** Those two 65536 rows are the same
binary and the same parameters 54% apart, because the second was taken while two deployments
were recreating containers. A single reading taken during a deploy is how you end up retuning
against noise — it nearly caused exactly that here.

Even idle, the third run of the shipped configuration came in 23% above the other two, which
agreed with each other to within 0.6 ms. Treat a lone high reading as interference and repeat
it rather than acting on it.

At 271 ms against a target of roughly 250 ms, the shipped default is where it should be.

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

## 6. The API documentation requires a session

`/api/docs` and `/api/openapi.json` are **not** public. Opening either in a browser that
has not signed in returns `401` with a problem document, not a login redirect, so it looks
broken rather than protected.

Sign in to the application first, in the same browser and on the same origin. The cookie
goes with the request and Swagger UI loads normally.

This is deliberate: the documentation enumerates the entire API surface, and there is no
reason an unauthenticated scan should get it for free. A command-line client fetching the
schema needs a session cookie; nothing in this repository does that, because the OpenAPI
drift check in CI generates the document in process rather than over HTTP.

## 7. Login throttling

Five failed attempts for a username inside 15 minutes; the sixth is rejected with `429`
before the password is even verified. A successful login clears the counter.

The counter is held in the application process, not the database, so a deployment or a
restart clears it. That is an accepted trade-off for a single-user application on a private
network — see `docs/specs/003-single-user-password-login.md`.

If you lock yourself out, wait 15 minutes or recreate the container.

## 8. Where Bitcoin balances are read from

Balances come from an [Esplora](https://github.com/Blockstream/esplora) instance. Two are
configured, tried in order, and the defaults are the public ones — so this works with no
configuration at all, and an operator running their own index changes two variables.

| Variable | Default | What it is |
|---|---|---|
| `PORTFOLIO_BITCOIN_ESPLORA_URL` | `https://mempool.space/api` | The instance tried first. |
| `PORTFOLIO_BITCOIN_ESPLORA_FALLBACK_URL` | `https://blockstream.info/api` | Tried when the first one fails. **Blank means one instance only.** |
| `PORTFOLIO_BITCOIN_NETWORK` | `mainnet` | `mainnet`, `testnet` or `regtest`. Must match the network the URLs above serve. |

Set them in `secrets.env` and recreate the container, as in section 1. No trailing slash is
needed on either URL; one is removed if you leave it.

**Include the scheme.** A URL with no scheme, no host, or a scheme other than `http` or
`https` is refused at startup: the container never becomes healthy and the deployment rolls
back, the same as the other unsafe configurations in section 1. That is deliberate and it
is the cheaper failure. `mempool.space/api` without the `https://` cannot be requested at
all, and `htp://` — one missing `t` — would otherwise be reported as "the chain is
unavailable" on every sync forever, with nothing anywhere mentioning the typo. The startup
message names the variable and the problem, and never echoes the URL, because these may
carry a username and password for a private instance.

**`PORTFOLIO_BITCOIN_NETWORK` is not cosmetic, and it is the one to get right.** An Esplora
instance serves exactly one network, and neither vendor documents what theirs answers for an
address from another one. So the application refuses an address that does not belong to the
configured network, offline, before it makes a request — because the alternative failure is
the expensive one: a balance read against the wrong chain comes back as a number rather than
an error, and nothing downstream can tell it from a correct one. If you point the URLs at a
testnet instance, set this to `testnet` in the same edit.

Two limits of that check, both of which the address itself cannot resolve:

- testnet3, testnet4 and signet are one network to this application. Pointing at a signet
  instance while holding testnet4 addresses produces confident, wrong answers.
- a legacy address (one starting `m`, `n` or `2`) on regtest looks exactly like a testnet
  one, so with `PORTFOLIO_BITCOIN_NETWORK=regtest` it is refused. Use a `bcrt1` address.

**Failover, and why the reads are slow on purpose.** A connection failure, a 5xx or a 429
moves to the fallback instance and the rest of that read continues there; any other refusal
stops, because the second instance runs the same software and would refuse it too. Requests
to one host are spaced by at least one second: mempool.space's documentation says that
exceeding its rate limit returns 429 and that repeatedly exceeding it may result in a ban,
and it publishes no numbers, so the interval is deliberately cautious. A ban would outlast
the sync that caused it. If a sync of many addresses feels slow, that is this, and the fix
is your own Esplora instance rather than a shorter interval.

Neither URL is a credential, and neither is logged: a provider request appears in the log as
its host and an endpoint label, never a path — the address is in the path on this API.

**What the defaults disclose, stated plainly.** This application goes to some trouble to
keep your addresses out of its own logs and out of this repository. It cannot do anything
about the other end: reading a balance means asking somebody who has the chain, and with the
defaults above that somebody is mempool.space and Blockstream. Every address you register is
sent to one of them, over TLS, on every sync, and they can see which addresses arrive
together from one IP — which is the set of addresses you own.

That is the price of not running an index, and it is the usual one; a block explorer in a
browser tab discloses the same thing. If it is not a price you want to pay, run your own
[Esplora](https://github.com/Blockstream/esplora) and point both variables at it. The
application does not care which instance answers, and the fallback URL may be left blank.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Container never becomes healthy after the auth deployment, deployment rolls back | `PORTFOLIO_ALLOWED_ORIGIN` not set in `secrets.env` — section 1 |
| Container refuses to start, log names the bootstrap password | It is blank, under 12 characters, or a deny-listed default |
| Login returns 204 but the app still shows the login page | The cookie was dropped. `__Host-` requires `Secure`, which requires HTTPS — check the origin is not plain HTTP on a non-`localhost` host |
| Reads work, every write returns 403 | `PORTFOLIO_ALLOWED_ORIGIN` does not match the address bar exactly |
| `/api/docs` returns 401 in the browser | Working as intended — sign in first, section 6 |
| Login returns 429 | Throttled — section 7 |
| Logged out roughly weekly | Working as intended: the 7-day idle window |
| Logged out roughly monthly despite daily use | Working as intended: the 30-day absolute ceiling, which activity does not extend |
| Edited `secrets.env`, nothing changed | `env_file` is read at container creation — recreate, do not restart |
| Container never becomes healthy after setting the Esplora URLs | One of them has no scheme, no host, or a scheme other than `http`/`https` — the startup log names which — section 8 |
| A Bitcoin wallet reports "the address is on a different network" | `PORTFOLIO_BITCOIN_NETWORK` does not match the address — section 8 |
| Bitcoin balances stop updating and the log shows 429 | The public index is throttling us. Lengthen nothing by hand; run your own Esplora — section 8 |
| Reading many Bitcoin addresses takes a minute | Working as intended: one request per second per host — section 8 |
