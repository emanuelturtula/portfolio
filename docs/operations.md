# Operations

Day-two tasks on the running instance: creating the account, tuning the password hash to the
hardware, changing the password, understanding when a session ends, pointing the application
at the chain index it reads balances from, refreshing the prices that turn a balance into
a value, connecting the Bitget and BingX accounts whose trades say what each asset cost,
keeping the import of those trades running, reading the cost-basis snapshot built from
them, and checking that history against the balances actually held.

`docs/deployment.md` covers getting the image onto the host. This covers living with it.

Throughout, `<deploy-root>` is the live deployment's directory on the host,
`~/portfolio-app/prod` (its layout is in `docs/deployment.md`), and every command below
spells it out so it can be pasted as it is. `<origin>` is the scheme and host the browser
actually shows when you open the application — for example `https://portfolio.example`.
Neither the real host name nor any credential belongs in this repository, so both stay as
placeholders here and as real values only in the host-local `secrets.env`.

Every command against the running container goes through
`~/portfolio-app/prod/compose.sh`, which each successful deployment rewrites for what it
deployed. It passes its arguments to docker compose along with the image, port, environment
and secrets file that compose refuses to run without, so there is nothing to export first.

## 1. Required before the first deployment carrying authentication

**This is the one manual step the authentication change needs, and skipping it makes the
deployment fail.** The application now refuses to start in `prod` when
`PORTFOLIO_ALLOWED_ORIGIN` is still the development default, so the container never becomes
healthy and `deploy.py` rolls back.

Add both variables to the host-local secrets file — the same file exchange credentials go
in, at mode 0600, never through GitHub:

```bash
$EDITOR ~/portfolio-app/prod/secrets.env
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
~/portfolio-app/prod/compose.sh up -d --force-recreate app
```

A plain restart silently keeps the old values. That is already the last row of the
troubleshooting table in `docs/deployment.md`, and it catches people here too.

### Serving over plain HTTP on the local network

The application is built to sit behind HTTPS, and its defaults assume it. If you would rather
open it at the address the Raspberry Pi has on your own network, with no TLS in front, `prod`
accepts that when you say so twice — in the origin and in the cookie flag:

```
PORTFOLIO_ALLOWED_ORIGIN=http://<host-address>:<port>
PORTFOLIO_SESSION_COOKIE_SECURE=false
```

Both lines are required. A browser drops a `Secure` cookie that arrives over plain HTTP from
any host but `localhost`, so with only the origin set, login returns `204` and the page stays
on the sign-in form. The flag on its own is refused unless the origin starts with `http://`:
on an `https://` origin `Secure` stays mandatory.

**What this costs.** The password and the session cookie cross the network unencrypted, so
anyone who can capture traffic on that network can sign in as you. That is a reasonable trade
on a home network you control and not on a shared one, and the port must never be forwarded
to the internet.

A second cost is easy to miss. Without `Secure` the cookie is named `psid` rather than
`__Host-psid`, and browsers do not isolate cookies by port (RFC 6265 §8.5): every other
plain-HTTP service on the same address receives `psid` with each request and could log or
overwrite it, with no network capture needed. On a host that also runs other web applications,
that means trusting each of them with a live session token. Signing out deletes the session
server-side, which limits what a leaked token is worth afterwards.

Recreate the container after editing the file, exactly as above.

## 2. Creating the account without a bootstrap password

If you would rather not put the password in a file at all, leave
`PORTFOLIO_BOOTSTRAP_PASSWORD` unset and create the account interactively in the running
container:

```bash
~/portfolio-app/prod/compose.sh exec app python -m portfolio create-user --username <name>
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
~/portfolio-app/prod/compose.sh exec app python -m portfolio hash-benchmark
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
question, nothing to attack. Recover by setting a new password on the existing account from
the host:

```bash
~/portfolio-app/prod/compose.sh exec app python -m portfolio create-user --replace
```

The command asks for confirmation, then for the new password twice. It changes the password
on the existing account in place and **keeps its username**, so the command above is safe to
copy as it is. The account keeps its identity, so **wallets, balance history, exchange
accounts and imported fills are all kept**. Every session is signed out in the same
transaction, exactly as a password change through the application does, so a cookie stolen
before the recovery stops working the moment it completes. The last line of output names
the account that was changed.

To rename the account at the same time, add `--username <new-name>`. Without that flag the
username is never changed.

With no account yet, the command creates one, named by `--username` or, without it, by
`PORTFOLIO_BOOTSTRAP_USERNAME` (default `owner`). With more than one account — which the
application cannot produce, only hand-written SQL can — it refuses and changes nothing,
rather than guessing which one you mean.

## 5. Sessions

| | |
|---|---|
| Cookie | `__Host-psid` — `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, no `Domain`. Named `psid` and not `Secure` on a plain-HTTP deployment — section 1 |
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

**Failover, and why the reads are slow on purpose.** Anything other than an answer moves to
the fallback instance, and the rest of that read continues there — a connection failure, a
503, a 429, and equally a 401, a 403 or a 404. An instance that will not answer is exactly
what the second one is for, and the shapes a ban or an auth proxy actually take are refusals
rather than outages. **If one instance is misconfigured you will not see it in your
balances, which will keep arriving from the other one — you will see it in the health
check**, which probes each instance separately and is the thing to look at when something
feels wrong.

The one failure that does not fail over is an instance answering `200` with a body the
application cannot read. That is not a refusal, it is a sign that we no longer understand
what the vendor is sending, and asking somebody else would either produce the same
unreadable answer or a number that hides the problem.

Requests to one host are spaced by at least one second: mempool.space's documentation says that
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

## 9. Where Kaspa balances are read from

