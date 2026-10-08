# Operations

Day-two tasks on the running instance: creating the account, tuning the password hash to the
hardware, changing the password, understanding when a session ends, pointing the application
at the chain index it reads balances from, refreshing the prices that turn a balance into
a value, backfilling the daily prices the value-over-time chart is drawn from, backing the
database up and restoring it, reading the logs, and reading how every source stands. Section
12 records what an operator does about the exchange sync, the accounting and the holdings
check, which were removed.

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

Add both variables to the host-local secrets file — the same file every credential goes in,
at mode 0600, never through GitHub:

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
copy as it is. The account keeps its identity, so **its wallets and their balance history
are kept**. Every session is signed out in the same
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

### Adding an extended public key instead of single addresses

A wallet that hands out a fresh address for every payment, and a fresh change address for
every spend, cannot be tracked one pasted address at a time. Paste its **account extended
public key** into the address field instead, with the chain set to Bitcoin. The application
derives the wallet's addresses itself, offline, and reads each one from the instances above.

| Paste | Addresses derived | Network |
|---|---|---|
| `zpub` | native segwit, P2WPKH (BIP84) | mainnet |
| `ypub` | nested segwit, P2SH-P2WPKH (BIP49) | mainnet |
| `xpub` | legacy, P2PKH (BIP44) | mainnet |
| `vpub` / `upub` / `tpub` | the same three, in that order | testnet, signet and regtest |

**The prefix decides the address type, and nothing else does.** Many wallets export an
account key as `xpub` whatever kind of addresses the account actually uses, and an `xpub`
here derives legacy addresses only. If a segwit wallet shows zero, that is almost always
why: export the key again as `zpub` (or `ypub` for nested segwit) — most wallets offer it
in the same screen — and register that instead.

What is refused, with the reason the form shows:

- **A private key** (`xprv`, `zprv` and the rest). The API refuses it with "This is a
  private key. Never enter it here or anywhere else.", before anything else about the value
  is read, on either chain, in two cases:
  - the value starts with a private prefix, ignoring surrounding spaces and invisible
    characters such as a zero-width space;
  - a whole private key appears anywhere in it: after other text and a space, inside
    quotes, or with an invisible character in the middle of it.

  The form applies the same test as you enter the value, clears the field and never sends
  it, and shows its own warning instead: the sentence above is what any other client of the
  API sees. A private key stuck directly onto other letters or digits,
  with nothing in between, is refused as an invalid address instead; it is still never
  stored. Nothing here ever needs one; if you pasted one anywhere, treat that wallet as
  compromised.
- **A multisig key** (`Ypub`, `Zpub`, `Upub`, `Vpub`): not supported.
- Taproot is not supported either: there is no taproot prefix to paste.

**The key is shown masked from then on**, as its first and last four characters, and it is
never served in full again — not in the list, not in the response to adding it. Keep your
own copy in your wallet software, where it came from.

**The first sync after adding one takes about a minute.** A key has two branches, receive
and change, and the scan reads addresses on each until it has seen 20 unused ones in a row
after the last used one (the standard gap limit of 20). That is at least 40 reads, and reads
to one host are spaced by at least a second, so at least 40 seconds; a wallet with history
takes about a second more per address it has used. The addresses it finds are remembered,
so a later sync derives only what is new — but it still **reads every remembered address on
every sync**, used or not, because a payment can arrive at any of them. A long-lived wallet
therefore makes each sync slower by roughly a second per address it has ever handed out. A
branch that would need more than 1000 addresses stops the scan with an error rather than
reading on; the plausible cause is an instance reporting history for every address.

The wallet counts as one wallet in the run log, and its balance is the sum over every
address read. One line per sync is logged for it, `balance_sync_extended_key_scanned`, with
the wallet's id and counts only; it is `INFO` when the scan found new addresses and `DEBUG`
otherwise.

**Do not also register an address the key derives.** The balance of an address that is both
registered on its own and derived from a registered key is counted twice, and nothing
detects the overlap. If you are moving from single addresses to the key, archive the single
addresses after adding the key.

**The same account is one wallet, however it was exported.** A key is compared by what it
derives — its prefix, chain code and public key — not by the string, so the same account
exported by two tools that write the rest of the key differently is refused the second time
as a duplicate. A `zpub` and an `xpub` of the same account are two wallets, because they derive
different addresses.

**The network check applies to the key as it does to an address.** A key is accepted
whichever network it is for, and the sync refuses it, as `address_rejected` with
`wrong_network`, when it does not match `PORTFOLIO_BITCOIN_NETWORK`: a `zpub` on a testnet
instance, a `vpub` on mainnet. Nothing is requested for it, and the rest of the Bitcoin
chain fails with it, on every sync. A key for the other network is one this instance cannot
read at all, so archive it: an archived wallet is not read.

#### Forgetting the derived addresses after switching between testnet and regtest

**A rare operator act**, for a development or self-hosted test instance; production is
mainnet and never needs it. `testnet` and `regtest` both read `vpub`, `upub` and `tpub` keys.
A `tpub` or `upub` wallet needs nothing: its addresses are Base58, byte for byte the same on
both networks, and they keep reading correctly after a switch. **A `vpub` wallet does**: its
addresses are bech32, `tb1` on testnet and `bcrt1` on regtest, the ones already derived were
encoded for the network the instance had then, and after a switch every sync is refused as
`wrong_network` while one is registered. Archiving the wallet and adding the key again does
not help: the archived wallet keeps its place, so the second add is refused as a duplicate,
and restoring it brings the old addresses back.

Instead, take a backup and make the application forget every derived address. The wallets,
their labels and their balance history stay; the next sync derives the addresses again for
the new network, at the cost of a first scan — for every key wallet, `tpub` and `upub` ones
included, since the command forgets them all. **The backup is the undo.**

```bash
~/portfolio-app/prod/compose.sh exec app python -m portfolio backup
~/portfolio-app/prod/compose.sh exec app python -c "import sqlite3; c = sqlite3.connect('/app/data/portfolio.db', timeout=30); n = c.execute('DELETE FROM derived_addresses').rowcount; c.commit(); print(n, 'derived addresses forgotten')"
```

#### Deleting extended-key wallets before a downgrade

**A rare operator act**, needed only to go back to a release from before extended keys. That
downgrade refuses while any key is registered, archived ones included, with
`Refusing to downgrade below 0011_extended_keys: ... wallet(s), archived ones included, hold
an extended public key.` The previous release has no way to tell a key from an address: it
would read the key as an address and fail the whole Bitcoin chain on every sync.

Archiving keeps the row, and there is no button that deletes a wallet, so take a backup and
delete those wallets by hand. **This loses their balance history** along with their derived
addresses — history the previous release could not have read anyway. **The backup is the
undo.**

```bash
~/portfolio-app/prod/compose.sh exec app python -m portfolio backup
~/portfolio-app/prod/compose.sh exec app python -c "import sqlite3; c = sqlite3.connect('/app/data/portfolio.db', timeout=30); c.execute('PRAGMA foreign_keys = ON'); n = c.execute(\"DELETE FROM wallets WHERE kind = 'extended_key'\").rowcount; c.commit(); print(n, 'extended-key wallets deleted')"
```

**`PRAGMA foreign_keys = ON` is not optional.** Python's `sqlite3` opens a connection with
foreign keys off, and without it the delete leaves every balance reading and derived address
of those wallets behind, pointing at a wallet that no longer exists. The downgrade drops the
derived addresses with their table, but the readings stay, and every startup after it warns
about them as `pre_existing_foreign_key_violations`.

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

### The price history, and the daily backfill

The dashboard's value-over-time chart needs a price for every past day, and `prices` keeps
only the price now. So prices are also kept per day, in `price_history` (spec 037): one row
per asset, quote currency and UTC day, marked with how good the number is.

| Basis | What it is | Written by |
|---|---|---|
| `close` | the day's closing price, from Kraken's daily candle. Final | the backfill below, over anything |
| `observed` | the latest price the hourly refresh saw that day | every refresh, for each pair it stored. Never over a `close` |

**The hourly refresh writes today's `observed` row** as it already fetches, so the history
grows from the first deploy whatever happens to the backfill. Today's point on the chart
therefore moves during the day; the day after, the backfill replaces it with the close.

