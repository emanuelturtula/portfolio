# Operations

Day-two tasks on the running instance: creating the account, tuning the password hash to the
hardware, changing the password, understanding when a session ends, pointing the application
at the chain index it reads balances from, refreshing the prices that turn a balance into
a value, connecting the Bitget and BingX accounts whose trades say what each asset cost,
keeping the import of those trades running, reading the cost-basis snapshot built from
them, checking that history against the balances actually held, backing the database up and
restoring it, reading the logs, and reading how every source stands.

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
what the venue keeps, 90 days at Bitget and at BingX -- newest first, in windows the
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
  nothing twice. Only a clock behind by more than the venue's retention (90 days at each
  venue) loses history, and `history_truncated` then says so.

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
you asked for everything, and Bitget keeps 90 days. At BingX this application reads **90
days**, measured on 2026-10-05: a window older than that is not refused, it is answered with
the account's newest fills and both time bounds ignored, and the sync refuses such a page
(`N fill(s) have an executed_at outside the requested window`). BingX's API documentation
says 7 days, which it does not enforce, and its support centre says a year, about exporting
trade history from the website, which the API does not follow. The same article says some
regions and risk-controlled accounts get 30 days. Trades older than 90 days have to come
from somewhere else, such as a one-time import.

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
- **A wallet** is compared when its chain did not fail in the last balance run that finished,
  and its latest reading is at most 24 hours old. When the chain did fail in that run, the
  wallet is compared only if a later run has already read it, and that reading is at most
  24 hours old.

The limit is served as `max_reading_age_hours` and is not configurable. Both syncs run every
fifteen minutes by default, so a reading only reaches it when a source has stopped being
read. The age is measured when the request is served.

**A wallet whose chain failed is left out at once**, without waiting for the limit (spec
`docs/specs/028-wallet-chain-failed.md`). The run that decides is the newest one in
`GET /api/balances/runs` whose `status` is `success`, `partial` or `failed` (section 11). When
that run has the wallet's chain as `failed`, the wallet's last reading is not compared,
however recent it is, unless a later run has already stored it. A `running` or `interrupted`
run records no chain, so it decides nothing and the run before it still stands. A reading stored by a run later than the one that
decides, which is a run still in progress or one interrupted after it read the chain, is kept
and compared. A chain with no entry in that run did not fail, and neither has any chain
before the first run finishes.

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

`wallets` has four counts that add up to the active wallets. `compared` wallets are in the
comparison. A wallet that is left out is counted under the first of these that applies:

| Count | Means | What to do |
|---|---|---|
| `chain_failed` | The last balance run that finished could not read the wallet's chain, and no later run has read the wallet. Its last reading is **not** compared. A wallet on that chain with no reading at all is counted here, and not under `unread`. | `failed_chains` names the chain. Read that chain's `error_kind` in `GET /api/balances/runs`: section 11. The wallet is compared again once a run reads the chain. If no run follows, check the balance timer (`PORTFOLIO_BALANCE_SYNC_ENABLED`): with it off no run comes, and the wallet stays left out. |
| `unread` | No balance run has read the wallet yet (section 11). | Nothing if the wallet was just added: the next run reads it. |
| `stale` | The reading is more than 24 hours old, and the last run that finished does not have the chain as `failed`. No run has read the wallet for a day. | Check `PORTFOLIO_BALANCE_SYNC_ENABLED`, and whether the runs are all `interrupted` (section 11). |

`failed_chains` lists the chains behind `chain_failed`, sorted by `chain_key`. Each entry has
the `chain_key` and `wallets`, the number of wallets on that chain that were left out. Only a
chain with at least one wallet left out is listed, so `wallets` is never zero, the entries add
up to `chain_failed`, and the list is empty when no wallet is left out this way. An entry does
not say why the chain failed: the run log does. The dashboard shows one notice per chain.
`oldest_observed_at` is the oldest reading among the compared wallets.

A source that is left out adds nothing to the held side. That can hide a difference, and
cannot produce one. What can still produce a false one is coins moved between two current
readings, taken at different times by two different syncs: they are counted twice, or not at
all, until both sources have been read again. Those readings are **minutes** apart while both
syncs are running, and **up to 24 hours** apart when a source has stopped being read without
a recorded failure. Its last reading then stays in the comparison until it reaches the limit,
and nothing names the source until then. These are the residuals:

- a wallet, when the balance timer is switched off (`PORTFOLIO_BALANCE_SYNC_ENABLED=false`),
  or when no balance run finishes: the last run that finished is then an old one, and says
  nothing about what happened since;