Balances come from a
[kaspa-rest-server](https://github.com/kaspa-ng/kaspa-rest-server) instance. The same two
variables and the same failover as section 8, and the same reading of what a refusal means.

| Variable | Default | What it is |
|---|---|---|
| `PORTFOLIO_KASPA_API_URL` | `https://api.kaspa.org` | The instance tried first. |
| `PORTFOLIO_KASPA_API_FALLBACK_URL` | *(blank)* | Tried when the first one fails. **Blank means one instance only**, which is the shipped default. |
| `PORTFOLIO_KASPA_NETWORK` | `mainnet` | `mainnet`, `testnet` or `devnet`. Must match the network the URLs above serve. |

**The fallback is blank on purpose.** Bitcoin has two independent public Esplora operators,
which is what makes one a usable fallback for the other. Kaspa has one well-known public
REST operator, so there is no second one to ship — and two variables pointed at the same
host is not a fallback: a 429 would cost one round of retries, and then the "failover" would
spend another round on the host that has just asked us to stop. The application recognises
that case and treats the two as one instance. Fill the fallback in if you run your own.

**`PORTFOLIO_KASPA_NETWORK` matters for the same reason as its Bitcoin counterpart**, and
here the check has no blind spot. `kaspa:`, `kaspatest:` and `kaspadev:` are three distinct
prefixes, each folded into the address checksum, so the same payload cannot be read as two
networks. An address from another network is refused offline, before a request is made.

That refusal is worth more here than it looks, and this was measured against the public
instance on 2026-09-23 rather than assumed. The server validates an address by matching
`^kaspa:[a-z0-9]{61,63}$` — prefix, character set and length, **and not the checksum**. So a
mistyped mainnet address that still matches that pattern is not refused: it is answered with
a balance of `0`, for a wallet that does not exist, on every sync, forever. This
application's own validation is strictly stronger, which is why an address that fails it
never leaves the process.

**Reads are batched, and there is one number in that which is a guess.** More than one
address is read in a single `POST`, up to 64 addresses per call. The vendor's API
documentation declares no maximum and names no ceiling anywhere, so 64 is a value chosen to
be comfortably small rather than one anybody verified.

If a server ever refuses a batch, the error says how many addresses were in it. **It only
suggests lowering the limit when the status can actually mean "too large"** — a 413 or the
422 this vendor documents. For any other refusal it names the size and stops there, because
the refusal you are most likely to meet is not about the batch at all: a 403 from a CDN or
firewall in front of the host, a 401 from an auth proxy, a 404 from a base URL with a typo
in it. Read the status in the message before you change anything. When the message *does*
say the batch may have been too large, the fix is to lower the limit in
`backend/src/portfolio/providers/chains/kaspa.py` — not to retry.

**The health check is stricter than the vendor's own, deliberately.** It requires the index
database to be synced *and* at least one backing node that is both synced and UTXO-indexed.
A node without the UTXO index answers a ping perfectly well and cannot answer a single
balance query, which is exactly the state where a simpler check says everything is fine and
every read fails. So this may report unhealthy where the vendor reports healthy. That is a
false alarm rather than a false balance, which is the direction worth being wrong in. The
health detail says how many nodes were usable and never which or where they are.

**Kaspa has no mempool figure**, so a Kaspa balance reports its pending amount as *unknown*
rather than as zero. Zero would be a claim that nothing is pending, which nobody has checked.

The same disclosure applies as in section 8: with the default URL, every Kaspa address you
register is sent to the public instance on every sync. Run your own if that is not a price
you want to pay.

## 10. Where prices come from, and refreshing them by hand

Balances are counts; prices are what turns a count into a value. Four sources, tried per pair
in a fixed order, **none of them required to be configured** — the primary needs no key and no
URL.

| Pair | Order | Notes |
|---|---|---|
| BTC/USD, BTC/EUR | Kraken, Coinbase, CoinGecko¹ | |
| KAS/USD | Kraken, Kaspa, CoinGecko¹ | Coinbase does not list KAS |
| KAS/EUR | Kraken, CoinGecko¹ | **Kraken is the only key-free source for this pair** |

¹ only when a CoinGecko key is set; see below.

### The schedule, and the two variables that set it

| Variable | Default | What it is |
|---|---|---|
| `PORTFOLIO_PRICE_REFRESH_ENABLED` | `true` | Whether the timer runs. Separate from the balance switch on purpose — see section 11. |
| `PORTFOLIO_PRICE_REFRESH_INTERVAL_MINUTES` | `60` | Minutes between refreshes. Must be at least 1; the container refuses to start otherwise. |

Sixty minutes because that is what `STALE_AFTER` is written against: a price is flagged stale
after an hour, so a price that has missed exactly one refresh is the first worth flagging.
**The two are a pair.** Lengthening the interval without lengthening the staleness threshold
marks every price stale most of the time, for no reason an operator can see.

A refresh at startup happens only if the newest `prices.fetched_at` is older than one
interval, for the same two reasons section 11 gives: a fresh deployment should not show every
holding unpriced for an hour, and a crash-looping container should not call four market-data
APIs on every restart. **When it is not due, the timer sleeps what is left of the interval,
not a whole one** — so a deploy at 12:50 after a 12:00 refresh refreshes again at 13:00, and
does not leave every price stale until 13:50.

**One residual, bounded.** Unlike the balance timer, this one counts *successes*: a refresh
writes rows only for what it fetched, and there is no record of an attempt. So while every
price source is failing, a crash-looping container costs one price request per restart. The
first refresh that succeeds writes rows and suppresses the next one.

**Prices read stale for a few seconds each hour, and that is accepted.** The interval equals
the staleness threshold and `as_of` is stamped when a refresh *starts*, so the previous price
is an hour and a few seconds old by the time the next one lands. It errs toward "stale", never
toward "fresh"; see `STALE_AFTER` in `services/prices.py`.

The command below is still worth having — it forces a refresh now rather than waiting out
the interval, and it prints the prices where the scheduler logs a count.

### Refreshing by hand

```bash
~/portfolio-app/prod/compose.sh exec app python -m portfolio refresh-prices
```

It fetches every supported pair once, writes what it got, and prints one line per pair:

```
as of 2026-09-23T12:00:00+00:00
BTC/EUR 79211.100000000000 via kraken
BTC/USD 86123.400000000000 via kraken
KAS/EUR 0.038881000000 via kraken
KAS/USD 0.042286450000 via kraken
```

`via <source>` is the source that **actually answered**, not the one that was asked first, so
a line reading `via coinbase` is how you find out Kraken was down without reading a log.

**The number is the one in the database, not the one the vendor sent**, printed at the
column's full twelve decimal places. A transcript showing the vendor's number would disagree
with the row every later valuation reads.

The trailing zeros are padding and carry no information on their own — every line gets twelve
places whatever the vendor sent. What the full scale is for is that it shows you **where the
column's precision ends**, so you can compare a line against the vendor's own page and see
whether anything was dropped: `0.042286450000` next to a quoted `0.0422864500004` tells you
the thirteenth place is gone, where a trimmed `0.04228645` would look like a clean price.

`as of` is the instant the refresh **began**, not the instant each price arrived: the clock
is read once, before the first request, so every row of one refresh carries the same
timestamp. A refresh that fails over across several hosts therefore stamps its rows a few
seconds early — which is the safe direction, since it can only make a price look older than
it is, never fresher.

**Exit code 1 means the refresh was incomplete**, and the pairs it could not fetch are printed
to stderr with a reason. A refresh that got three pairs out of four has not succeeded: the
missing one would otherwise surface days later as a portfolio total that has been quietly
short the whole time. The pairs that did work are still stored.

| Reason printed | What it means | What to do |
|---|---|---|
| `every_source_failed` | every eligible source was asked and none answered | look at the network, or at the vendors |
| `unsupported_pair` | this application does not price that pair | nothing was asked; check what you asked for |
| `no_source_configured` | the pair is supported but no source was available | check the configuration |

### The call budget

**One request per refresh**, because Kraken returns all four pairs in a single call —
measured on 2026-09-23. At an hourly refresh that is **24 a day and 24 × 30 = 720 a month**,
to one host, against a vendor that publishes no monthly quota for this endpoint. A 31-day
month is 744. The shared floor of one request per second per host is three orders of
magnitude above that.

A refresh with Kraken down costs more, because the fallbacks are not batched: at most three
requests to three different hosts, or four with CoinGecko configured. It never costs more than
one request per pair per source.

`docs/providers.md` carries the full arithmetic and the measurements behind it.

### The optional CoinGecko key

| Variable | Default | What it is |
|---|---|---|
| `PORTFOLIO_COINGECKO_API_KEY` | *(unset)* | a CoinGecko **Demo** key. Optional. |

**Everything works without it**, and that is the normal deployment: the three key-free
sources cover all four pairs. Setting it adds a last-resort fallback for every pair.

With the variable unset, **the source is not built at all** — not built and skipped, not
built. Nothing in the process holds a blank credential and nothing can reach the vendor.

If you do set it:

- It is a **Demo** key, not a Pro key. They use different hosts and different headers, and a
  Demo key sent to the Pro host is rejected.
- It goes in the host-local `secrets.env` and **nowhere else**. It is never written to the
  database, never returned by any endpoint and never logged; the application sends it as a
  request header on the one call that uses it, never in a URL.
- A wrong or exhausted key is not an outage. That source refuses, the failover moves past it,
  and the pair is priced by whoever else can — which is also why a wrong key can sit there
  unnoticed. If you set one, check a refresh line says `via coingecko` at least once with the
  other sources unreachable.

### What a missing price looks like, and why it is not a zero

A price that cannot be fetched is reported as **unavailable with a reason**, never as zero. A
portfolio total that silently omits a holding is indistinguishable from one that includes it,
and a zero renders, sums and gets believed. A valuation therefore comes back with the total it
could compute, the list of assets it could not price, and a flag saying the total is
incomplete.

**A price older than one hour is flagged stale and is still returned.** Staleness is computed
when the price is read, not stored, so it is never out of date by a second. The last known
price is better information than none — the same reasoning that makes an unreachable chain
report "unavailable" rather than a balance of zero.

### Two things worth knowing before you rely on this

- **No vendor returns a quote timestamp.** Measured on all three key-free sources. The `as_of`
  a price carries is when *we asked*, not when the vendor says the price was true, so a price
  can be older than it looks by however long the vendor cached it.
- **The Kaspa price endpoint does not say what currency it is in.** Its body is a bare number.
  USD is an inference, so that source is used only for KAS/USD, only after Kraken has failed,
  and never for KAS/EUR. If you need certainty about a KAS price's currency, use a refresh
  line that says `via kraken`.

## 11. Reading balances: the schedule, the run log and the off switch

The application reads every active wallet's balance on a timer and writes what it found to
`balance_snapshots`. Each attempt is one row in `sync_runs` plus one row per chain in
`sync_run_chains`, so what happened is a query rather than a guess.

| Variable | Default | What it is |
|---|---|---|
| `PORTFOLIO_BALANCE_SYNC_ENABLED` | `true` | Whether the timer runs. **Does not disable `POST /api/balances/sync`** — the manual trigger is the tool you debug a vendor with. |
| `PORTFOLIO_BALANCE_SYNC_INTERVAL_MINUTES` | `15` | Minutes between runs. Must be at least 1; the container refuses to start otherwise, because zero is a loop with no sleep in it against an index that documents a ban as the consequence. |
| `PORTFOLIO_BALANCE_SYNC_SHUTDOWN_GRACE_SECONDS` | `10` | How long shutdown waits for a run in flight before cancelling it and recording it `interrupted`. |

**There are two timers and they are deliberately independent.** Balances are on the variables
above; prices are on `PORTFOLIO_PRICE_REFRESH_ENABLED` and
`PORTFOLIO_PRICE_REFRESH_INTERVAL_MINUTES` in section 10. They are separate tasks with
separate switches, so neither can stop the other, and an operator waiting out a chain outage
does not also stop valuing the balances they already have. They answer to different vendors:
chain indexes that ban you for asking too often, against market-data APIs where the primary
answers every configured pair in a single call.

**At most one sync per interval, across restarts.** That is the property, stated exactly,
and three rules produce it:

- A sync runs at startup only if the newest run **of any status** started more than one
  interval ago. Attempts count, not successes: a container that dies faster than one sync
  takes — thirty Bitcoin wallets is thirty seconds at one request a second — leaves an
  `interrupted` run behind, and that run still asked two public indexes something.
- When a sync is not due at startup, the first wait is **what is left of the interval**,
  rounded up to a whole second, not a whole interval. A deploy does not push the schedule back.
- After that, one sync every `PORTFOLIO_BALANCE_SYNC_INTERVAL_MINUTES`.

Sleeping unconditionally at startup would leave a fresh deployment blank for fifteen minutes,
which is exactly when somebody is watching; running unconditionally would let a crash-looping
container hit two public indexes on every restart. One query answers both.

### Reading the run log

```bash
curl -s --cookie-jar - -b "$COOKIE" https://<host>/api/balances/runs | jq .
```

Each run carries `status`, `started_at`, `finished_at`, `duration_ms`, the three wallet counts
and one entry per chain. `duration_ms` comes from a monotonic clock, not from subtracting the
two timestamps, so a host that syncs its clock mid-run cannot report a negative one.

A run's `status` is one of five:

| Status | Means |
|---|---|
| `running` | in flight right now |
| `success` | every chain attempted produced balances — including a database with no wallets at all, which has nothing to read |
| `partial` | at least one chain worked and at least one did not. **The balances of the chains that worked were written** |
| `failed` | every chain attempted failed |
| `interrupted` | the process died, or was shut down, while the run was in flight |

A failed chain carries an `error_kind`, and **three different parties can be at fault**:

| Kind | Whose problem | What to do |
|---|---|---|
| `unavailable`, `rate_limited`, `response`, `unknown_chain` | the vendor | section 8 or 9 for that chain |
| `address_rejected` | **yours** | a wallet on that chain holds an address this chain will not accept — almost always the wrong network. Check `PORTFOLIO_BITCOIN_NETWORK` / `PORTFOLIO_KASPA_NETWORK` against the addresses in your wallet list |
| `internal` | ours | a bug. The container log carries the traceback |

`address_rejected` is separate from `internal` because it is a configuration mistake, not a
defect, and it used to be reported as one — with a traceback, four times an hour, forever.
`detail` names the reason and the number of wallets on that chain that went unread; **it never
names the address**, so you match it against your wallet list rather than against a log.

### Triggering one by hand

```bash
curl -X POST -H 'Content-Type: application/json' -H "Origin: https://<host>" \
     -b "$COOKIE" https://<host>/api/balances/sync
```

Pressing it twice does not start two runs: the second call attaches to the one already going
and returns that run's summary with `"joined": true`. That is also what happens when you
trigger one while the timer's own run is in progress.

### Reading the total, and what it is missing

`GET /api/balances/current?quote_currency=EUR` (or `USD`; anything else, lower-case included,
is a 422). **Read `complete` before you read `total`.** It is true only when nothing is missing,
and two lists say what is:

| List | What is missing | Usual cause |
|---|---|---|
| `unpriced` | an asset nothing could price | no price refresh yet, or every source failing — section 10 |
| `unread` | a wallet no run has ever read | added since the last sync, its chain failing, or `address_rejected` |

A wallet with an old reading is not `unread`; its `observed_at` says how old the number is.

### Paging through a wallet's history

`GET /api/wallets/<id>/balances` returns the **latest** `limit` readings by default. To walk a
longer stretch, start with `since=<instant with offset>` and follow `next_cursor`, passing it
back as `cursor`, until it is `null`. One page holds at most 1000 readings, which is about ten
days at the default interval, so a year is a walk of several pages. `cursor` and `since`
together is a 422, and so is a cursor the endpoint did not issue.

### When a run is interrupted

A `sync_runs` row is written at `running` **before the first request to any chain**, so a
process that is killed mid-sync leaves evidence. Any surviving `running` row is swept to
`interrupted` at startup, at shutdown, **and at the start of every run** — so a run whose
close-out failed is corrected within one interval rather than at the next restart, which on
a host that stays up can be weeks. `finished_at` and `duration_ms` are left null: the run has
no honest end time, and stamping the sweep's own clock on it would record a duration that is
mostly however long the container was down.

The sweep at the start of a run is safe because only one run happens at a time in the process
and there is one process. **Do not run a sync from a second process while the server is up** —
a future command that did would sweep the server's live run.

Snapshots are committed per chain as the run goes, so an interrupted run keeps whatever it had
already read.

## 12. Connecting the Bitget account

The application reads your Bitget **spot fills** -- every buy and sell execution -- with a
read-only API key, to know what you paid for each asset. The provider that reads them landed
with #13, and the sync that runs it and stores the fills with #15: once the three variables
below are set and the container recreated, the exchange timer imports fills every fifteen
minutes. Section 13 covers the sync -- its settings, what an account's status means, and how
to recover when Bitget refuses the key.

### Keep the account Classic: do not accept the Unified Trading Account upgrade

Bitget has two account systems, **Classic** and the **Unified Trading Account (UTA)**, and
the app offers the upgrade with a banner. **Do not accept it.** This application reads fills
through the Classic (v2) API. A UTA account reads them through a different API, with a
different cursor, window and field names, and Bitget's notice to broker partners states that
a UTA key cannot call Classic endpoints at all.

**What a Classic call made with a UTA account's key returns is not documented.** A refusal is
expected -- an auth or invalid-request error on every sync until the account is switched
back, which is loud and costs nothing but time. But it is expected, not documented. If the
venue answered with an empty success instead, it would be indistinguishable from a period in
which you made no trades: nothing would fail, the sync would move on, and once those weeks
aged past Bitget's 90-day retention the trades would be gone for good. That is one more
reason not to accept the upgrade. The application refuses the one undocumented empty shape it
can recognise, a `null` in place of the list of fills, but it cannot tell a documented empty
list from a real one. Support for UTA is #76.

Two facts from Bitget's documentation, read on 2026-09-25:

- **Since 2026-09-15 Bitget has been moving eligible Classic accounts to UTA automatically.
  An account with an API key linked is not eligible**, so the read-only key below also keeps
  the account where it is.
- **A main account can switch back** to Classic after an upgrade; a sub-account cannot.

To check which one you have: a Classic account shows separate Spot, Futures and Margin tabs,
and a banner offering the upgrade. The owner's account was Classic on 2026-09-25.

### Creating a read-only key

In Bitget's API management page, create a new API key:

1. If Bitget offers a choice of key type, choose the **system-generated (HMAC)** one. This
   application signs with HMAC-SHA256; it does not use RSA keys.
2. Set a **passphrase**. Bitget asks you to choose one when the key is created, and it is the
   third of the three values below, so keep it with the other two. Use printable ASCII with no
   space at either end -- the application refuses anything else at startup.
3. Grant **read-only** permission and nothing else. The application only ever reads fills,
   symbol information and, since #104, the spot account's balances (section 16); it never
   places, cancels or transfers anything, and a key that cannot is a key that cannot be
   misused. **Never grant trade, transfer or withdrawal.**
4. An IP allowlist is optional. If you set one, it must include the address the host's
   requests reach the internet from, or every sync is refused as an auth error (venue code
   `40018` or `40038`). Do not write that address into this repository.
5. Copy the **API key** and the **secret key** straight into `secrets.env`. Assume the
   secret is shown only once.

### The three variables, in `secrets.env` and nowhere else

| Variable | What it is |
|---|---|
| `PORTFOLIO_BITGET_API_KEY` | the API key |
| `PORTFOLIO_BITGET_API_SECRET` | the secret key |
| `PORTFOLIO_BITGET_API_PASSPHRASE` | the passphrase you chose for the key |

They go in the host-local secrets file -- the same file as section 1, at mode 0600, never
through GitHub, never in this repository, never in any other file:

```bash
$EDITOR ~/portfolio-app/prod/secrets.env
```

```
PORTFOLIO_BITGET_API_KEY=<the API key>
PORTFOLIO_BITGET_API_SECRET=<the secret key>
PORTFOLIO_BITGET_API_PASSPHRASE=<the passphrase you chose>
```

Then recreate the container, because `env_file` is read at creation:

```bash
~/portfolio-app/prod/compose.sh up -d --force-recreate app
```

**All three, or none.** With none set, Bitget is not configured and the provider is not built
at all -- nothing in the process holds a credential and nothing can reach the venue. The
container **refuses to start** if:

- only some of the three are set -- the log names the ones that are missing;
- any of them is set but blank;
- any of them holds text that cannot be encoded as UTF-8 -- usually a value copied from a
  file or a terminal in another encoding;
- the key or the passphrase holds a character an HTTP header cannot carry: a space or tab at
  either end, a line break or another control character, or anything outside printable
  ASCII. **A trailing space pasted along with the value is the usual cause.** The secret is
  not checked this way; it is never sent, only used to sign.

No refusal ever prints a value, only the variable's name. The credentials are never written
to the database, never returned by any endpoint and never logged: they travel in request
headers on the one call that needs them, and the log names that call
`https://api.bitget.com/exchange_fills` and nothing more.

### What a Bitget error means

An exchange error carries Bitget's own code, as `venue code NNNNN`, and never the text of
Bitget's message. The ones worth knowing:

| Venue code | Reported as | Means | What to do |
|---|---|---|---|
| `40008`, `40005` | unavailable | **the host clock**: Bitget refuses a request whose timestamp is more than 30 seconds from its own clock | check the clock is synchronised -- `timedatectl` should say `System clock synchronized: yes`. One `40008` right after a throttle is harmless: the transport resent a signed request late, and the next run signs a fresh one |
| `40006`, `40037`, `40041`, `40012`, `40036`, `40009` | auth | the key, the secret or the passphrase is wrong, or the key was deleted | check the three variables; create a new key if in doubt |
| `40018`, `40038` | auth | the request came from an address the key's IP allowlist does not include | update the allowlist, or remove it |
| `40014`, `40025`, `40040` | insufficient scope | the key lacks read permission | edit the key's permissions |
| `429` | rate limited | too many requests. Bitget's overall per-address limit takes five minutes to recover | nothing; the next run asks again |
| `40704` | retention window | a window older than Bitget keeps: "the last three months" | nothing; the sync starts later |
| `45001`, `40725`, `40808`, `40015` | unavailable | Bitget is deploying (Tuesdays and Thursdays) | nothing; the next run asks again |

Two refusals come from this application rather than from Bitget, and both name a field:

- **`feeDetail.deduction`**: the fill's fee was paid in **BGB**. What Bitget's fee fields hold
  then is not documented, so such a fill is refused rather than recorded with a guessed fee.
  If Bitget is set to pay fees with BGB, turn that off; fills from before that stay refused
  until support for BGB fees is written from a real example.
- **`feeDetail.totalFee`** "is positive": Bitget reported a fee with the opposite sign from
  its documentation. Refused rather than recorded as income. Report it; it needs a rule
  written from the real fill.

A sync that starts failing with an auth or invalid-request error right after you accepted
something in the Bitget app is most likely the UTA upgrade. Switch the main account back to
Classic. The same goes for a schema error saying `data must be an array of fills` -- and, since
the documentation does not say what a UTA account's key gets back, for syncs that suddenly
find no trades at all after an upgrade.

## 13. Syncing exchange fills

Every configured venue's spot fills are imported into `exchange_fills` on a timer of their
own. Each attempt is one row in `exchange_sync_runs` plus one row per account in
`exchange_sync_run_accounts`. The fills table is **append-only**: the database itself refuses
an update or a delete of a fill, and re-reading history the sync already holds inserts
nothing.

The signed-in `/exchanges` page shows all of this without a terminal: the imported fills with
their totals, the account list, a banner for any venue whose retention window truncated its
history, and the run log. The
`curl` commands below still work, and are what a script needs, but a human recovering an
`auth_failed` key can do the last step from the page - see step 4 below.

### The four settings

| Variable | Default | What it is |
|---|---|---|
| `PORTFOLIO_EXCHANGE_HISTORY_START` | unset | The earliest date to import fills from, `YYYY-MM-DD`, at 00:00 UTC. Unset means everything the venue still keeps. A date after today's (UTC) date is refused at startup. Moving it **earlier** later is supported: the next run imports the older range, as far as the venue's retention allows. |
| `PORTFOLIO_EXCHANGE_SYNC_ENABLED` | `true` | Whether the timer runs. **Does not disable `POST /api/exchanges/sync`**, as with the balance switch. |
| `PORTFOLIO_EXCHANGE_SYNC_INTERVAL_MINUTES` | `15` | Minutes between runs. Must be at least 1; the container refuses to start otherwise. |
| `PORTFOLIO_EXCHANGE_SYNC_SHUTDOWN_GRACE_SECONDS` | `10` | How long shutdown waits for a run in flight before cancelling it and recording it `interrupted`. The balance sync's grace runs at the same time, not after it. |

**The timer only exists when a venue is configured.** With no exchange credentials in
`secrets.env` nothing is scheduled and no empty run is written every fifteen minutes; the
manual trigger still works and records a run with nothing in it. The same at-most-once-per-
interval rule as the balance timer applies (section 11): the startup run happens only if the
newest exchange run, of any status, started more than one interval ago.

**The first sync is a backfill.** It reads everything from the history start -- clamped to
what the venue keeps, 90 days at Bitget and a year at BingX -- newest first, in windows the
venue accepts, one page at a time. Each page is committed with its checkpoint, so a restart
in the middle loses at most the page in flight and the next run resumes where the last one
stopped. Later runs read from where the previous plan ended, reaching five minutes back to
catch a fill the venue recorded late.

### The host clock must be synchronised

The sync plans by the host's clock, and a signed venue checks it: Bitget refuses any request
whose timestamp is more than 30 seconds from its own (venue code `40008`), and **BingX any
more than 5 seconds from its own** (venue code `100421`), both reported as `unavailable`.
Keep NTP on -- `timedatectl` should say `System clock synchronized: yes`.

If the clock is wrong anyway:

- **Ahead**: every request is refused while it is, so nothing is read. The run plans up to
  the wrong time; once the clock is corrected, the next run notices the plan is ahead of the
  clock (log event `exchange_sync_clock_behind_plan`), pulls it back, and the run after that
  reads everything from there. No fill is lost, but nothing is imported until the clock is
  right.
- **Behind**: the venue refuses requests as well, beyond its window. Once corrected, the
  sync re-reads from where the slow clock left the plan. That costs requests and inserts
  nothing twice. Only a clock behind by more than the venue's retention (90 days at Bitget,
  a year at BingX) loses history, and `history_truncated` then says so.