**The backfill** asks Kraken's public OHLC endpoint for the daily candles of BTC/USD
(`XXBTZUSD`) and KAS/USD (`KASUSD`) and stores every committed close as `close`. Then, for
BTC/USD only, it asks **Coinbase Exchange's** public candles for every day before the earliest
close it has stored, back to 2015-07-20, and stores those as `close` too (spec 038). USD only:
the chart is in USDT, read as USD one for one. It runs on a timer of its own, `price-backfill`:

| Variable | Default | What it is |
|---|---|---|
| `PORTFOLIO_PRICE_BACKFILL_ENABLED` | `true` | Whether the timer runs. **Switches the timer only**: `backfill-prices` below works either way, and the hourly refresh still writes `observed` rows. |
| `PORTFOLIO_PRICE_BACKFILL_INTERVAL_MINUTES` | `1440` | Minutes between backfills: a day, because a new close appears once a day. Must be at least 1; the container refuses to start otherwise. Shorter rewrites the same rows more often. |

- **At startup it runs only if the newest `close` row was recorded more than one interval
  ago.** Like the price timer it counts successes, so the first start after this release
  backfills at once, a restart within the day asks nothing, and while both pairs fail a
  crash-looping container costs two requests per restart.
- **It is idempotent.** A close for a day is the same number on every run and its row is
  replaced, not added, so running it daily for a year, or twice in a row, leaves one row per
  day.
- **One pair failing does not stop the other.** Each pair is asked, written and committed on
  its own. A run that stored both logs `price_backfill_finished` with `days`, the number of
  closes it wrote; one that did not logs `price_backfill_incomplete` with `days` and `failed`,
  the pairs it could not read. Neither line carries a price.
- **Two requests a run, one run a day**, to the host the refresh already uses: 60 a month.
  **Coinbase is asked once**: the first run fills 2015-07-20 to the day before Kraken's first
  close in twelve requests of up to 300 days each, and from then on the earliest stored close
  is 2015-07-20, so it is asked nothing. `docs/providers.md` has the arithmetic and what was
  confirmed about both endpoints.

**Kraken serves the 720 most recent days and nothing older**, a rolling window. A day more
than 720 days old is in the history only if the backfill ran while that day was still inside
the window; the daily timer keeps it complete from the first deploy on. Older BTC days come
from Coinbase, above. **KAS has no price before 2024-11-19**, its first day on Kraken, and
Coinbase lists no KAS at all, so those days are a gap on the chart rather than a value.

### Backfilling by hand

```bash
~/portfolio-app/prod/compose.sh exec app python -m portfolio backfill-prices
```

It backfills every pair once, now, without waiting for the timer, and prints one line per
pair and source: how many closes it stored and their first and last day. A first run, with
what both vendors served when they were measured on 2026-10-08:

```
BTC/USD 720 day(s) via kraken: 2024-10-18 to 2026-10-07
BTC/USD 3378 day(s) via coinbase: 2015-07-20 to 2024-10-17
KAS/USD 688 day(s) via kraken: 2024-11-19 to 2026-10-07
```

Every later run prints no Coinbase line: the range before Kraken's window is already filled.

The last day is yesterday: today's candle is still trading and is never stored as a close.
**No price is printed** — 720 lines a pair would bury the answer, and the table holds them.

**Exit code 1 means a pair failed.** It is printed to stderr with its source and the class
name of the error, followed by a count of pairs, for example:

```
KAS/USD via kraken failed: ProviderUnavailableError
1 of 2 pair(s) were not fully backfilled.
```

The pair that worked is still stored, and so is the other source's answer for the same pair.
A failed Coinbase read stores none of its range, so the next run asks for all of it again.

| Error printed | What it means | What to do |
|---|---|---|
| `ProviderUnavailableError` | the source did not answer, or answered with a 5xx | check the network, then the vendor's status; run it again later |
| `ProviderRateLimitedError` | the source answered 429 after the transport's retries | wait, then run it again. The backfill asks Kraken twice a day and Coinbase only on its first fill, so a 429 most likely means something else on this IP is spending the limit |
| `ProviderResponseError` | the source answered, and the answer could not be trusted: an error in Kraken's envelope, a candle not at a UTC midnight, two candles for one day | report it; nothing for that pair from that source was written |
| `UnsupportedPair` | the pair has no row in `assets`, so nothing was asked | a defect: the migrations create both assets |
| `NoRecentClose` (`via coinbase`) | no Kraken close is stored for the pair yet, so there is no earliest day to fill back from; nothing was asked | fix Kraken's line first; the next run fills Coinbase's range |

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

**The timers that read from outside are deliberately independent.** Balances are on the
variables above; prices are on `PORTFOLIO_PRICE_REFRESH_ENABLED` and
`PORTFOLIO_PRICE_REFRESH_INTERVAL_MINUTES`, and the daily price backfill on
`PORTFOLIO_PRICE_BACKFILL_ENABLED` and `PORTFOLIO_PRICE_BACKFILL_INTERVAL_MINUTES`, all in
section 10. They are separate tasks with separate switches, so none can stop another, and an
operator waiting out a chain outage does not also stop valuing the balances they already
have. They answer to different vendors: chain indexes that ban you for asking too often,
against market-data APIs where the primary answers every configured pair in a single call.

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

### Reading the value over time

The dashboard's chart and the Details page's per-wallet chart read two endpoints (spec 037).
Both need a session, and **neither asks a vendor anything**: they read the stored snapshots
and `price_history` (section 10).

```bash
curl -s -b "$COOKIE" "<origin>/api/portfolio/history?range=90d" | jq .
curl -s -b "$COOKIE" "<origin>/api/wallets/<id>/value-history?range=30d" | jq .
```

`range` is `30d`, `90d` (the default), `1y` (365 days) or `all`, from the first day any active
wallet was read. Every day of the range is a point, oldest first, ending today (UTC), so a gap
shows as a gap. The portfolio answers `range` and `points`, each point a `day` and a `value`;
the wallet answers `wallet_id`, `asset`, `range` and `points`, each a `day`, a `quantity` and a
`value`. Amounts are JSON strings, values in USDT.

- **A day's balance is its closing balance**: each wallet's last reading before the next UTC
  midnight, carried forward over the days it was not read. A wallet with no reading yet by a
  day adds nothing to it.
- **A day nothing can value is `null`, never `"0"`**: no wallet had been read by its end, or a
  wallet holding something that day has no price for that day. A partial sum would be believed
  as the portfolio's value; a gap is not.
- **Today** is the latest readings at today's `observed` price, so it moves during the day.
- The portfolio counts **active wallets only**, as the summary does. A wallet's own history
  answers for an archived one too, and is a 404 for an id that is not the owner's.

Before a wallet's first snapshot the chart uses its **rebuilt** days, below, so a wallet
added today that has held coins for years charts those years. A wallet the rebuild could not
prove complete starts at its first snapshot, as before.

### Rebuilding past balances from the chain

A snapshot exists only from the day a wallet was added here. Everything before that is rebuilt
from the wallet's transactions (spec 038): the chain index lists every confirmed transaction of
each address, and walking them back from today's balance gives the closing balance of every
earlier day. The result is stored in `reconstructed_balances`, one row per wallet and UTC day
from its first transaction to today.

**A rebuild is stored only when it proves itself complete.** For each address, the
transactions read must number exactly what the index counts, their effects must add up to the
balance, the count and balance read before the paging must equal those read after it, and the
walk back must end at exactly 0 before the first transaction without ever going below it.
Anything less stores nothing and **keeps the rows the wallet had**: a partial history would
chart a false past, and a gap is not believed. An extended-key wallet sums every address it has
derived and used; a transfer between two of its own addresses nets to zero on its day.

It runs on a timer of its own, `balance-rebuild`:

| Variable | Default | What it is |
|---|---|---|
| `PORTFOLIO_BALANCE_REBUILD_ENABLED` | `true` | Whether the timer runs. **Switches the timer only**: `rebuild-balances` below works either way, and the balance sync is not affected. |
| `PORTFOLIO_BALANCE_REBUILD_INTERVAL_MINUTES` | `1440` | Minutes between rebuilds: a day. A wallet's past does not change, so more often reads the same transactions again. Must be at least 1; the container refuses to start otherwise. |

- **At startup it runs only if the newest rebuilt row was written more than one interval ago.**
  It counts successes, as the price backfill does: the first start after this release rebuilds
  at once, and a restart within the day asks nothing. While no wallet proves complete, every
  restart asks again.