- a venue whose credentials were removed after a read;
- a venue, when the exchange timer is switched off (`PORTFOLIO_EXCHANGE_SYNC_ENABLED=false`);
- the double failure logged as `exchange_balances_failure_not_recorded` (below).

The chain rule leaves two windows of its own, each bounded by one balance interval
(`PORTFOLIO_BALANCE_SYNC_INTERVAL_MINUTES`, fifteen minutes by default) while the balance
timer runs:

- A chain that starts failing **between** two balance runs is not known to have failed until
  the next run finishes, so a wallet on it stays in the comparison for up to one balance
  interval.
- A run does not attempt a chain with no active wallet, so it has no entry for that chain, and
  a chain with no entry did not fail. When a chain's only wallets were archived while the
  last run ran and were restored afterwards, their previous readings are compared, while
  they are under 24 hours old, even if the run before has the chain as `failed`. That lasts
  until the next run finishes.

The dashboard shows each reading's age. Treat a `history_short` as a prompt to look.

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

## 17. Backups: where they are, how they stand, and restoring one

The application copies its own database on a timer, checks each copy, and keeps a rotating
set of them. Why that matters, and why it is not the deployment's backup in `prod/backup/`,
is in `docs/deployment.md`, *Scheduled backups*. The contract is spec
`docs/specs/029-sqlite-backups.md`.

**Every copy holds the owner's complete financial data, as the live database does**: every
imported trade, every wallet address, every manual adjustment, and the owner's account with
its password hash. Treat a copy you take off the host as you would the database.

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
     accounting_lots: 12
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
   and look at the wallets, the adjustments and the positions. The Health page should show the
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