### Reading the account list

```bash
curl -s -b "$COOKIE" https://<host>/api/exchanges | jq .
```

One entry per configured venue, plus any venue that has an account row but no credentials
any more (`configured: false`). **Nothing here is a credential**: `configured` says whether
the process has one, never what it is.

| Field | Means |
|---|---|
| `status` | where the account stands; see below |
| `syncing` | a sync is running now and covers this venue |
| `requested_since` | what you asked for: `PORTFOLIO_EXCHANGE_HISTORY_START`, or 2009-01-03 when it is unset |
| `effective_since` | the instant from which the history held is complete |
| `history_truncated` | `effective_since` is later than `requested_since`: the venue did not keep everything you asked for, and **the history is complete from `effective_since`**. Some older fills may still be stored -- from before a long outage, or before the venue refused a window as too old -- but there is no promise about anything before it |
| `last_synced_at` | when a run last finished the account with nothing left to read |
| `fills_stored` | how many fills are stored for it |
| `pending_windows` | how many windows of history are planned and not yet read. Non-zero after a failure or an interruption; the next run continues from them |
| `last_error` | the kind and detail of the latest attempt, when that attempt failed. A run that skipped the account does not replace it |

`history_truncated: true` with the history start unset is the normal state at both venues:
you asked for everything, and Bitget keeps 90 days. At BingX this application **assumes** a
year. BingX's API documentation says 7 days, which it does not enforce. The only year BingX
states is its support centre's, about exporting trade history from the website, and the same
article says some regions and risk-controlled accounts get 30 days.