- **Wallets are rebuilt one at a time, each committed on its own.** One that fails or proves
  incomplete does not stop the next. A run that rebuilt every wallet logs
  `balance_rebuild_finished` with `wallets` and `days`; one that did not logs
  `balance_rebuild_incomplete` with the same counts plus `incomplete`, `failed` and
  `unsupported`, each a list of `wallet_id:reason`. Neither line carries an address or an
  amount.
- **It costs one request per page of history**, through the shared floor of one request per
  second per host: an address with N transactions is about N/25 + 3 requests on Bitcoin
  (the pages, the empty page that ends them, and its figures read before and after) and
  about N/500 + 5 on Kaspa (the pages, and its count and balance read before and after). `docs/providers.md` has what was confirmed
  about both endpoints.

A transaction that confirms while an address is being read fails the before-and-after check;
the next day's run tries again. A pending transaction is not a past balance and is never read.

#### Rebuilding by hand

```bash
~/portfolio-app/prod/compose.sh exec app python -m portfolio rebuild-balances
```

It rebuilds every active wallet once, now, and prints one line per wallet:

```
wallet 1 (bitcoin) rebuilt: 1103 day(s) from 2023-09-28
wallet 2 (kaspa) incomplete: moved_during_read
```

**No address and no balance is printed.** **Exit code 1 means a wallet was not rebuilt**, and
its rows, if it had any, are kept.

| Printed | What it means | What to do |
|---|---|---|
| `incomplete: count_mismatch` | fewer or more transactions were read than the index counts | run it again; if it persists, report it with the chain |
| `incomplete: balance_mismatch` | the transactions read do not add up to the balance | as above |
| `incomplete: moved_during_read` | the address received or spent while it was read | run it again later |
| `incomplete: unresolved_input` | a Kaspa input came back without its source address or amount | run it again later |
| `incomplete: does_not_reach_zero` / `goes_negative` | the walk back did not end at 0, or crossed it | report it: the index served a history that does not add up |
| `failed: <ErrorClass>` | the index did not answer, or answered something that could not be trusted | the same errors as the balance sync, section 8 and 9 |
| `unsupported` | the chain's provider cannot read a history | a defect: both chains can |

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

## 12. Exchanges, accounting, adjustments and the holdings check: removed

Up to spec 036 this guide had five more sections here: connecting the Bitget account,
syncing exchange fills, connecting the BingX account, the cost-basis snapshot and manual
adjustments, and the holdings check. That functionality is gone. The application now tracks
wallets, read on-chain, and values them in USDT from the cached prices. Sections 13 to 16 are
left unused, so that the numbers of sections 17 to 19, which other files cite, stay right.

**Do these once, when the release carrying migration `0012_drop_exchanges_accounting` is
deployed.**

1. **Revoke the API keys at the venues.** Delete the read-only keys in Bitget's and BingX's
   own API-management pages. Nothing reads them any more, and a key that is no longer used
   is only a liability.
2. **Delete the variables from the host's secrets file**:

   ```bash
   $EDITOR ~/portfolio-app/prod/secrets.env
   ```

   Remove every line naming `PORTFOLIO_BITGET_API_KEY`, `PORTFOLIO_BITGET_API_SECRET`,
   `PORTFOLIO_BITGET_API_PASSPHRASE`, `PORTFOLIO_BINGX_API_KEY` or
   `PORTFOLIO_BINGX_API_SECRET`, and any of `PORTFOLIO_EXCHANGE_HISTORY_START`,
   `PORTFOLIO_EXCHANGE_SYNC_ENABLED`, `PORTFOLIO_EXCHANGE_SYNC_INTERVAL_MINUTES` and
   `PORTFOLIO_EXCHANGE_SYNC_SHUTDOWN_GRACE_SECONDS` if you set them. The application ignores
   them now, so a line left behind does not stop it starting; it is a credential sitting on
   the host for no reason. Then recreate the container so it no longer carries them in its
   environment:

   ```bash
   ~/portfolio-app/prod/compose.sh up -d --force-recreate app
   ```

**The deployment deletes the data, and the pre-deployment backup is where it survives.**
Migration `0012_drop_exchanges_accounting` drops eleven tables -- `exchange_accounts`,
`exchange_fills`, `exchange_sync_windows`, `exchange_sync_runs`,
`exchange_sync_run_accounts`, `exchange_balances`, `accounting_snapshots`,
`accounting_positions`, `accounting_lots`, `accounting_warnings` and `manual_adjustments` --
and the two triggers that kept `exchange_fills` append-only. Every imported fill, every sync
run, every cost-basis snapshot and every manual adjustment goes with them.

Before the new container starts, `deploy.py` snapshots the live database, as it does on every
deployment (`docs/deployment.md`, *What happens on the host*, step 6). Once the deployment
succeeds, that snapshot is `prod/backup/database.sqlite3`, and it is the copy holding the deleted
rows. **The next successful deployment replaces it**, because the host keeps one
(`docs/deployment.md`, *One backup, and why*). The scheduled copies in `/app/backups` taken
before the deployment hold the rows too, until the rotation removes them (section 17). If the
history may ever be wanted, copy one of them off the host before the next deployment, as
section 17, *Copying one off the host*, shows.

**Rolling back past 0012 means restoring that backup, not running the old image.** If the
deployment itself fails, the automatic rollback restores its own snapshot, so nothing is lost
(`docs/deployment.md`, *Rolling back*). After it has succeeded, an older image refuses to
start on a database at 0012, since it does not know the revision, and a revert fails to
deploy for the same reason. The migration's downgrade recreates the eleven tables and the two
triggers by running the upgrades of migrations 0006 to 0010, but **empty**: it cannot bring
back a row. Going back with the data means restoring a copy taken before the deployment, by
hand (section 17, *Restoring one*, and *Bringing a copy back onto the host* for the file in
`prod/backup/database.sqlite3`), which loses everything written since.

What else changed for an operator:

- `GET /api/health/detail` has no `exchanges` or `reconciliation` section, and its timers
  were three at that release: `balance-sync`, `price-refresh` and `backup`. Spec 037 has since
  added `price-backfill` and spec 038 `balance-rebuild`, so they are five (section 19).
- `GET /api/portfolio/summary` reports wallets only: `total_value`, `holdings` and `missing`,
  where each entry of `missing` is a `wallet_unread`, a `wallet_stale`, an `unpriced` or a
  `stale_price`. There is no invested figure and no profit or loss.
- `/api/exchanges/*` and `/api/accounting/*` are gone, and so are the Exchanges and
  Adjustments pages.

## 17. Backups: where they are, how they stand, and restoring one

The application copies its own database on a timer, checks each copy, and keeps a rotating
set of them. Why that matters, and why it is not the deployment's backup in `prod/backup/`,
is in `docs/deployment.md`, *Scheduled backups*. The contract is spec
`docs/specs/029-sqlite-backups.md`.

**Every copy holds the owner's complete financial data, as the live database does**: every
wallet address and extended public key, the balance history read from them, and the owner's
account with its password hash. A copy taken before migration
`0012_drop_exchanges_accounting` also holds the imported trades and manual adjustments that
migration deleted (section 12). Treat a copy you take off the host as you would the
database.

**The copies do not protect against losing the storage device**: they are on the same device
as the database. Copy one off the host from time to time, as *Copying one off the host* below
shows.

### Where the copies are, and the five settings

In the `backups` volume, mounted at `/app/backups` in the container. Each copy is one
self-contained SQLite file named after the UTC instant it was started, to the microsecond:
`portfolio-20261002T030000123456Z.sqlite3`. **Only files named that way are listed, rotated or
restored**; anything else in the directory is left alone and never deleted. A
`.portfolio-<stamp>.partial` file is a copy being written, or one an interrupted attempt left;
the first attempt more than an hour later removes it. A younger one is left alone, because
another process may still be writing it.