- **every credential the application holds**: the bootstrap password, the CoinGecko key, the
  three Bitget variables and the two BingX ones -- every `SecretStr` setting, found by type, so
  one added later is covered too. Wherever it appears when it is 8 characters or longer, and
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
  `https://host/path?[REDACTED]`. One exchange signs its requests in the query string.

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
    {"name": "exchange-sync", "state": "disabled", "last_tick_at": null, "last_tick_succeeded": null},
    {"name": "backup", "state": "ok", "last_tick_at": "2026-10-03T03:00:00.912345Z", "last_tick_succeeded": true}
  ],
  "chains": {"state": "ok", "items": [
    {"chain_key": "bitcoin", "state": "ok", "last_success_at": "2026-10-03T09:15:31.004812Z", "last_error_kind": null},
    {"chain_key": "kaspa", "state": "failing", "last_success_at": "2026-10-03T08:45:12.774102Z", "last_error_kind": "rate_limited"}
  ]},
  "exchanges": {"state": "ok", "items": [
    {"exchange_key": "bitget", "sync_state": "ok", "last_synced_at": "2026-10-03T09:00:44.310275Z", "balances_state": "ok", "balances_read_at": "2026-10-03T09:00:45.120934Z"}
  ]},
  "prices": {"state": "fresh", "latest_fetched_at": "2026-10-03T09:00:01.003712Z"},
  "reconciliation": {"state": "match", "computed_at": "2026-10-03T09:00:46.551203Z", "assets_compared": 2, "assets_mismatched": 0, "sources_not_compared": 0}
}
```

`backup` is section 17. **Each source is what its last recorded attempt says**: the endpoint
calls no chain index and no exchange -- the page refetches every minute -- so a source looks
healthy until its next attempt says otherwise. No setting is served: no interval, path, URL,
key, tolerance or age limit. The timers' fields are held in memory, so a restart clears them.

### `schedulers`: the four timers

| `state` | Meaning | What to do |
|---|---|---|
| `ok` | Running and not late. | Nothing. |
| `late` | Running, and one of two things. A tick is in flight, and it started more than two intervals ago. Or no tick is in flight, and the last one finished more than two intervals ago -- or, before the first tick, the timer started more than two intervals ago. | Look for the timer's lines in the log; a sync stuck on a vendor that never answers shows as a tick in flight. Restarting the container starts the timer again. |
| `stopped` | The timer was built and its task is not running. | It should not happen while the application runs. Look for a traceback in the log and restart the container. |
| `disabled` | Its `PORTFOLIO_*_ENABLED` setting is false. The exchange timer is also `disabled` when no exchange has credentials. | Nothing, unless it should be on: sections 10, 11, 13 and 17 name the setting. |

"In flight" is read from the wall clock: the timer records when each tick starts and
finishes, and a tick is in flight when its start is later than the last finish. If the Pi's
clock is set back between a finish and the next start, that start can be recorded before the
finish. The tick is then measured from the finish, so it turns `late` later than it should,
by less than the clock moved -- never sooner. A clock set back far enough that the instant
measured from is in the future reads `ok` until the clock catches up.

`last_tick_succeeded` is `false` when the tick raised: the log has `scheduler_tick_failed` with
`scheduler` and the traceback. A balance sync that recorded a failed chain still finished its
tick; that failure is the `chains` section's.

### `chains`: the balance sync per chain

One entry per chain that a wallet uses or that the latest finished balance run read.

| `state` | Meaning | What to do |
|---|---|---|
| `ok` | The newest finished run that read the chain read it. | Nothing. |
| `failing` | It failed. `last_error_kind` says how, in the vocabulary of the run log. | Section 11, *Reading the run log*: `GET /api/balances/runs` has the provider's message. `last_success_at` says how long it has been failing. |
| `never` | No finished run has read it: the wallet was registered after the last run, or no run has finished yet. | Wait for the next run, or trigger one (section 11). |

### `exchanges`: one entry per account

`sync_state` is the fill sync's status -- `ok`, `error`, `auth_failed` or `never_synced`, as
section 13 describes -- and `last_synced_at` when a run last left the account with nothing
pending.

| `balances_state` | Meaning | What to do |
|---|---|---|
| `ok` | The last balance read succeeded, at `balances_read_at`. | Nothing. |
| `failing` | The last balance read failed. | Section 16, *What to do about a `balances_error`*. |
| `never` | No balance read has been made: balances are read only after a successful fill sync. | Look at `sync_state` first. |

### `prices`

| `state` | Meaning | What to do |
|---|---|---|
| `fresh` | The newest price is at most an hour old. | Nothing. |
| `stale` | It is older: the refresh has stopped writing prices. | Check the price timer above and the `price_refresh_incomplete` lines; section 10. |
| `never` | No price has ever been stored. | Check that the price timer is not `disabled`; section 10. |

### `reconciliation`: the holdings check, in short

A state and three counts: `assets_compared`, `assets_mismatched` and `sources_not_compared`
(exchange accounts and wallets left out of the comparison). No quantity is served; the
holdings check on the dashboard, and `GET /api/accounting/reconciliation`, have the detail.

| `state` | Meaning | What to do |
|---|---|---|
| `match` | Every asset matches and every source was compared. | Nothing. |
| `mismatch` | At least one asset's history and balances disagree beyond the tolerance. | Section 16: a `history_short` asset usually means a buy the history does not hold. |
| `incomplete` | Every compared asset matches, but at least one source was left out: an account whose reading is not current, or a wallet that is stale, unread or on a chain that failed. | Section 16, *Which readings are compared*. |
| `not_computed` | No cost-basis snapshot has been computed yet. | Wait for the startup recompute, or look at section 15. |

### `unavailable`: a section that could not be read

`chains`, `exchanges`, `prices` and `reconciliation` can each be `unavailable`, with its items
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
| Container refuses to start, log names `PORTFOLIO_EXCHANGE_HISTORY_START` | The date is after today's date in UTC — section 13 |
| An exchange account stays `auth_failed` after fixing the key | Scheduled runs skip it: recreate the container, then trigger a sync by hand, and again if the first POST says `"joined": true` — section 13 |
| Exchange syncs fail `unavailable` with venue code `40008` | The host clock is off by more than 30 seconds — section 13, "The host clock must be synchronised" |
| An exchange account fails with `conflict` on every run | A stored fill changed under the same id; it needs a person — section 13 |
| The holdings check says a venue's balances could not be read, while the account is `ok` | A failed balance read never changes the account's status. Read `balances_error` — section 16 |
| A venue's `balances_error` stays `auth` or `insufficient_scope` after fixing the key | Scheduled runs do not ask again: recreate the container, then trigger a sync by hand — section 16 |
| The holdings check shows the history above the balances for an asset | Not a finding: only the spot account is read, so coins in Earn, futures or an unregistered wallet are not counted. A sale the import did not see looks the same, and the check cannot tell them apart — section 16 |
| The holdings check leaves a venue or a wallet out although nothing failed today | Its reading is more than 24 hours old, or the venue's fill sync is not `ok`. Read `not_compared_reason` and `wallets.stale` — section 16 |
| The holdings check says the last balance sync that finished could not read a chain, and leaves its wallets out | The last balance run that finished has that chain as `failed`. Read `wallets.failed_chains`, then that chain's `error_kind` in `/api/balances/runs`. The wallets are compared again once a run reads the chain; if none follows, check `PORTFOLIO_BALANCE_SYNC_ENABLED` — sections 11 and 16 |
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