### What an account's status means

| `status` | Means | What to do |
|---|---|---|
| `never_synced` | no run has finished with this account yet | nothing, or trigger one |
| `ok` | the last run read everything planned | nothing |
| `error` | the last attempt failed; `last_error` says why. **The next scheduled run tries again** | see `error_kind` below |
| `auth_failed` | the venue refused the key, or the key lacks read permission. **Scheduled runs skip the account** (their outcome says `skipped`) until you act | recover as below |

`auth_failed` is not retried on the timer on purpose: asking a venue to refuse the same key
every fifteen minutes is how an address gets banned. **To recover:**

1. Fix the key at the venue, or create a new read-only one (section 12).
2. Put the corrected values in the host-local `secrets.env`, and nowhere else.
3. Recreate the container -- `env_file` is read at creation, so a restart is not enough:

   ```bash
   ~/portfolio-app/prod/compose.sh up -d --force-recreate app
   ```

4. Trigger a sync by hand. **A manual sync is the one that retries an `auth_failed`
   account**. On the `/exchanges` page, press **Sync now**; the same page shows the result
   once it lands. Scripting the same thing:

   ```bash
   curl -X POST -H 'Content-Type: application/json' -H "Origin: https://<host>" \
        -b "$COOKIE" https://<host>/api/exchanges/sync
   ```

   The response carries the run and one entry per account. `status: "success"` on the
   account means it is `ok` again, and the timer picks it up from there.

   **If the response says `"joined": true`**, your request attached to a scheduled or
   startup run that was already in flight -- the one right after the container came up,
   most likely -- and that run skipped the `auth_failed` account: its entry says `skipped`.
   Wait for it to finish (`GET /api/exchanges` shows `syncing: false`), then send the POST
   again. A run you start yourself is the one that retries the account.

### `error_kind`

| Kind | Whose problem | What happens next |
|---|---|---|
| `auth`, `insufficient_scope` | the key | the account becomes `auth_failed`; recover as above. `insufficient_scope` means the key was accepted but lacks read permission |
| `rate_limited` | the venue throttled us | each request is retried three times, waiting what the venue asks or 2, 4, then 8 seconds. A wait over 60 seconds is not waited: the account fails for this run, and the next one asks again |
| `unavailable` | the venue | the shared HTTP client already retried; the next run asks again |
| `retention_window` | the venue keeps less than it declares | first, once per window, the sync moves the refused window up to the retention edge as it stands now -- a long first backfill reaches its oldest window after that edge has moved on. If that does not help, it moves the window a day later and asks again, up to three times per window per run. When those run out the account fails, **keeping the moved start**, so the next run continues from there; `effective_since` rises with it |
| `invalid_request`, `schema` | the venue changed what it accepts or answers, or our request is wrong | read `detail`; it names a field and a rule. A cursor that returned to one already visited is a `schema` error too |
| `conflict` | see below | the account stops at that page until someone looks |
| `internal` | ours | a bug. The container log has the traceback; `detail` is only the exception's type name |

`detail` never holds a trade id, a symbol, an amount or anything the venue wrote in its
message -- only a fixed summary, the HTTP status and the venue's numeric code.

### `conflict`: a fill that changed under the same id

Re-reading a fill that is already stored is normal and inserts nothing. A re-read fill whose
trade id is stored but whose **contents differ** -- side, symbol, assets, quantity, price,
quote amount, fee, fee asset, order id or execution time -- is a conflict, and it is refused
rather than silently keeping either version. The page is rolled back, the checkpoint does not
move, and the account fails with `conflict` on every run until someone looks.