| Variable | Default | Meaning |
|---|---|---|
| `PORTFOLIO_BACKUP_ENABLED` | `true` | The timer only. `backup`, `list-backups` and `restore-backup` work either way. |
| `PORTFOLIO_BACKUP_INTERVAL_MINUTES` | `1440` | One day. Whole minutes, at least 60. Rotation keeps every copy of the most recent days, so the scheduled copies kept number about `KEEP_DAILY × 1440 / interval`: 7 at the default, 168 hourly. They share the database's disk. |
| `PORTFOLIO_BACKUP_DIR` | `./data/backups` | Where copies are written. The image sets `/app/backups`, and so does the deployment's compose file; leave it. The default is a development checkout's. |
| `PORTFOLIO_BACKUP_KEEP_DAILY` | `7` | Keep every copy on this many most recent days that have one. At least 1. |
| `PORTFOLIO_BACKUP_KEEP_WEEKLY` | `4` | Keep the newest copy of each of this many most recent ISO weeks that have one. At least 0. |

**Four of them can be changed in `secrets.env`**: `PORTFOLIO_BACKUP_ENABLED`,
`PORTFOLIO_BACKUP_INTERVAL_MINUTES`, `PORTFOLIO_BACKUP_KEEP_DAILY` and
`PORTFOLIO_BACKUP_KEEP_WEEKLY`. After changing one, run
`~/portfolio-app/prod/compose.sh up -d --force-recreate app`. A value out of range stops the
container from starting, and the log names the variable. **`PORTFOLIO_BACKUP_DIR` cannot be
changed there**: `deploy/compose.yml` sets it under `environment:`, which overrides
`env_file:`, so a value in `secrets.env` is ignored.

**When a copy is taken.** At startup if there is no copy yet, or the newest is older than one
interval; otherwise when the rest of the interval has passed, and then once per interval. The
newest copy's instant is the last run, so a restart or a deployment does not take an extra
copy.

**What rotation keeps.** After every copy that succeeded, and never after one that failed:
**every copy on the 7 most recent UTC days that have a copy**, and the newest copy of each of
the 4 most recent ISO weeks that have one. Counting days that have a copy, rather than the
last 7 calendar days, means a pause in backups does not empty the set when they resume.
Keeping every copy on those days means a copy taken by hand, or a restore's safety copy, does
not push out the scheduled copy of the same day: the copy from before a bad import survives
the copy you took after noticing it. Once its day falls out of the 7, only the newest copy of
its week can survive, so to keep a particular copy for longer, copy it off the host.

**If rotation fails**, the copy just taken has still been kept, and rotation may have deleted
some of the older copies it meant to delete before it stopped. The attempt counts as failed:
`backup_failed` names the kept copy in a `kept` field, and the state is `failed`.

### How their state shows

- **The Health page** has a *Backups* section: the state in words, the newest copy's date and
  time, and how many copies there are -- "unknown" for both while the directory cannot be
  read.
- **The dashboard** shows a warning above the Value section when the state is `failed`,
  `stale` or `unreadable`, and nothing otherwise.
- **`GET /api/health/detail`**, signed in. It is not public, unlike `GET /api/health`:

  ```bash
  curl -s -b "$COOKIE" <origin>/api/health/detail | jq .backup
  ```

  ```json
  {
    "state": "ok",
    "latest_at": "2026-10-02T03:00:00.123456Z",
    "count": 9,
    "last_attempt_at": "2026-10-02T03:00:00.123456Z",
    "last_error_kind": null
  }
  ```

  `latest_at` is the newest copy's instant and `count` the number of copies; both are `null`
  when the state is `unreadable`, because they are unknown, not zero. `last_attempt_at` and
  `last_error_kind` are the timer's most recent attempt since the process started, both
  `null` before one; they live in memory, so a restart clears them. No setting is served.

| `state` | Meaning |
|---|---|
| `unreadable` | The backup directory cannot be listed, so whether copies are being kept is unknown. It comes before every other state. Usually `/app/backups` is not a directory the container's user can read: check the volume in `deploy/compose.yml` and `PORTFOLIO_BACKUP_DIR`. The timer still attempts a copy at startup and records how it went in `last_error_kind`. |
| `ok` | The newest copy is less than two intervals old, and the timer's last attempt succeeded -- or it has not attempted one since the process started, which is the usual state after a restart. |
| `pending` | There is no copy yet, and the first attempt has not finished. A fresh installation shows this for the seconds its first copy takes. |
| `stale` | The newest copy is more than two intervals old, or there is no copy although an attempt has finished. Scheduled copies have stopped: check `PORTFOLIO_BACKUP_ENABLED` and the log. After a restart, a backup that keeps failing shows this until the timer's first attempt fails again. |
| `failed` | The timer's most recent attempt failed. `last_error_kind` says how; the table below says what to do. |
| `disabled` | `PORTFOLIO_BACKUP_ENABLED` is false. The copies already taken are still listed and counted. |

**In the container log**, one line per attempt:

```bash
~/portfolio-app/prod/compose.sh logs app | grep backup_
```

| Event | Fields | Meaning |
|---|---|---|
| `backup_completed` | `name`, `bytes`, `duration_ms`, `deleted` | A copy was taken, checked and kept. `deleted` is how many older copies rotation removed. |
| `backup_failed` | `error_kind`, `error_type`, and `kept` when rotation failed | The attempt failed. Without `kept`, nothing was kept or deleted. With `kept`, the copy it names was taken and kept, and the rotation after it failed, possibly after deleting some older copies. `error_type` is the class name of the error at the bottom, such as `OperationalError` or `PermissionError`. |

Neither line carries a row of the database or an error's message. Two warnings can appear
too, and neither fails the copy: `backup_wal_release_failed` (`error_type`), when the
clean-up after a copy could not run, which can make the next restore refuse as if the
database were open; and `backup_temporary_file_not_removed` (`suffix`), whose file an attempt
more than an hour later removes. A restore over a damaged database can log the first one too,
and it means nothing there. A defect of ours shows as `scheduler_tick_failed` with
`scheduler=backup` and a traceback, and as `failed` with `last_error_kind: null`.

| `error_kind` | What it means | What to do |
|---|---|---|
| `database_error` | SQLite could not open or read the live database, or `PORTFOLIO_DATABASE_URL` names no file. | Check that the container is healthy and the data volume is mounted. If the application itself works, report it with the `error_type`. |
| `integrity_failed` | The copy did not pass `PRAGMA integrity_check`, or its `alembic_version` is missing or does not hold one row. It was not kept. | A copy is read from the live database, so this points at the database itself. Take one by hand (below) to see the message. If the database is damaged, restoring the newest good copy is the remedy, and the restore moves the damaged file aside first (*Restoring one*). Note the newest copy's date before you choose: everything after it is lost from the live database. |
| `storage_error` | Writing, reading back, syncing, renaming or deleting a file in `/app/backups` failed: a full disk, or a directory the container's user cannot write. With `kept` in the log line, the copy was kept and the rotation after it failed. | Check free space with `df -h` on the host. A copy is about the size of the database; rotation keeps at least one a day for 7 days and up to 4 weekly ones, plus any taken by hand or by a restore on those days. |

### Taking one by hand, and listing them

With the application running:

```bash
~/portfolio-app/prod/compose.sh exec app python -m portfolio backup
~/portfolio-app/prod/compose.sh exec app python -m portfolio list-backups
```

```
Took backup portfolio-20261002T091500654321Z.sqlite3 (2154496 bytes).
```

```
portfolio-20261002T091500654321Z.sqlite3  2026-10-02T09:15:00.654321Z  2154496 bytes
portfolio-20261001T030000123456Z.sqlite3  2026-10-01T03:00:00.123456Z  2150400 bytes
```

`backup` is the same code as a scheduled copy, rotation included, and says which older copies
rotation deleted when it deleted any. It works whether the timer is on or not. On a failure it
prints one line and exits 1. It reads the live database without writing to it, with one
exception: after an unclean stop (the application killed, a power cut) the clean-up that
follows the copy writes the committed transactions still in the `-wal` into the live file, as
the application's next start would have, and removes the `-wal`. The copy already holds those
transactions. With the application stopped, use `run --rm --no-deps` in place of `exec`:

```bash
~/portfolio-app/prod/compose.sh run --rm --no-deps app python -m portfolio backup
```

### Restoring one

A restore replaces the whole database with a copy: everything after that copy was taken is
gone from the live database, and is kept only in the safety copy the restore takes first.
The application has to be stopped, so this is an operator's act and there is no button for
it.

**Do not merge to `main` while a restore is in progress.** A merge deploys, and the
deployment starts a new container on the database the restore is writing. Finish all five
steps, the check included, before merging anything.