It means one of two things: the venue revised a settled fill, or its trade ids are not unique
per account the way this application assumes. Neither is fixed from here, and neither
recovers on its own; open an issue with the run's `detail` (a count, never an id). Recording a
correction as an adjustment is the cost-basis milestone's decision. A venue adding a new
field to its response is **not** a conflict: the stored payload is not compared.

### Reading the run log, and interrupted runs

```bash
curl -s -b "$COOKIE" "https://<host>/api/exchanges/runs?limit=20" | jq .
```

Newest first; `limit` is 1 to 100. Each run has the same five statuses as a balance run
(section 11), computed over the accounts it **attempted**: a run that only skipped an
`auth_failed` account is a `success` that did nothing. `fills_seen` counts every fill read,
overlap included; `fills_inserted` only the new ones, so a quiet interval shows a few seen and
none inserted.

A run row is written before the first request to any venue, and a surviving `running` row is
swept to `interrupted` at startup, at shutdown and at the start of every exchange run. An
interrupted run loses nothing already committed: every page is its own transaction, and the
next run resumes each window from its last committed cursor -- at Bitget, whose cursor is a
trade id, even when the retention edge has moved past the window's start in the meantime.
(BingX's cursor is a time, so there such a window is re-read from its first page, which
costs requests and inserts nothing twice.) **Do not run an exchange sync from a second
process while the server is up**, for the reason section 11 gives.

### Reading the imported fills

The Transactions section of the `/exchanges` page shows them, with filters and totals. The
same read from a terminal:

```bash
curl -s -b "$COOKIE" -G https://<host>/api/exchanges/fills \
  --data-urlencode exchange=bitget --data-urlencode exchange=bingx \
  --data-urlencode from=2026-03-01T00:00:00Z --data-urlencode to=2026-04-01T00:00:00Z \
  --data-urlencode limit=50 --data-urlencode offset=0 | jq .
```

Every parameter is optional:

- `exchange` is repeatable, and leaving it out means every venue.
- `from` is inclusive and `to` exclusive, each an ISO 8601 datetime **with an offset**. A
  naive one is refused rather than read as UTC. Use `--data-urlencode`, because a literal
  `+01:00` in a URL arrives as a space.
- `limit` is 1 to 200 (default 50). `offset` counts from 0.

The response has three parts:

- `fills` is newest first. Each fill carries its **order id**, which is what finds the trade
  at the venue, but never the venue's trade id.
- `total_count` is how many fills matched.
- `totals` covers **every** matching fill, whatever the page. It has per-asset quantities
  bought and sold, USDT spent and received, and fees per asset with their sign. A fill quoted
  in anything but USDT is summed in its own quote asset under `not_valued_in_usdt` and never
  converted.

Every amount is a string. The totals cover only what has been imported, so read them together
with `history_truncated`, `pending_windows` and `status` from the account list above.

## 14. Connecting the BingX account

The application reads your BingX **spot fills** -- every buy and sell execution -- with a
read-only API key, exactly as it reads Bitget's (section 12). The provider landed with #14.
Once the two variables below are set and the container recreated, the exchange timer imports
BingX fills alongside Bitget's; section 13 covers the sync, what an account's status means,
and how to recover when BingX refuses the key. Either venue can be configured without the
other.

### Creating a read-only key

On the BingX website, under **User Center → API Management**, create a new API key:

1. **Leave it read-only.** BingX creates new keys with read-only permission by default, and
   that is exactly what this application needs: it only ever reads fills and, since #104, the
   spot account's balances (section 16), and never places, cancels or transfers anything.
   **Never enable trading, transfer or withdrawal.**
2. BingX keys have no passphrase. There are two values, not three.
3. An IP whitelist is optional, and BingX recommends one. If you set one, it must include the
   address the host's requests reach the internet from, or every sync is refused with venue
   code `100419`, reported as an auth error. Do not write that address into this
   repository.
4. Copy the **API key** and the **secret key** straight into `secrets.env`. Assume the secret
   is shown only once.

### The two variables, in `secrets.env` and nowhere else

| Variable | What it is |
|---|---|
| `PORTFOLIO_BINGX_API_KEY` | the API key |
| `PORTFOLIO_BINGX_API_SECRET` | the secret key |

They go in the host-local secrets file -- the same file as section 1, at mode 0600, never
through GitHub, never in this repository, never in any other file:

```bash
$EDITOR ~/portfolio-app/prod/secrets.env
```

```
PORTFOLIO_BINGX_API_KEY=<the API key>
PORTFOLIO_BINGX_API_SECRET=<the secret key>
```

Then recreate the container, because `env_file` is read at creation:

```bash
~/portfolio-app/prod/compose.sh up -d --force-recreate app
```

**Both, or neither.** With neither set, BingX is not configured and the provider is not built
at all -- nothing in the process holds a credential and nothing can reach the venue. The
container **refuses to start** if:

- only one of the two is set -- the log names the one that is missing;
- either is set but blank;
- either holds text that cannot be encoded as UTF-8 -- usually a value copied from a file or
  a terminal in another encoding;
- the key holds a character an HTTP header cannot carry: a space or tab at either end, a line
  break or another control character, or anything outside printable ASCII. **A trailing space
  pasted along with the value is the usual cause.** The secret is not checked this way; it is
  never sent, only used to sign.

No refusal ever prints a value, only the variable's name. The credentials are never written to
the database, never returned by any endpoint and never logged. The key travels in a request
header and the signature in the query string of the one call that needs them, and the log
names that call `https://open-api.bingx.com/exchange_fills` and nothing more.

### What a BingX error means

A BingX error carries its code, as `venue code NNNNNN`, and never the text of BingX's
message. Every error BingX sent during testing came with HTTP status 200 and the code in the
body, so the code is what to read. The ones worth knowing:

| Venue code | Reported as | Means | What to do |
|---|---|---|---|
| `100421` | unavailable | **the host clock**: BingX refuses a request whose timestamp is more than **5 seconds** from its own | check the clock is synchronised -- `timedatectl` should say `System clock synchronized: yes`. Five seconds is tight: a clock NTP keeps is well inside it, and one that drifts is not. One `100421` right after a throttle or a server error is harmless: the transport resent a signed request late, and the next run signs a fresh one |
| `100419` | auth | the request came from an address the key's IP whitelist does not include | update the whitelist, or remove it |
| `100001`, `100412`, `100413` | auth | the secret or the key is wrong, or the key was deleted | check the two variables; create a new key if in doubt |
| `100414`, `100441`, `100401` | auth | BingX considers the account abnormal, or wants identity verification completed | sort it out with BingX in the app or with support, then recover as in section 13 |
| `100004` | insufficient scope | the key lacks read permission | edit the key's permissions |
| `100410`, `109429`, HTTP `418` | rate limited | too many requests; BingX restores a throttled account after five minutes, and a `418` means requests continued after a `429` | nothing; the next run asks again |
| `100500`, `100503` | unavailable | BingX is busy | nothing; the next run asks again |
| `100400`, `100204`, `100404`, `100490` | invalid request | BingX refused a request this application built | read `detail` and open an issue; it will not fix itself |

Three refusals come from this application rather than from BingX, and each names a field:

- **`commission` "is positive"**: BingX reported a fee with the opposite sign from its
  documentation. Refused rather than recorded as income. Report it; it needs a rule written
  from the real fill.
- **`symbol` "must be BASE-QUOTE"**: a fill arrived for a pair spelled in a way no BingX
  spot pair has been. The rule is wide -- anything before the last hyphen, up to 40
  characters, so `STRK-OLD-USDT`, `$U-USDT` and `MØTH-USDT` all pass -- and refuses only
  whitespace, control characters and a quote that is not upper-case letters and digits.
  Report it.
- **"a full page of 500 fills all executed in the millisecond it was asked from"**: more than
  500 fills share one millisecond, and the time cursor BingX pages with cannot get past them.
  Report it. It is not expected from one person's trading.

### Checking the first sync after trading a second symbol

The application asks BingX for every symbol at once, without naming one. BingX documents that,
and it held when this was tested -- but that account had traded a single symbol, so no answer
carrying two symbols has been seen yet. If BingX ever answered for one symbol only, the other
symbol's fills would be missing, and nothing would fail.

So the first time you have traded **two different pairs** on BingX, check once:

1. Trigger a sync (section 13, step 4) and wait for it to finish.
2. Count the fills stored for BingX -- on the `/exchanges` page, or as `fills_stored` in
   `GET /api/exchanges` -- and count the spot trades in BingX's own trade history for the
   same period. Both count executions, not
   orders: one order filled in several parts is several fills.
3. If this application holds fewer, open an issue saying so -- the counts, never the trades.

The same check is worth making once the account has more than 500 fills in any 30 days, the
first time BingX pages past a single answer.

## 15. The cost-basis snapshot: recomputing it, and reading it

The stored fills, and the manual adjustments the owner has entered, are replayed into one
position per asset -- quantity, cost basis, average cost, realized P&L -- by the engine
`docs/accounting.md` describes, and the result is kept as a **snapshot** in four tables
(`accounting_snapshots` and its positions, lots and warnings). `GET /api/accounting/positions`
serves that snapshot, valued at the cached USD prices. The contracts are specs
`docs/specs/021-position-snapshots.md` and `docs/specs/023-manual-adjustments.md`.

### When it is recomputed

- **Once at startup**, in the background. The health check does not wait for it, so a deploy
  never fails over a slow replay. This is the run that covers the first deploy over fills
  already stored, and an engine upgrade.