1. **Choose the copy**, while the application still runs:

   ```bash
   ~/portfolio-app/prod/compose.sh exec app python -m portfolio list-backups
   ```

2. **Stop the application**:

   ```bash
   ~/portfolio-app/prod/compose.sh stop app
   ```

3. **Restore**, in a one-off container on the same image, volumes and settings:

   ```bash
   ~/portfolio-app/prod/compose.sh run --rm --no-deps app python -m portfolio restore-backup portfolio-20261001T030000123456Z.sqlite3
   ```

   It refuses while the database is open, refuses a name it does not find, a copy taken by a
   newer version and a copy with a `-wal` beside it, and checks the copy. Then it takes a **safety copy** of the live database
   (or, if the live database is damaged, moves it aside: see below), copies the chosen backup
   into the live file, checks the result, and prints:

   ```
   Restored portfolio-20261001T030000123456Z.sqlite3.
   The database as it was before is in the safety copy portfolio-20261002T093012345678Z.sqlite3.
   Rows per table after the restore:
     assets: 2
     ...
     wallets: 3
   ```

   The row counts are checked against the copy's own before this is printed. They are printed
   here and never logged.

4. **Start the application**:

   ```bash
   ~/portfolio-app/prod/compose.sh start app
   ```

   It migrates a copy taken by an older version up to the current schema, as it migrates any
   database at startup.

5. **Check**: `~/portfolio-app/prod/compose.sh ps` until the container is healthy, then sign in
   and look at the wallets and the dashboard. The Health page should show the
   backups as `ok`.

**A restore brings the account back as it was too.** The password is the one the account had
when the copy was taken, and the sessions are that copy's: every browser signed in since then
gets a login page, and a session revoked since then -- by logging out, or by a password change
-- is valid again until it expires (section 5). If the password was changed after the copy was
taken, sign in with the old one and change it again (section 4), which revokes every session
the copy brought back.

**To undo a restore**, restore the safety copy it printed, with the same five steps. The
safety copy is an ordinary copy and rotates like one: it is kept while its day is among the 7
most recent days that have a copy, and after that only if it is the newest copy of its ISO
week. If you may need it later than that, copy it off the host.

**Over a damaged database.** A damaged live database is the usual reason to restore, and it
is also one a safety copy cannot be taken of: its copy fails the same check. So when the
safety copy fails its check, or cannot read the live database, the restore checks the live
file itself. If the live file opens but fails that check -- it is not a database, it is
damaged, or it holds no schema revision, which is what a 0-byte file looks like -- the
restore **moves it aside** in the data volume, to
`/app/data/portfolio.db.damaged-<UTC stamp>`, takes no safety copy, and goes on. A
`portfolio.db-journal` beside it goes with it, to the same name followed by `-journal`,
because it belongs to the damaged file. Often there is none left by then: the safety copy
the restore tries first ends by opening the live file read-write, as every copy does, and
SQLite deals with a `-journal` on that open, rolling it back into the file or removing it.
So a `-journal` moves only when SQLite has not already used it. The restore prints:

```
Restored portfolio-20261001T030000123456Z.sqlite3.
The live database opened but did not pass its own check, so no safety copy was taken: it was moved aside to /app/data/portfolio.db.damaged-20261002T093012345678Z. Keep it until the restore is checked, then delete it.
```

**The moved file holds the owner's financial data**, as the database did: it is the database
as it was, damage included. Nothing lists, rotates or restores it. Keep it until step 5 has
shown the restore is right, then delete it by hand, with the application running:

```bash
~/portfolio-app/prod/compose.sh exec app rm /app/data/portfolio.db.damaged-20261002T093012345678Z
```

Delete its `-journal` the same way, if one was moved with it.

**Only a file that opens is judged damaged.** A live file that cannot be read at all -- a
permission, an I/O error from the storage device -- may be healthy, so the restore refuses,
and it stays where it is. If the live file passes its own check, the safety copy failed for
another reason; the restore refuses and moves nothing. A safety copy that cannot be written
at all -- a full disk, a backup directory the container cannot write -- also refuses, and
leaves the live file as it is. A file already at the name the damaged file would be moved to
is never overwritten: the restore refuses instead.

**Onto a new, empty data volume**, there is no live database to copy first, and the restore
says `There was no database to copy first, so no safety copy was taken.` The steps change
in three places, because there is no container to `exec` into or to stop: in step 1, list
with `~/portfolio-app/prod/compose.sh run --rm --no-deps app python -m portfolio list-backups`;
skip step 2; and in step 4 create the container with `~/portfolio-app/prod/compose.sh up -d app`
rather than `start` it.

**When the restore refuses or fails**, it prints one line and exits 1:

| Message begins | Why | What to do |
|---|---|---|
| `Refusing to restore: ... -wal exists, so the database is open` | The application is running, or it stopped without closing the database cleanly (killed, a power cut), or the clean-up after a copy failed. Nothing was changed. | `~/portfolio-app/prod/compose.sh ps`. If it is running, stop it. If it is already stopped, start it, wait until it is healthy, and stop it again, which lets SQLite recover the file. Then restore again. There is no option to skip this check. |
| `Refusing to restore: there is no database at ..., but ... lies beside its path` | There is no `portfolio.db`, but a `portfolio.db-journal` with content is in `/app/data`: most likely an earlier restore moved the damaged database aside and could not move its `-journal` with it. Writing the restored file would make SQLite discard that journal, which belongs to the damaged file (`leftover_journal`). Nothing was changed. | Move it beside the damaged file it belongs to, as `portfolio.db.damaged-<stamp>-journal` with that file's stamp, or out of the data directory, with `~/portfolio-app/prod/compose.sh run --rm --no-deps app mv <from> <to>`. Then restore again. |
| `Refusing to restore: '...' is not the name of a backup` | The name is mistyped. Nothing was changed. | Copy the name from `list-backups`. |
| `Refusing to restore: there is no backup named ...` | No copy by that name is in `/app/backups`. Nothing was changed. | Copy the name from `list-backups`. |
| `Refusing to restore: ... is at schema revision ...` | The copy was taken by a newer version of the application than the one running, and this one cannot migrate it. The message names both revisions. Nothing was changed. | Deploy that version or a newer one, then restore. |
| `Refusing to restore: ... is not a self-contained copy` | A `-wal` with content is beside the chosen copy in `/app/backups`, most likely because it was brought from elsewhere with its `-wal`. The restore reads the file alone, so the transactions in the `-wal` would be lost (`not_self_contained`). Nothing was changed. | Make it one file. Copy it and its `-wal` off the host as *Copying one off the host* shows. On your own machine, in a directory holding both, run `sqlite3 portfolio-<stamp>.sqlite3 'PRAGMA journal_mode=DELETE;'`, which writes the `-wal` into the file and deletes it. Remove the `-wal`, and a `-shm` if there is one, from `/app/backups` with `~/portfolio-app/prod/compose.sh run --rm --no-deps app rm <path>`. Then bring the file back as *Bringing a copy back onto the host* shows, and restore again. |
| `Refusing to restore: the backup ... did not pass its check` | The chosen copy is damaged (`integrity_failed`). Nothing was changed. | Choose another copy. |
| `The database's directory ... does not exist` | `PORTFOLIO_DATABASE_URL` points somewhere unexpected, or the data volume is not mounted. Nothing was changed. | Check the compose file and the volumes, and run the restore through `compose.sh` as above. |
| `Refusing to restore: no safety copy of ... could be taken, so nothing was changed` | The safety copy could not be written to `/app/backups` (`storage_error`): a full disk, or a directory the container's user cannot write. Nothing was changed. | `df -h` on the host. The rest of the message is the error underneath. Free space, then restore again. |
| `Refusing to restore: no safety copy of ... could be taken, and the live database passes its own check` | The safety copy failed, but the live database is sound, so it was not moved aside. Nothing was changed. | The rest of the message is the error underneath. Restore again; if it fails the same way, report it with the message. |
| `Refusing to restore: the live database ... cannot be read` | The safety copy could not read the live database, and neither could the restore's own check of it: a permission, or an I/O error from the storage device (`database_error`). A file that cannot be read may be healthy, so it was not moved aside. Nothing was changed. | Check the data volume, that the file belongs to the container's user, and the storage device (`dmesg` on the host shows I/O errors). Then restore again. |
| `Refusing to restore: the live database ... did not pass its own check, and ..., where it would be moved aside, already exists` | The live database is damaged, but a file is already at the name it, or its `-journal`, would be moved to. The name is the UTC instant to the microsecond, so the host's clock is wrong or the file was put there by hand. Nothing was changed. | Check the host's clock, and move the file the message names out of `/app/data`. Then restore again. |
| `Refusing to restore: ... appeared during the restore` | A `-wal` with content appeared beside a damaged live database before it was moved aside: something opened the database, most likely the application starting. Nothing was changed. | Stop the application, then restore again. |
| `The live database ... did not pass its own check, and moving it aside ... failed` | The live database is damaged and could not be renamed in `/app/data`. Nothing was restored, and the file is where it was. | Check the data volume's permissions, then restore again. |
| `The damaged live database was moved aside to ..., but moving ... to ... failed` | The damaged database was moved, but the `-journal` beside it could not be moved with it. Nothing was restored. | Move the `-journal` by hand to the name the message gives, with `~/portfolio-app/prod/compose.sh run --rm --no-deps app mv <from> <to>`, then restore again. Do not delete it: it belongs to the damaged file. Until it is moved, the restore refuses. |
| `The damaged live database was moved aside to ..., but the move could not be made durable` | The damaged database was renamed, and is at the name the message gives, but syncing `/app/data` afterwards failed, so a power cut could still undo the rename. Nothing was restored. | The storage device reported an error: check it (`dmesg` on the host). Then restore again: there is no live database at the usual name now, so the restore takes no safety copy, as onto an empty volume. |
| `The damaged live database was moved aside to ..., but ... cannot be removed` | The damaged database was moved, but a `-shm` or empty `-wal` beside it could not be deleted. Nothing was restored. | Delete the file the message names with `~/portfolio-app/prod/compose.sh run --rm --no-deps app rm <path>`, then restore again. |
| Anything naming the safety copy or the moved-aside file | The restore failed after the safety copy was taken, or after the damaged database was moved aside. | The message says which file holds the database as it was. Restore the safety copy; a moved-aside file is kept as evidence and is not a copy that can be restored. |