- **After every exchange sync run that stored at least one new fill**, inside that run. A
  manual `POST /api/exchanges/sync` therefore returns only once the snapshot is current.
- **After an exchange sync run that stored nothing, only if the last recompute failed**, so
  that a transient failure is retried at the next sync (every fifteen minutes by default)
  rather than at the next stored fill or restart. Otherwise a run that stored nothing leaves
  the snapshot alone.
- **After every change to a manual adjustment**, inside the request that made it: a `POST`,
  `PUT` or `DELETE` under `/api/accounting/adjustments` answers only once the snapshot is
  current. The change is committed first, so a recompute that fails does not undo it.
- **Never on a read.** `GET /api/accounting/positions` reads what is stored.

Two recomputes never overlap: a second one waits for the first. A recompute whose input has
not changed -- the same fills and adjustments, the same engine version -- writes nothing, and
the snapshot's `computed_at` stays where it was.

### The two log lines

```bash
~/portfolio-app/prod/compose.sh logs app | grep accounting_recompute
```

| Event | Fields | Meaning |
|---|---|---|
| `accounting_recompute_finished` | `reason`, `duration_ms`, `event_count`, `outcome` | `reason` is `startup`, `exchange_sync` or `adjustment`. `outcome` is `written` (the snapshot was replaced) or `unchanged` (the input was the same, nothing was written). `event_count` is the number of fills and adjustments replayed. |
| `accounting_recompute_failed` | `reason`, `duration_ms`, `error`, and `adjustment_id` when `error` is `UnconvertibleAdjustmentError` | The recompute raised. `error` is the exception's **class name only** -- never its message and never a traceback. The database engine already hides the values a failed statement was binding, but a message is free text that nothing promises to keep clear of a trade id or an amount, and the class name is enough to act on. `adjustment_id` names the manual adjustment to correct; an adjustment id is logged on every change to one anyway. A fill's identity is never logged. |

**The startup run's `duration_ms` on the Pi is the measurement for spec 021's criterion 8**
(under two seconds). It is the replay of the whole history on the real hardware.

### What a failed recompute means

**The previous snapshot stays exactly as it was, and is still served.** A recompute replaces
the snapshot in one transaction, and a failure rolls it back. Whatever triggered it is not
affected: a sync's run and fills are recorded as usual, and a manual adjustment stays saved and
the request that changed it still succeeds.

The endpoint says so. `last_recompute` carries the last attempt since the process started:

```bash
curl -s -b "$COOKIE" https://<host>/api/accounting/positions | jq '.computed_at, .last_recompute'
```

`computed_at` is when the snapshot served was written, `null` before the first one.
`last_recompute` is `{"at", "outcome", "error"}`, with `outcome` `unchanged`, `written` or
`failed`. It lives in memory, so it is `null` after a restart until the startup run finishes.

| `error` | What it means | What to do |
|---|---|---|
| `UnconvertibleFillError` | A stored fill has a shape the engine cannot account for: a pair whose base and quote are the same asset, a fee that consumes everything received, or a rebate larger than everything given. Ingestion has refused these since #99, so this is a row written before that, or by hand. | Do not edit the database: `exchange_fills` is append-only, and the recompute will keep failing until the row is dealt with. Report it with the venue and the date. The error names neither the account nor the trade on purpose, so that trade ids stay out of the log. |
| `UnconvertibleAdjustmentError` | A stored manual adjustment breaks a rule the engine enforces: a quantity not above zero, a negative cost, more than 18 decimal places, a cost times a quantity too large to represent, a blank asset, or a date that cannot be expressed in UTC. The API refuses all of these when an adjustment is entered, so this is a row written some other way, such as by hand on the Pi. | The `accounting_recompute_failed` log line names it: its `adjustment_id` field. The Adjustments page does not show ids. `GET /api/accounting/adjustments` in `/api/docs`, while signed in, lists each adjustment with its `id`: find the one the log names there, and note its asset and date. Then correct that adjustment, or delete it, on the Adjustments page, where its asset and date identify the row; either recomputes at once. "Recording an opening balance" below describes the page, and the API under `/api/accounting/adjustments` as the alternative to it. |
| `OperationalError` | SQLite refused the statement, most likely "database is locked": another write held the lock past the five-second busy timeout. Transient. | Nothing. It is retried at the next exchange sync, even one that stores nothing, and at the next restart. If it persists across several syncs, report it. |
| `StatementError` | The write was refused. The likeliest cause is a figure of 10²⁰ or more, which no column can hold and no real history reaches. | Report it. |
| `InvalidOperation` | Replay left the engine's range (spec 019, *Risks*). | Report it. |
| anything else | A defect of ours. | Report it with the class name and the time. |

**Every failed recompute is retried**: at the next exchange sync, whether or not that sync
stored a fill, at the next change to a manual adjustment, and at the next restart. A transient
failure such as `OperationalError` clears itself that way. A failure caused by the data --
`UnconvertibleFillError`, `UnconvertibleAdjustmentError`, `StatementError`,
`InvalidOperation` -- does not, because the same rows fail the same way every time; the retry
costs one replay per sync and changes nothing until the cause is dealt with.

### Recording an opening balance, or any acquisition the history does not show

A `negative_inventory` warning, and the `history_incomplete` flag on an asset, mean a sale
larger than everything the imported history holds -- usually coins bought before the venue's
retention window. The fix is a **manual adjustment**: an inflow of the asset, at its cost or at
an unknown cost, dated **before the first sale it has to cover**. `docs/accounting.md`,
"Recording what the history does not show", explains the rules and works an example.

**Enter it on the Adjustments page**, at `/adjustments` in the signed-in application. The page
lists the adjustments recorded, and one form records a new one or edits an existing one. A
delete asks for confirmation first. When the asset entered is one the imported history trades,
the form says when its earliest imported trade is and offers a date before it; the date is
offered, never filled in. The form's hint says which coins that date is for: coins already
held by then are dated before that trade, and **coins acquired later carry the date they were
acquired**. An inflow dated too early changes the cost applied to every sale in between, and
nothing warns about it (`docs/accounting.md`, "Dating an opening balance"). Under "Held exceeds
history", the dashboard's holdings check offers to record the missing coins and links each
asset it lists there to the page, with the asset filled in and nothing else.

The page calls these endpoints, which need a session, like every other:

| Method | Path | Does |
|---|---|---|
| `GET` | `/api/accounting/adjustments` | Lists them, in the order they replay. |
| `POST` | `/api/accounting/adjustments` | Records one. `201`. |
| `PUT` | `/api/accounting/adjustments/{id}` | Replaces all five fields, `unit_cost` included: `null` is an unknown cost. |
| `DELETE` | `/api/accounting/adjustments/{id}` | Deletes one. `204`. |
| `GET` | `/api/accounting/first-trades` | Per asset, the instant of the earliest imported fill it takes part in, as base asset, as quote asset, or as the asset of a fee that is not zero. Sorted by asset. USDC and USDT are left out and adjustments are not counted. It is where the page's suggested date comes from, and it reads the stored fills, so it does not wait for a recompute. |

**The alternative to the page is `/api/docs`, which works for all of these while signed in**
(section 6) **except the delete.** Every write must carry `Content-Type: application/json`,
and Swagger UI sends no content type for a request without a body, so a delete from there is
refused with a 403 before it reaches the endpoint. To delete without the page, use the browser
console, on a page of the signed-in application, with the adjustment's id in place of `<id>`:

```js
await fetch('/api/accounting/adjustments/<id>', {method: 'DELETE', headers: {'Content-Type': 'application/json'}})
```

The browser adds the `Origin` header and the session cookie itself; the promise resolves to a
response whose `status` is `204` when the adjustment is gone, and `404` when no adjustment of
yours has that id.

Every amount is a JSON string: a JSON number is refused. Each change answers after the
snapshot is recomputed, so `GET /api/accounting/positions` shows it straight away; if that
recompute fails, the change is still saved and `last_recompute` says `failed`. The log records
`adjustment_created`, `adjustment_updated` and `adjustment_deleted` with the adjustment's id
**and nothing else**: never the asset, an amount, a date or the note.

### Reading the positions

Every amount is a JSON string. Quantities, costs, values and P&L are at eighteen decimal
places; `price.amount` is the price as stored, at twelve; and the percentage is at four.
Valuation is in **USD**, because the unit of account is USDT/USDC pinned at 1. EUR is not
offered: it would need an exchange rate at every purchase, which the application does not
have.

**Only a chain's native asset is priced** -- BTC and KAS today -- because those are the only
pairs the price refresh fetches. Any other asset a venue traded shows `market_value: null` with
`market_value_unavailable_reason: "unsupported_pair"`, and is listed in `totals.excluded` as
`unpriced`. A chain asset with no price yet shows `never_fetched` instead (section 10). A
price that makes the value too large to represent -- 10²⁰ dollars or more, which no real price
reaches -- shows `value_out_of_range` and is left out of the totals the same way. An
asset holding units of unknown cost is listed there as `unknown_basis`. The totals cover only
what is left, so their percentage is the return on exactly the money in the total beside it.

Two totals are the exception and cover **every** position, held or closed, comparable or
excluded: `totals.realized_pnl` and `totals.unmatched_proceeds`. The second is what sales
brought in for units with no known cost -- units that arrived without a cost, or units sold
beyond what the imported history held -- kept out of realized P&L because there is no cost to
compare it with. It is signed: proceeds are net of fees, and a fee paid in a third asset can
cost more than the sale brought in. The dashboard shows it beside realized P&L whenever a
position carries any, with the assets it comes from.