### Copying one off the host

**The copy holds the owner's complete financial data.** Keep it somewhere at least as private
as the Pi, preferably on an encrypted disk, and delete the intermediate copy on the host when
it has been moved.

With the application running, copy it out of the volume into your home directory on the host.
Give the destination as an absolute path: `compose.sh` runs from its own directory, so `.`
would mean `~/portfolio-app/prod`.

```bash
~/portfolio-app/prod/compose.sh cp app:/app/backups/portfolio-20261001T030000123456Z.sqlite3 ~/portfolio-20261001T030000123456Z.sqlite3
chmod 600 ~/portfolio-20261001T030000123456Z.sqlite3
```

Then, from your own machine, move it off the host and remove the one left in the home
directory:

```bash
scp <user>@<host>:portfolio-20261001T030000123456Z.sqlite3 .
ssh <user>@<host> rm portfolio-20261001T030000123456Z.sqlite3
```

A copy is a plain SQLite file and needs nothing else beside it to be read.

### Bringing a copy back onto the host

To restore a copy kept off the host, put it back in `/app/backups` first, owned by the
container's user. Copying it in the way it came out does not do that: `docker cp` creates
the file as root, and the application's user then cannot read it. A one-off container
running as root can. Copy the file into your home directory on the host (`scp` it there),
then:

```bash
~/portfolio-app/prod/compose.sh run --rm --no-deps -u root -v "$HOME/portfolio-20261001T030000123456Z.sqlite3:/import/portfolio-20261001T030000123456Z.sqlite3:ro" app sh -c 'cp /import/portfolio-20261001T030000123456Z.sqlite3 /app/backups/portfolio-20261001T030000123456Z.sqlite3 && chown app:app /app/backups/portfolio-20261001T030000123456Z.sqlite3 && chmod 600 /app/backups/portfolio-20261001T030000123456Z.sqlite3'
rm ~/portfolio-20261001T030000123456Z.sqlite3
```

Keep the name exactly as it was: only a file named like a copy is listed or restored. Then
`list-backups` shows it, and *Restoring one* applies. The image has `sh`, `cp`, `chown` and
`chmod`: its own build runs `sh`, `mkdir` and `chown` in the final stage, and all four
commands come from the base image's essential packages. It was run on the Pi on 2026-10-03,
with v0.29.0, as part of acceptance criterion 14 of spec 029.

## 18. Logs: one line per record, one id per request, and what is redacted

### Reading them

In production every record is **one JSON object on one line** on the container's stdout:
the application's own, and those of the libraries it uses -- `httpx`, `aiosqlite`, uvicorn --
which pass through the same processors and the same renderer. In development the same
records are rendered for a terminal instead.

```bash
~/portfolio-app/prod/compose.sh logs app
~/portfolio-app/prod/compose.sh logs app | grep '"level": "error"'
```

| Key | What it holds |
|---|---|
| `event` | What happened: `request_completed`, `backup_failed`, or a library's own message. |
| `level` | `debug`, `info`, `warning`, `error` or `critical`. |
| `timestamp` | When, in UTC, ISO 8601. |
| `request_id` | On every record written while serving a request (below). |
| `logger` | The library's logger name, on a record a library wrote. |
| `exception` | The traceback, as text, on a record that logged one. |

`PORTFOLIO_LOG_LEVEL` sets the level, `INFO` by default; a value it does not recognise is
`INFO` too. At `DEBUG` the health check's `request_completed` line appears every thirty
seconds, a line appears for every asset the web application loads, and the providers'
per-request lines appear too.

A record that cannot be written -- a library's message whose arguments do not format, say --
is dropped, and stderr gets one line in its place, naming the error's type and the logger:

```
--- Logging error: TypeError in a record from logger httpx; the record was not written ---
```

Never the record, its arguments or the traceback: the standard library's own report printed
all three, unredacted.

### Following one request

Every response carries an `X-Request-ID` header: a UUID, 36 characters in five hyphenated
groups, made by the server for that request -- the 200s, the 401s, the 404s, the 422s and the
500s alike. Every record written while serving it carries the same value as `request_id`,
whichever part of the application or which library wrote it. An `X-Request-ID` sent by a client is ignored: never
used, never logged, never echoed.

```bash
curl -si -b "$COOKIE" <origin>/api/wallets | grep -i '^x-request-id'
~/portfolio-app/prod/compose.sh logs app | grep 01234567-89ab-cdef-0123-456789abcdef
```

When the page shows "The server encountered an unexpected condition.", the response's
`X-Request-ID` leads to the `unhandled_exception` line with its traceback, and to the
`request_completed` line with status 500. The browser's developer tools show the header under
the request's response headers.

### `request_completed`, one line per request

| Field | Meaning |
|---|---|
| `method` | `GET`, `POST`, ... |
| `route` | What the request was for; the list below. |
| `status` | The status sent. A request that ended in an unhandled exception is 500. |
| `duration_ms` | How long the request took, in whole milliseconds. |

`route` is the first of these that applies:

1. **The matched route's template**, such as `/api/wallets/{wallet_id}`.
2. **The API's documentation**: `/api/openapi.json`, `/api/docs` and
   `/api/docs/oauth2-redirect`, each logged as its own path.
3. **`spa`** for any path outside `/api`: the web application's page and its assets.
4. **`unmatched`** for anything else under `/api`: a path with no route (404), or a request
   refused before routing for having no session -- the `request_refused` line beside it gives
   the reason.

**Never the raw path and never the query string.** It is at `INFO`, except at `DEBUG` for
`spa`, since one page load fetches several assets, and for `GET /api/health`, which the
container's health check calls every thirty seconds. It replaces uvicorn's access line, which
is no longer written: the access line carried the raw path and query.

### `request_refused`, a request turned away before any route

At `WARNING`, written before the request's `request_completed` line, with the same
`request_id`:

| Field | Meaning |
|---|---|
| `status` | `401` or `403`. |
| `reason` | `no_session`: no session cookie. `session_invalid`: a cookie that names no live session. `origin`: a request that changes state without the configured `Origin`. `content_type`: one that changes state and is not JSON. |
| `method` | `GET`, `POST`, ... |
| `path` | The path asked for, never the query string, and **at most 256 characters of it**. A longer path is cut back to the last `/` within its first 256, so no part of a segment is written. |
| `path_truncated` | `true`, only on a path that was cut. |
| `path_length` | Only on a path that was cut: its full length, in characters. |

The path is the one thing in these lines that a client with no session chooses. Written whole,
one request could make the application write a line as long as the path, 200 KB and more.
A `path_truncated` line is never the web application's: the API's own paths are far shorter.

### What is redacted

Two rules run on every record, the libraries' included, immediately before it is rendered.
Each replaces what it finds with `[REDACTED]`.

**By key name.** The value of any field whose name contains `secret`, `passphrase`,
`api_key`, `apikey`, `token`, `authorization`, `signature`, `password` or `address`, or starts
with `xpub`, `ypub`, `zpub`, `xprv`, `yprv`, `zprv`, `tprv`, `uprv` or `vprv`, in any case and
however deeply nested.

**By value**, inside every string of the record -- the message, the traceback, every field's
value, and every key at any depth, so a field keyed by address is caught too. The rules repeat
over a string until nothing more changes, so two values written back to back are both caught:

- **every credential the application holds**: the bootstrap password and the CoinGecko key
  -- every `SecretStr` setting, found by type, so one added later is covered too. Wherever it appears when it is 8 characters or longer, and
  only as a whole value when shorter, so that a short value does not redact ordinary words;
- **extended keys, public and private**: the public `xpub`, `ypub`, `zpub`, `tpub`, `upub`,
  `vpub`, `Ypub`, `Zpub`, `Upub` and `Vpub`, and the private `xprv`, `yprv`, `zprv`, `tprv`,
  `uprv`, `vprv`, `Yprv`, `Zprv`, `Uprv` and `Vprv`, followed by 100 or more Base58
  characters. Registration refuses a private key before reading it; this is for one that
  reaches a log some other way;
- **addresses**, mainnet and testnet: bech32 and bech32m starting `bc1`, `tb1` or `bcrt1` in
  either case; Base58 addresses starting `1`, `3`, `m`, `n` or `2`; Kaspa addresses with the
  `kaspa:`, `kaspatest:`, `kaspasim:` or `kaspadev:` prefix. No checksum is checked, so a word
  that merely looks like one is redacted too;
- **the query string of any URL**: `https://host/path?query` is logged as
  `https://host/path?[REDACTED]`. A query string can carry a key or a signature.

Nothing is exempt, `request_id` included. A request id never matches a rule: its longest run
of characters without a hyphen is 12, and the shortest address a rule recognises is 14.

### What is not redacted

- **A value that is none of the above**: an exception message quoting a trade id, an amount,
  an asset or a label is logged as it is.
- A credential encoded some other way -- percent-encoded, base64 -- and a path's query when
  the URL has no scheme.
- Part or all of a URL's query in four forms: what follows a quote inside the query; what
  follows an unencoded `#` inside it, which is read as the fragment; what follows a `?`
  inside the fragment, which is not a query; and the whole query of a URL whose slashes are
  JSON-escaped (`https:\/\/host\/path?query`), which has no `://`.
- An address joined to a letter or a digit with nothing between them, such as `x<address>`.
  A space, an underscore, a hyphen, a slash or other punctuation separates it, so
  `wallet_<address>` is redacted.
- Parts of two addresses joined with nothing between them:
  - after a Bitcoin bech32 address (`bc1`, `tb1`, `bcrt1`), a second one starting with `t`,
    `k`, `m`, `n`, `2` or `3` is, as a rule, printed but for its first few characters. One
    starting with `b` (`bc1`, `bcrt1`) or `1` is redacted, and so is a whole run of `b`
    addresses joined together;
  - after a Base58 one (`1`, `3`, `m`, `n`, `2`), both are printed.

  After a Kaspa address the second is redacted too, whatever its form -- except about one
  Base58 address in ten thousand behind a Kaspa address with the shorter, 61-character
  payload, which is printed in part.

**The second layer.** `httpx`, `httpcore` and `aiosqlite` are held at `WARNING` whatever
`PORTFOLIO_LOG_LEVEL` says. `httpx` logs every request's URL at `INFO`, and `aiosqlite` every
statement's parameters at `DEBUG` -- including the password hash whenever one is written,
which no rule above recognises. Turning the application up to `DEBUG` to investigate does not
turn them on.

## 19. The health detail: every source, and what to do about each

The Health page (`/health`) shows each section below, after *Backups*. Signed in, the same is
served by `GET /api/health/detail`:

```bash
curl -s -b "$COOKIE" <origin>/api/health/detail | jq 'del(.backup)'
```

```json
{
  "schedulers": [
    {"name": "balance-sync", "state": "ok", "last_tick_at": "2026-10-03T09:15:02.481210Z", "last_tick_succeeded": true},
    {"name": "price-refresh", "state": "ok", "last_tick_at": "2026-10-03T09:00:01.102934Z", "last_tick_succeeded": true},
    {"name": "price-backfill", "state": "ok", "last_tick_at": "2026-10-03T00:12:04.630918Z", "last_tick_succeeded": true},
    {"name": "balance-rebuild", "state": "ok", "last_tick_at": "2026-10-03T00:14:41.208310Z", "last_tick_succeeded": true},
    {"name": "backup", "state": "ok", "last_tick_at": "2026-10-03T03:00:00.912345Z", "last_tick_succeeded": true}
  ],
  "chains": {"state": "ok", "items": [
    {"chain_key": "bitcoin", "state": "ok", "last_success_at": "2026-10-03T09:15:31.004812Z", "last_error_kind": null},
    {"chain_key": "kaspa", "state": "failing", "last_success_at": "2026-10-03T08:45:12.774102Z", "last_error_kind": "rate_limited"}
  ]},
  "prices": {"state": "fresh", "latest_fetched_at": "2026-10-03T09:00:01.003712Z"}
}
```

`backup` is section 17. **Each source is what its last recorded attempt says**: the endpoint
calls no chain index and no price source -- the page refetches every minute -- so a source looks
healthy until its next attempt says otherwise. No setting is served: no interval, path, URL,
key, tolerance or age limit. The timers' fields are held in memory, so a restart clears them.

### `schedulers`: the five timers

Served in this order, the order the application starts them:

| Timer | What one tick does | Switch, and default interval | Section |
|---|---|---|---|
| `balance-sync` | reads every active wallet's balance | `PORTFOLIO_BALANCE_SYNC_ENABLED`, 15 minutes | 11 |
| `price-refresh` | fetches the current prices, and records today's `observed` price | `PORTFOLIO_PRICE_REFRESH_ENABLED`, 60 minutes | 10 |
| `price-backfill` | stores every daily close Kraken still serves, and older BTC closes from Coinbase | `PORTFOLIO_PRICE_BACKFILL_ENABLED`, 1440 minutes | 10 |
| `balance-rebuild` | rebuilds every active wallet's past daily balances from its transactions | `PORTFOLIO_BALANCE_REBUILD_ENABLED`, 1440 minutes | 11 |
| `backup` | copies the database, checks the copy and rotates | `PORTFOLIO_BACKUP_ENABLED`, 1440 minutes | 17 |

| `state` | Meaning | What to do |
|---|---|---|
| `ok` | Running and not late. | Nothing. |
| `late` | Running, and one of two things. A tick is in flight, and it started more than two intervals ago. Or no tick is in flight, and the last one finished more than two intervals ago -- or, before the first tick, the timer started more than two intervals ago. | Look for the timer's lines in the log; a sync stuck on a vendor that never answers shows as a tick in flight. Restarting the container starts the timer again. |
| `stopped` | The timer was built and its task is not running. | It should not happen while the application runs. Look for a traceback in the log and restart the container. |
| `disabled` | Its `PORTFOLIO_*_ENABLED` setting is false. | Nothing, unless it should be on: sections 10, 11 and 17 name the setting. |

"In flight" is read from the wall clock: the timer records when each tick starts and
finishes, and a tick is in flight when its start is later than the last finish. If the Pi's
clock is set back between a finish and the next start, that start can be recorded before the
finish. The tick is then measured from the finish, so it turns `late` later than it should,
by less than the clock moved -- never sooner. A clock set back far enough that the instant
measured from is in the future reads `ok` until the clock catches up.