## 16. The holdings check: which balances are compared, and what a failed read means

The cost-basis snapshot says what the imported history adds up to. The **holdings check**
compares that, per asset, with the balances read: each wallet's latest balance and each
venue's, for as long as those readings are current. More held than the history accounts for
usually means buys are missing from it, which no warning in the snapshot can show.
`docs/accounting.md`, "Checking the history against the balances held", explains the
comparison and what each result means; the contract is spec
`docs/specs/025-holdings-reconciliation.md`. The signed-in dashboard shows it as the
**Holdings check** block of the Invested section.

### What is read, and when

- **Only the spot account of each venue is read.** Earn, futures, margin and funding accounts
  are not. Coins held there are simply missing from the held side, which can make the history
  look larger than the balances (`history_over`, shown and never an error) and can never make
  it look smaller.
- **After every successful fill sync of a venue**, inside the same run, the venue is asked
  what its spot account holds, and the answer **replaces** the stored reading whole. One
  reading per account is kept; there is no history of balances. The one exception is a venue
  that refused the key on its last balance read: scheduled and startup runs do not ask it
  again, and a manual sync does (see "What to do about a `balances_error`").
- **Not after a failed or skipped fill sync.** Fresh balances beside a stale history would
  show differences that mean nothing.
- **Never on a read.** `GET /api/accounting/reconciliation` compares what is stored.

### Which readings are compared: the 24-hour rule

A reading that is out of date is worse than a missing one. Coins withdrawn from a venue to a
wallet after the venue was last read would be counted in both, and the check would report
units that do not exist as missing from the history. So a reading is compared only while it
is **current**, and one that is not adds nothing:

- **A venue** is compared when its last balance read succeeded, its fill sync is `ok`, and
  the reading is at most **24 hours** old.
- **A wallet** is compared when its latest reading is at most 24 hours old.

The limit is served as `max_reading_age_hours` and is not configurable. Both syncs run every
fifteen minutes by default, so a reading only reaches it when a source has stopped being
read. The age is measured when the request is served.

```bash
curl -s -b "$COOKIE" https://<host>/api/accounting/reconciliation \
  | jq '.max_reading_age_hours, .last_recompute, .exchanges, .wallets'
```

Each entry of `exchanges` has:

| Field | Means |
|---|---|
| `balances_read_at` | when that venue's balances were last read **successfully**; `null` when they never have been |
| `balances_error` | the kind the last attempt failed with, in the `error_kind` vocabulary of section 13; `null` when it succeeded or none was made |
| `not_compared_reason` | `null` when the venue's balances are in the comparison; otherwise why they are not |

`not_compared_reason` is the first of these that applies:

| Reason | Means | What to do |
|---|---|---|
| `read_failed` | The last balance read failed. The rows of the reading before it stay in the database and are **not** compared. | Read `balances_error`; see below. |
| `never_read` | No balance read has succeeded yet. | Nothing if the venue was just configured: the next successful fill sync reads it. If the venue shows `configured: false` on `GET /api/exchanges`, its credentials were removed and no sync will come. |
| `sync_failed` | The venue's fill sync is not `ok` (`error`, `auth_failed` or `never_synced`). Balances are read only after a successful fill sync, so nothing is refreshing the reading. | Fix the fill sync: section 13, "What an account's status means". |
| `out_of_date` | The reading is more than 24 hours old, although the last read succeeded and the account is `ok`. No balance read has succeeded for that venue since, which normally means no sync has run for it. | Check `PORTFOLIO_EXCHANGE_SYNC_ENABLED`, and whether the venue still shows `configured: true`. A manual sync of a configured venue refreshes it. |

`wallets` has three counts that add up to the active wallets: `compared`, `stale` (a reading
more than 24 hours old) and `unread` (no reading at all, section 11). A `stale` wallet is
usually one whose chain has been failing on every balance run, or the balance timer being
off; `GET /api/balances/runs` says which (section 11). `oldest_observed_at` is the oldest
reading among the compared ones.

A source that is left out adds nothing to the held side. That can hide a difference, and
cannot produce one. What can still produce a false one is coins moved between two current
readings, taken at different times by two different syncs: they are counted twice, or not at
all, until both sources have been read again. Those readings are **minutes** apart while both
syncs are running, and **up to 24 hours** apart when a source has stopped being read without
a recorded failure: a wallet whose chain is failing on every balance run (#116 will leave such
a wallet out), a venue whose credentials were removed or whose timer is off, and the double
failure logged as `exchange_balances_failure_not_recorded` (below). The dashboard shows each
reading's age. Treat a `history_short` as a prompt to look.

`last_recompute` is the one `GET /api/accounting/positions` serves (section 15). It is `null`
after a restart until the startup recompute ends, and the stored snapshot is compared
meanwhile. When its outcome is `failed`, the snapshot compared is older than the balances,
every asset bought since shows as missing from the history, and the dashboard shows no
comparison until a recompute succeeds. A shorter window of the same kind opens on every run
that stores a fill: between a sync's commits and its recompute, an asset bought in that sync
can show as `history_short`, and with two venues that window spans the second venue's sync.

### The read-only key should need no new permission (not verified with a real key)

The balances are read with the key the fills are read with (sections 12 and 14). Nothing
should have to be granted, and **nothing more should be**: trade, transfer and withdrawal
stay off.

What that rests on, read on 2026-10-01: BingX documents its spot balance endpoint as needing
the **Read** permission, which the key already has. Bitget's page for its spot assets endpoint
does not state a permission; the read-only permission is what its fills are read with.
`docs/providers.md` records both. **Neither endpoint had been called with a real key when
this was written.** If a venue does refuse the key for it, the fills keep syncing and the
refusal shows as described below.

### Where a failed balance read shows up

**Not in the account's status, and not in the run log.** `status`, `last_error` and
`GET /api/exchanges/runs` describe the fills, and a failed balance read changes none of them:
the account stays `ok` and the run stays a `success`. It shows in two places.

**In the holdings check itself**: the venue's entry has `balances_error` set and
`not_compared_reason: "read_failed"`, and the dashboard names the venue and the reason. The
rows of the last good reading are kept in the database, with their `balances_read_at`, and
are not used: that venue's coins are missing from the comparison until a read succeeds.

**In the container log**, one line per venue whose fills were synced, per run, and two when a
failed read could not be recorded either:

```bash
~/portfolio-app/prod/compose.sh logs app | grep exchange_balances
```

| Event | Fields | Meaning |
|---|---|---|
| `exchange_balances_read` | `exchange_key`, `assets` | The balances were read and stored. `assets` is how many assets the spot account holds a balance of. |
| `exchange_balances_read_failed` | `exchange_key`, `error_kind`, `error_type` | The read failed. `error_type` is the exception's class name. An `internal` kind is a defect of ours and the line carries the traceback. |
| `exchange_balances_read_skipped` | `exchange_key`, `reason` | A scheduled or startup run did not ask, because the last read was refused for the key. `reason` is `auth` or `insufficient_scope`. |
| `exchange_balances_failure_not_recorded` | `exchange_key`, `error_type` | The read failed **and** writing its kind to the account failed too, most likely "database is locked". It follows the `exchange_balances_read_failed` line of the same venue and carries the traceback. The run carries on and closes normally, and the fills are unaffected, but `balances_error` still shows what the attempt before left, so the endpoint does not show this failure: the two log lines are the only record. **The venue's previous reading then stays in the comparison** until a read succeeds or that reading is 24 hours old. Nothing to do unless it repeats; the next successful fill sync reads the balances again. |

A venue whose fill sync failed or was skipped has no line here: its balances were not asked
for. None of these fields carries an asset or an amount, and the reconciliation endpoint is
the only place a venue's balances are served.

### What to do about a `balances_error`

| Kind | What happens next |
|---|---|
| `unavailable`, `rate_limited` | Nothing to do. The next successful fill sync of that venue asks again, and the venue is left out of the comparison until a read succeeds. |
| `auth`, `insufficient_scope` | The venue refused the key for this read. **Scheduled and startup runs stop asking**, for the reason an `auth_failed` account is skipped (section 13), so the venue stays out of the comparison until you act. Fix the key at the venue, put the corrected values in `secrets.env`, recreate the container, and trigger a sync by hand: **a manual sync is the one that asks again**. If the response says `"joined": true`, wait and send it again, as in section 13. |
| `schema`, `invalid_request` | The venue answered something the parser does not recognise, or refused the request. The parsers refuse whatever they do not recognise rather than store a guess, so this is a missing reading and never a wrong number. Every run asks again. Report it with the venue and the kind. |
| `internal` | A defect of ours. The container log has the traceback. Every run asks again. Report it. |