`last_tick_succeeded` is `false` when the tick raised: the log has `scheduler_tick_failed` with
`scheduler` and the traceback. A balance sync that recorded a failed chain still finished its
tick; that failure is the `chains` section's. The same holds for the two price timers: a
refresh or a backfill that could not read a pair logs `price_refresh_incomplete` or
`price_backfill_incomplete` and still finishes its tick. Nothing in this endpoint reports a
backfill's pairs, so read those lines (section 10). Likewise a rebuild that left a wallet
incomplete logs `balance_rebuild_incomplete` and still finishes its tick (section 11).

For `price-backfill`, `balance-rebuild` and `backup`, two intervals are two days at the default: such a timer is
`late` when its last tick finished more than two days ago, or one has been in flight that long.

### `chains`: the balance sync per chain

One entry per chain that a wallet uses or that the latest finished balance run read.

| `state` | Meaning | What to do |
|---|---|---|
| `ok` | The newest finished run that read the chain read it. | Nothing. |
| `failing` | It failed. `last_error_kind` says how, in the vocabulary of the run log. | Section 11, *Reading the run log*: `GET /api/balances/runs` has the provider's message. `last_success_at` says how long it has been failing. |
| `never` | No finished run has read it: the wallet was registered after the last run, or no run has finished yet. | Wait for the next run, or trigger one (section 11). |

### `prices`

| `state` | Meaning | What to do |
|---|---|---|
| `fresh` | The newest price is at most an hour old. | Nothing. |
| `stale` | It is older: the refresh has stopped writing prices. | Check the price timer above and the `price_refresh_incomplete` lines; section 10. |
| `never` | No price has ever been stored. | Check that the price timer is not `disabled`; section 10. |

### `unavailable`: a section that could not be read

`chains` and `prices` can each be `unavailable`, with its items
empty and every other field `null`, while the other sections still answer. The Health page
says "Could not be read. The log says why." The log has `health_section_failed` with `section`
and `error_type`, the exception's class name:

```bash
~/portfolio-app/prod/compose.sh logs app | grep health_section_failed
```

A lasting one is a defect to report, with the `error_type` and the `request_id`.

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
| The value chart has gaps: days with no value | No price for those days. Run `backfill-prices` — section 10. A day still missing afterwards is a KAS day before 2024-11-19, a BTC day before 2015-07-20, or a KAS day older than Kraken's 720 days that was never inside the window while the backfill ran |
| KAS days before 2024-11-19 are always gaps, for KAS wallets and for the total | Kraken has no KAS price before its first day there. Working as intended: a gap, never a zero — section 10 |
| The value chart starts later than the range asks for | No wallet had a reading before that day, so there is nothing to value. Balances before the first snapshot are not rebuilt yet — section 11 |
| Today's point on the value chart moves during the day | Working as intended: today is valued at the latest hourly price, and becomes the day's close after the next backfill — sections 10 and 11 |
| `backfill-prices` exits 1 naming a pair and an error class | That pair was not backfilled; the other one was stored. The class says why — section 10, *Backfilling by hand* |
| The log has `price_backfill_incomplete` | A pair could not be read on that run. `failed` names it and its source, as `KAS/USD via kraken`; the next day's run tries again, or run `backfill-prices` now to see the error class — section 10 |
| Container refuses to start naming `PORTFOLIO_PRICE_BACKFILL_INTERVAL_MINUTES` | It is zero or negative. To stop the backfill, set `PORTFOLIO_PRICE_BACKFILL_ENABLED=false` — section 10 |
| The value chart starts at the day a wallet was added | Its past was not rebuilt. Run `rebuild-balances` and read the wallet's line — section 11, *Rebuilding past balances from the chain* |
| `rebuild-balances` exits 1 naming a wallet | That wallet was not rebuilt and kept the rows it had; the others were stored. The reason says why — section 11, *Rebuilding by hand* |
| The log has `balance_rebuild_incomplete` | A wallet's history did not prove complete, or its index failed, on that run. The lists name the wallet and the reason; the next day's run tries again — section 11 |
| Container refuses to start naming `PORTFOLIO_BALANCE_REBUILD_INTERVAL_MINUTES` | It is zero or negative. To stop the rebuild, set `PORTFOLIO_BALANCE_REBUILD_ENABLED=false` — section 11 |
| A wallet's value history returns 404 | The id is not one of the owner's wallets. Archived wallets do answer — section 11 |
| A portfolio total looks too small | Check the incomplete flag: a total omits any holding it could not price, on purpose — section 10 |
| Prices are all flagged stale | The last refresh is over an hour old. The price is still shown; it is the age that is being reported — section 10 |
| KAS/EUR is the only pair that ever fails | Kraken is the only key-free source for it. CoinGecko is the only fallback — section 10 |
| A pair reports `every_source_failed` while the vendor is plainly up | A vendor can be refused for what it *sent*: a price of zero or below, a non-finite number, or one too large or too small for the column. Failover treats that like any other refusal — section 10 |
| The dashboard says the last scheduled backup failed, `last_error_kind` is `database_error` | SQLite could not open or read the live database. Check that the container is healthy and the data volume is mounted; report it with the `error_type` from the `backup_failed` line — section 17 |
| `last_error_kind` is `integrity_failed` | A copy did not pass `PRAGMA integrity_check` and was not kept. It is read from the live database, so take one by hand to see the message. If the database is damaged, restore the newest good copy: the restore moves the damaged file aside first — section 17 |
| `last_error_kind` is `storage_error` | Writing to `/app/backups` failed: usually a full disk. Check `df -h` on the host. If the `backup_failed` line has `kept`, the copy was kept and the rotation after it failed — section 17 |
| The Health page shows the backups as unreadable, and the dashboard warns that it is not known whether backups are being kept (`unreadable`) | The backup directory cannot be listed: `/app/backups` is not a directory the container's user can read. Check the `backups` volume and `PORTFOLIO_BACKUP_DIR` — section 17 |
| The dashboard says the newest backup is old and scheduled backups have not completed since (`stale`) | The timer is off or not running. Check `PORTFOLIO_BACKUP_ENABLED` and the `backup_` lines in the log — section 17 |
| Container refuses to start naming `PORTFOLIO_BACKUP_INTERVAL_MINUTES`, `_KEEP_DAILY` or `_KEEP_WEEKLY` | The interval is below 60, `KEEP_DAILY` below 1, or `KEEP_WEEKLY` below 0. A shorter interval multiplies the copies kept on the database's disk. To stop backups, set `PORTFOLIO_BACKUP_ENABLED=false` — section 17 |
| `restore-backup` says a `-wal` file exists, so the database is open | Stop the application first. If it is already stopped, start it, wait until it is healthy, stop it, and restore again — section 17 |
| `restore-backup` says there is no database, but a `-journal` lies beside its path | A rollback journal from a database that is gone, most likely one an earlier restore moved aside. Move it beside that damaged file, or out of `/app/data`, then restore again. Nothing was changed — section 17 |
| `restore-backup` says the name is not a backup's, or there is no backup by that name | Copy the name from `list-backups` — section 17 |
| `restore-backup` says the copy is at a schema revision this version does not know | It was taken by a newer version. Deploy that version or a newer one, then restore — section 17 |
| `restore-backup` says the backup is not a self-contained copy | A `-wal` with content is beside it in `/app/backups`. Make it one file with `sqlite3` off the host, remove the `-wal`, and bring the file back, as section 17 shows. Nothing was changed |
| `restore-backup` says the backup did not pass its check | That copy is damaged and nothing was changed. Choose another — section 17 |
| `restore-backup` says the live database did not pass its own check and was moved aside | The live database opened but was damaged, so it was moved to `/app/data/portfolio.db.damaged-<stamp>`, with its `-journal` if it had one, instead of being copied. It holds the owner's data: keep it until the restore is checked, then delete it — section 17 |
| `restore-backup` refuses because the live database cannot be read | A permission or an I/O error, not damage, so it was not moved aside. Nothing was changed. Check the data volume, its owner and the storage device, then restore again — section 17 |
| `restore-backup` refuses because no safety copy of the live database could be taken | The backup directory is full or not writable, or the safety copy failed while the live database is sound. Nothing was changed. The message has the error underneath — section 17 |
| `restore-backup` says the database's directory does not exist | The data volume is not mounted, or `PORTFOLIO_DATABASE_URL` points elsewhere. Run it through `compose.sh` as section 17 shows |
| A restore failed and its message names the safety copy, or a moved-aside file | That file holds the database as it was before. Restore the safety copy — section 17 |