The fills are untouched by any of these, and so are the positions.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Container never becomes healthy after the auth deployment, deployment rolls back | `PORTFOLIO_ALLOWED_ORIGIN` not set in `secrets.env` — section 1 |
| Container refuses to start, log names the bootstrap password | It is blank, under 12 characters, or a deny-listed default |
| Container refuses to start, log names `PORTFOLIO_SESSION_COOKIE_SECURE` | The flag is `false` but `PORTFOLIO_ALLOWED_ORIGIN` does not start with `http://` — section 1 |
| Login returns 204 but the app still shows the login page | The cookie was dropped. `__Host-` requires `Secure`, which requires HTTPS. On a plain `http://` origin, also set `PORTFOLIO_SESSION_COOKIE_SECURE=false` — section 1 |
| Reads work, every write returns 403 | `PORTFOLIO_ALLOWED_ORIGIN` does not match the address bar exactly |
| `/api/docs` returns 401 in the browser | Working as intended — sign in first, section 6 |
| Login returns 429 | Throttled — section 7 |
| Logged out roughly weekly | Working as intended: the 7-day idle window |
| Logged out roughly monthly despite daily use | Working as intended: the 30-day absolute ceiling, which activity does not extend |
| Edited `secrets.env`, nothing changed | `env_file` is read at container creation — recreate, do not restart |
| Container never becomes healthy after setting the Esplora URLs | One of them has no scheme, no host, or a scheme other than `http`/`https` — the startup log names which — section 8 |
| Container refuses to start, log names `PORTFOLIO_EXCHANGE_HISTORY_START` | The date is after today's date in UTC — section 13 |
| An exchange account stays `auth_failed` after fixing the key | Scheduled runs skip it: recreate the container, then trigger a sync by hand, and again if the first POST says `"joined": true` — section 13 |
| Exchange syncs fail `unavailable` with venue code `40008` | The host clock is off by more than 30 seconds — section 13, "The host clock must be synchronised" |
| An exchange account fails with `conflict` on every run | A stored fill changed under the same id; it needs a person — section 13 |
| The holdings check says a venue's balances could not be read, while the account is `ok` | A failed balance read never changes the account's status. Read `balances_error` — section 16 |
| A venue's `balances_error` stays `auth` or `insufficient_scope` after fixing the key | Scheduled runs do not ask again: recreate the container, then trigger a sync by hand — section 16 |
| The holdings check shows the history above the balances for an asset | Not a finding: only the spot account is read, so coins in Earn, futures or an unregistered wallet are not counted. A sale the import did not see looks the same, and the check cannot tell them apart — section 16 |
| The holdings check leaves a venue or a wallet out although nothing failed today | Its reading is more than 24 hours old, or the venue's fill sync is not `ok`. Read `not_compared_reason` and `wallets.stale` — section 16 |
| The dashboard shows no holdings comparison at all | The last recompute failed, so the history is older than the balances. Read `last_recompute` — sections 15 and 16 |
| A Bitcoin wallet reports "the address is on a different network" | `PORTFOLIO_BITCOIN_NETWORK` does not match the address — section 8 |
| Bitcoin balances stop updating and the log shows 429 | The public index is throttling us. Lengthen nothing by hand; run your own Esplora — section 8 |
| Reading many Bitcoin addresses takes a minute | Working as intended: one request per second per host — section 8 |
| Container never becomes healthy after setting the Kaspa URLs | One of them has no scheme, no host, or a scheme other than `http`/`https` — the startup log names which — section 9 |
| A Kaspa wallet reports "the address is on a different network" | `PORTFOLIO_KASPA_NETWORK` does not match the address's prefix — section 9 |
| "A batch of N addresses was refused… this status can mean the batch itself was too large" | The server's batch ceiling is below 64. Lower `MAX_ADDRESSES_PER_CALL` — section 9 |
| "A batch of N addresses was refused" with **no** sentence about the batch being too large | **Not a batch-size problem.** Read the HTTP status in the same message: 403 is usually a CDN or firewall block on the host, 401 an auth proxy in front of it, 404 a wrong base URL — section 9 |
| Kaspa health says "no node is synced and UTXO-indexed" | The upstream's nodes cannot answer a balance query, whatever a ping says — section 9 |
| A Kaspa balance shows its pending amount as unknown | Working as intended: this chain exposes no mempool figure — section 9 |
| Every holding reports "unpriced", reason `never_fetched` | No price refresh has completed yet. Check `PORTFOLIO_PRICE_REFRESH_ENABLED`, or force one with `refresh-prices` — section 10 |
| Prices update but balances do not, or the other way round | Two independent timers with two switches. Check the one that is quiet — sections 10 and 11 |
| A chain reports `address_rejected` on every run | A wallet on that chain holds an address for another network. Compare `PORTFOLIO_*_NETWORK` with your wallet list — section 11 |
| A wallet shows `confirmed: null` rather than a balance | No run has read it yet. Not the same as a zero, on purpose — check `/api/balances/runs` — section 11 |
| Balances never update and `/api/balances/runs` is empty | The timer is off. `PORTFOLIO_BALANCE_SYNC_ENABLED=false` — section 11 |
| Container refuses to start naming the sync interval | `PORTFOLIO_BALANCE_SYNC_INTERVAL_MINUTES` is zero or negative. To stop syncing, use the enabled flag — section 11 |
| A run says `partial` every time, one chain always failing | Read that chain's `error_kind`. The first four mean the vendor; `internal` means our bug and there is a traceback in the container log — section 11 |
| Runs pile up as `interrupted` | Each one was cut off by a restart or a deploy. If it is every run, the sync is outliving `PORTFOLIO_BALANCE_SYNC_SHUTDOWN_GRACE_SECONDS` — section 11 |
| Clicking refresh twice returns the same `run_id` | Working as intended: the second caller joins the run in flight rather than starting a second one — section 11 |
| `complete` is false and `unpriced` is empty | A wallet is listed under `unread`: no run has read it yet — section 11 |
| `quote_currency` returns 422 | Only `EUR` and `USD`, upper case — section 11 |
| A redeploy did not trigger a sync | Working as intended: the last attempt was less than one interval ago, and the timer resumes the schedule it had — section 11 |
| Prices flicker to stale for a few seconds on the hour | Accepted: the interval equals the staleness threshold — section 10 |
| `refresh-prices` exits 1 and names a pair as `every_source_failed` | Every eligible source refused or did not answer. Check connectivity, then the vendors — section 10 |
| `refresh-prices` exits 1 with `unsupported_pair` | The pair is not one this application prices. Nothing was asked — section 10 |
| A portfolio total looks too small | Check the incomplete flag: a total omits any holding it could not price, on purpose — section 10 |
| Prices are all flagged stale | The last refresh is over an hour old. The price is still shown; it is the age that is being reported — section 10 |
| KAS/EUR is the only pair that ever fails | Kraken is the only key-free source for it. CoinGecko is the only fallback — section 10 |
| A pair reports `every_source_failed` while the vendor is plainly up | A vendor can be refused for what it *sent*: a price of zero or below, a non-finite number, or one too large or too small for the column. Failover treats that like any other refusal — section 10 |
| Container refuses to start naming a `PORTFOLIO_BITGET_*` variable | Only some of the three are set, one is blank, one is not valid UTF-8, or the key or passphrase has a character a header cannot carry — usually a trailing space from pasting — section 12 |
| A Bitget error says venue code `40008` or `40005` | The host clock is more than 30 seconds off. Check `timedatectl`. A single one right after a throttle is harmless — section 12 |
| A Bitget error names `feeDetail.deduction` | Fees paid in BGB are not supported yet. Turn off paying fees with BGB in Bitget — section 12 |
| Bitget errors start, or Bitget syncs stop finding trades, right after accepting something in the Bitget app | Most likely the Unified Trading Account upgrade. What a Classic call returns then is not documented. Switch the main account back to Classic — section 12 |
| Container refuses to start naming a `PORTFOLIO_BINGX_*` variable | Only one of the two is set, one is blank, one is not valid UTF-8, or the key has a character a header cannot carry — usually a trailing space from pasting — section 14 |
| A BingX error says venue code `100421` | The host clock is more than 5 seconds off. Check `timedatectl`. A single one right after a throttle is harmless — section 14 |
| A BingX error says venue code `100419` | The key has an IP whitelist that does not include the host's address — section 14 |
| BingX holds fewer fills than BingX's own trade history shows | The case the application cannot detect by itself: report it with the two counts — section 14 |
| BingX holds more fills than its own trade history, around the time BingX renamed a pair (to a name like `XYZ-OLD-USDT`) | A fill read under both names is stored twice, because its id includes the pair's name. Report it with the two counts and the date; do not edit the database — section 14 |
| `/api/accounting/positions` has `computed_at: null` | No snapshot has been written yet. The startup recompute has not finished, or it failed: read `last_recompute` — section 15 |
| `last_recompute.outcome` is `failed` | The previous snapshot is still the one served. Read `error` — section 15 |
| New trades are imported but the positions do not change | Check `last_recompute`: a failed recompute keeps the old snapshot. If it says `unchanged`, the fills replayed were exactly the ones the snapshot was already computed from — section 15 |
| An asset shows `market_value: null` with `unsupported_pair` | Only chain assets (BTC, KAS) are priced. It is left out of the totals and named in `totals.excluded` — section 15 |
| A `negative_inventory` warning, and `history_incomplete` on an asset | A sale larger than the imported history holds. Record the missing coins as a manual adjustment dated before that sale, on the Adjustments page — section 15 |
| An opening balance was entered and the warning is still there | The adjustment is dated at or after the sale. At the same instant, a fill replays first. Edit it on the Adjustments page and date it earlier — section 15 |
| Deleting an adjustment from `/api/docs` returns 403 | Delete it on the Adjustments page (`/adjustments`). Swagger UI sends no content type for a request without a body, and every write needs `application/json`, so it cannot send the delete. Without the page, delete it from the browser console — section 15 |
| Creating an adjustment returns 422 naming `asset` | The symbol must be the venue's own spelling, upper case, such as `BTC`, and not USDC or USDT — section 15 |
