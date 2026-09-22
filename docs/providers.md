# Adding a chain provider

What a new chain has to implement, what the shared machinery already does for it, and --
kept separate on purpose -- which facts about the two current vendors were confirmed
against their published documentation and which are still guesses.

Read `backend/src/portfolio/providers/base.py` alongside this. The docstrings there are the
reasoning; this is the checklist.

## The shape

A provider is any class that satisfies the `ChainProvider` protocol in
`portfolio.providers.base`. Four members, and nothing else is required:

| Member | Kind | What it must do |
|---|---|---|
| `capabilities` | property | Return a `ChainCapabilities`. Constant for the life of the instance. |
| `validate_address` | sync method | Decide whether a string is an address on this chain, **offline**. |
| `fetch_balances` | async method | Read the confirmed balance of each address, one result per request, in order. |
| `health` | async method | Say whether the upstream is answering, without naming any address. |

`ChainProvider` is a `typing.Protocol` and is **deliberately not `@runtime_checkable`**.
`isinstance` against a runtime-checkable protocol compares attribute names and nothing
else, so a class whose `fetch_balances` takes the wrong arguments, or is not a coroutine
function, passes it. The real check is `mypy --strict` deciding assignability: a provider
that does not conform fails the gate, and a test fake proves the same way, with a
module-level `_CONFORMS: ChainProvider = FakeChainProvider()`.

## The six steps

### 1. Add the chain key

`ChainKey` in `portfolio.domain.chains` is a `StrEnum` whose values are what
`wallets.chain_key` stores and what the `CHECK` constraint admits, so adding a member is
also a migration. `CHAIN_VALIDATORS` must gain an entry in the same change -- a test
asserts the mapping is total, so a chain added without an address codec fails the build
rather than raising a `KeyError` on the first address anyone enters.

### 2. Write the address codec in `domain/`, not here

Address validation lives in `portfolio.domain.addresses` because `domain` imports nothing
and therefore *cannot* make a network call. That is what makes "validating an address never
costs a round trip" a structural guarantee rather than a matter of discipline. A provider's
`validate_address` delegates to `domain.chains.validate_address` and adds nothing.

Transcribe the checksum constants from the published specification and name the source in a
comment beside each one. Do not reconstruct them from a sample address: an implementation
fitted to its own sample proves only that it agrees with itself, and carries a systematic
error into every address the product accepts.

### 3. Declare the capabilities honestly

```python
ChainCapabilities(chain_key=ChainKey.X, decimals=8, max_addresses_per_call=1)
```

`max_addresses_per_call` is an integer, not a `can_batch` boolean, because the boolean is
derivable from the integer and the integer is not derivable from the boolean.
`max_addresses_per_call=1` means the API takes one address per call. `can_batch` is a
derived property; never store a second copy of it.

Callers size their work with `chunk_addresses(addresses, capabilities)`, so the declaration
is consumed rather than decorative. Declaring a batch size the vendor does not support
produces a 400 on the first sync, which is the right kind of loud.

### 4. Build the result with `align_balances`

`fetch_balances` promises one result per requested address, same length, same order, each
carrying the address it is about, and **an address with no history is a zero rather than an
omission** -- that is what an unused address means on chain.

Do not implement that by being careful. Parse the response into a `{address: base_units}`
mapping and hand it to `align_balances(requested, found, decimals=...)`, which decides the
four cases:

| Case | Outcome |
|---|---|
| a requested address is missing from the response | zero balance |
| the response carries an address nobody requested | `ProviderResponseError` |
| a count that is not a whole number of base units | `ProviderResponseError` |
| a negative base-unit count | `ProviderResponseError` |
| the same address requested twice | `ValueError` |

The third row is not paranoia about types. `json.loads` returns whatever the vendor sent,
so a balance rendered as `1.0e8` arrives as a `float`, and `100000000.0 < 0` is `False`.
`align_balances` refuses it; the AST float ban cannot, because that float has no literal
and no `float` anywhere in your source.

The second row is the one worth internalising. A batch API answering about something we did
not ask about is a correlation bug, and dropping the entry silently would hide it behind a
total that still looks plausible.

If your chain can see its mempool, pass the deltas as well:

```python
align_balances(requested, found, decimals=8, pending=pending)
```

**`pending` is signed, and a missing entry is `None` rather than zero.** Those are two
separate decisions and both matter:

- Signed, because a mempool delta is not a balance. An outgoing payment spends a confirmed
  output and funds nothing, so it reads negative, and a "balances cannot be negative" guard
  applied here would reject the normal case. The negative refusal is on `confirmed` only.
- `None` rather than zero, because "nothing is pending" and "this chain cannot tell you"
  are different statements and only one of them is a balance. Omit the argument entirely if
  your chain has no mempool endpoint; leave an address out of the mapping if a particular
  response carried no figures. Both arrive as `pending=None`.

The unrequested-address refusal and the whole-number guard apply to `pending` too: a
response that correlates wrongly correlates wrongly in both halves, and a vendor that
renders one sum with a decimal point renders both that way.

**Balances are integer base units, never `Decimal` and never `float`.** Satoshis, sompi.
`AddressBalance.amount()` converts on demand through `domain.money.from_base_units`, which
is the only conversion rule in the system. `float` is banned in `providers/` and an AST
test in `backend/tests/security/test_no_float.py` enforces it.

### 5. Register the provider, and import its module

```python
@register_chain_provider(ChainKey.X)
class XProvider:
    def __init__(self, client: httpx.AsyncClient) -> None: ...
```

The decorator returns the class unchanged -- a class whose `__init__` takes the shared
client already *is* a `Callable[[httpx.AsyncClient], ChainProvider]`, so there is no factory
to write. Registering a key twice raises `DuplicateProviderError` at import, because a
copy-pasted decorator shadowing the provider above it presents as wrong balances rather
than as an error.

Then add exactly one line to `backend/src/portfolio/providers/chains/__init__.py`:

```python
from portfolio.providers.chains import x  # noqa: F401
```

There is no `pkgutil` auto-discovery, deliberately: it turns a provider that fails to
import into a chain that is merely absent, and the symptom then surfaces as a zero balance
rather than as a traceback. A test scans the package and asserts every module in it is
registered, so the forgotten line fails CI.

Asking for a chain nobody registered raises `UnknownChainError`, which carries the key and
the sorted list of what *is* registered.

### 6. Make every request through the shared client

`portfolio.providers.http.build_http_client()` returns an `httpx.AsyncClient` whose
transport already carries connect/read/write/pool timeouts, bounded retry with full jitter,
`Retry-After` handling and a per-host rate limiter. A provider takes the client in its
constructor and does not build its own.

The machinery is in a transport rather than in a helper function because **a helper has to
be remembered and a transport cannot be bypassed** -- the same argument the authentication
middleware makes in rule 8.

**`httpx` stops at your provider, not at the transport.** The transport does the
mechanics -- attempts, backoff, pacing, what may be logged -- and leaves failures in the
library's own shapes: an `httpx.TransportError` propagates, and a failing status comes back
as a response. Your provider translates both, which is the one `except` that keeps `httpx`
out of `services/`:

```python
try:
    response = await self._client.get(url, extensions={ENDPOINT_EXTENSION: ADDRESS_BALANCE})
except httpx.TransportError as error:
    # Nobody answered. Transient by assumption: keep the last known balance.
    raise ProviderUnavailableError("the chain did not answer") from error

status = response.status_code
if status == HTTPStatus.TOO_MANY_REQUESTS:
    # We asked too often. The remedy is a longer HostRateLimiter interval for this
    # vendor, not patience -- so it must not arrive as a generic outage.
    raise ProviderRateLimitedError("the chain is throttling us")
if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
    # The vendor is broken, not us. Retrying later is the right response.
    raise ProviderUnavailableError("the chain failed to answer")
if status >= HTTPStatus.BAD_REQUEST:
    # The vendor understood and refused: 400, 401, 403, 404. Retrying changes
    # nothing, and reporting it as an outage buries the only useful fact.
    raise ProviderResponseError(f"the chain refused the request with {status}")
```

**All three branches matter, and collapsing them is the mistake this hierarchy exists to
prevent.** Concretely: a self-hosted Esplora put behind an auth proxy starts returning 401.
Mapped to `ProviderUnavailableError`, every sync reports "chain temporarily unavailable"
forever, the operator waits for a vendor to recover that was never down, and nothing ever
says the credential is the problem. Mapped to `ProviderResponseError` it is a failure that
does not retry and does demand a person, which is what it is.

`ProviderRateLimitedError` is the one most easily forgotten, because a 429 is also
"try later" and a generic outage error is not obviously wrong. It is separate because the
remedy is different: a 429 that survived the transport's retries means our interval for
that vendor is too short, and that is a configuration change, not something waiting fixes.

Do not raise a `ProviderError` from a transport or from a shared helper. The transport
implements `httpx.AsyncBaseTransport` and owes that interface its own exception types, and
translating a connection failure there while a 503 stayed a response would hand every
caller one concept in two shapes. Deciding what a failure *means* for a balance is a
judgement about the vendor, which is what a provider is.

Never invent a response for a request that got no answer. "The chain said nothing" and "the
chain said something unhelpful" must not collapse into the same zero.

Retries apply to `GET` and `HEAD` by default. If a read is expressed as a `POST` -- Kaspa's
batch balance call is -- opt in explicitly with
`RetryPolicy(retry_methods=frozenset({"GET", "HEAD", "POST"}))`. That is one visible line
in a diff, which is the house rule for making something less safe.

## Logging: label the endpoint, never the path

Set an endpoint label on every request, and take it from `providers/http.py` rather than
writing the string at the call site:

```python
from portfolio.providers.http import ADDRESS_BALANCE, ENDPOINT_EXTENSION

response = await self._client.get(url, extensions={ENDPOINT_EXTENSION: ADDRESS_BALANCE})
```

The transport logs `"{scheme}://{host}/{label}"` and **never the path**. Both current
vendors put the address in the path, so a log line built from the URL would disclose
exactly what the wallet registry refuses to disclose.

**A label reaches the log only if it is a member of `ENDPOINT_LABELS`.** Anything else --
including a perfectly well-shaped string -- renders `"<unlabelled>"`. That is the completion
of a residual #6 recorded and #7 closed: the gate used to be a *pattern*, and a truncated
address is lower-case, alphanumeric and under 32 characters, so it matched the pattern and
reached the log. Membership in a frozen set cannot be satisfied by accident.

The set this release ships is exactly two: `address_balance`, for a balance read, and
`block_tip_height`, for the tip-height call a `health()` makes.

So a new endpoint is two lines, not one: the constant, and its name in `ENDPOINT_LABELS`.
The same shape as `PUBLIC_API_PATHS` in rule 8 -- the default says nothing, and saying more
about an endpoint is a visible edit to a named constant. The label must still match
`ENDPOINT_LABEL`'s pattern, which a test asserts over the set's contents.

`strip_query(url)` exists separately and removes the query string, the fragment and any
userinfo. It is the rule `CLAUDE.md` states, and it is what a future **exchange** provider
will use, because one exchange signs its requests in the query string. It is not sufficient
for a chain provider: reaching for it to log a chain request would meet the letter of the
rule and leak the address anyway.

Four further rules, none of them optional:

- **Never log a response body.** An error body from a public index can echo the request,
  which is to say the address.
- **Never log a URL you built yourself.** The transport is the only thing enforcing the log
  contract, and a provider with a log call of its own bypasses all of it.
- **Never turn the `httpx` logger back up.** `configure_logging` holds `httpx` and
  `httpcore` at WARNING, because `httpx.AsyncClient.send` logs every request at INFO with
  the full URL -- path and query string -- through the standard library, above the
  transport and outside structlog's redaction chain. Raising it to debug one provider call
  puts every wallet address and every exchange signature on stdout, which is exactly when
  someone is tailing the log. Use the transport's own `provider_request` line, or add a
  temporary field to it; both are address-safe by construction.
- **`ProviderHealth.detail` is for an operator**, so it carries "connect timeout" or
  "HTTP 503" and never an address, a URL or a body.

No address appears anywhere in this repository, including in this document. Test fixtures
use testnet addresses only -- `tb1`, `bcrt1`, `kaspatest:`, `tpub` -- and they live in
`backend/tests/address_vectors.py`.

## What a new chain has to decide, that the protocol does not decide for it

- **What "confirmed" means on this chain, and where the number comes from.** It is not
  always a field. Esplora's confirmed balance is a derivation,
  `chain_stats.funded_txo_sum - chain_stats.spent_txo_sum`; Kaspa's REST balance endpoint
  returns a single figure.
- **Whether mempool or unconfirmed value is even expressible.** `AddressBalance.pending` is
  `int | None` and the `None` is the answer for a chain that cannot see its mempool -- which
  is the Kaspa REST balance endpoint. Do not report zero to mean "I could not tell": zero is
  a balance and the absence of one is not.
- **Which network an address is on, if the vendor serves one network per instance.** Esplora
  does. That question belongs in `domain/` beside the codec -- `bitcoin_network_of` is the
  Bitcoin one -- and the provider refuses a wrong-network address offline, before it builds
  a URL. The alternative is trusting an undocumented error response, and the failure it
  hides is the expensive one: a balance read from the wrong chain is a number, not an error.
- **What the vendor's rate limit actually is.** See below: for both current vendors, nobody
  knows, and one of them enforces the limit it does not publish with a ban.

## Vendor facts, and the line between confirmed and assumed

The next person cannot tell a verified endpoint from a plausible one unless the difference
is written down, and will trust both equally.

### Confirmed against the published documentation, read on 2026-09-22

| | Bitcoin (Esplora) | Kaspa (kaspa-rest-server) |
|---|---|---|
| single address | `GET /address/:address` | `GET /addresses/{address}/balance` |
| batch | none documented | `POST /addresses/balances`, body `{"addresses": [...]}` |
| response | `chain_stats` / `mempool_stats`, each with `tx_count`, `funded_txo_count`, `funded_txo_sum`, `spent_txo_count`, `spent_txo_sum` | `[{"address": ..., "balance": ...}]` |
| units | satoshis | sompi, 1 KAS = 1e8 |
| health | `GET /blocks/tip/height`, "the height of the last block", a plain integer body | not checked |
| public instances | `https://blockstream.info/api` (also `/testnet/api`, `/signet/api`) and `https://mempool.space/api` (also `/testnet/api`) | not checked |

The Esplora rows are Blockstream's published `API.md` and mempool.space's REST
documentation; the two implement the same interface, which is what makes one a usable
fallback for the other.

Two consequences the design already reflects. The address is in the path on both, which is
why `request_target` logs a label instead of a path -- necessary, not defensive. And Esplora
documents no batch endpoint while Kaspa documents one, which is why
`max_addresses_per_call` is an integer.

### The rate limit is unpublished, and one vendor enforces it with a ban

Verified on 2026-09-22 and stated here because it is the risk that outlives a bad sync:

- **mempool.space's REST documentation states that exceeding its limits returns HTTP 429,
  and that repeatedly exceeding them may result in a ban. It publishes no numbers at all**,
  and points at enterprise sponsorship for higher limits.
- **Blockstream's `API.md` documents no rate limit either way.**

So the number in `DEFAULT_MIN_HOST_INTERVAL_MS` is not a measurement. It is one request per
second, chosen from the shape of that warning rather than from evidence, and the first real
evidence will be a 429 in a production log. A ban from a free public index is not fixed by
retrying and is not fixed by waiting; it is the failure mode the conservative floor is
buying insurance against.

### Not confirmed, because neither vendor documents it

- **What an instance answers for an address it considers invalid.** Neither documents an
  error body, or even a status, for that case. **Every status-to-error mapping in a provider
  must therefore be written against the status code alone** -- that is the part both vendors
  do have to get right -- and a check that can be made offline should be made offline rather
  than inferred from an undocumented refusal.
- **Any `Retry-After` behaviour.** The transport honours the header if it arrives, in both
  RFC 9110 forms, and clamps it to `RetryPolicy.max_backoff_ms`. Whether either vendor ever
  sends one is unknown.
- **Any cap on the Kaspa batch size.** The endpoint takes a list; the documentation does not
  say how long a list.
- **Pagination and retention.** Neither matters for a balance read. Both will matter for
  transaction history, and neither has been checked.
- **That `mempool_stats` is always present.** The Bitcoin provider reads its absence as
  `pending=None` rather than as a zero, which is the safe reading of a field the vendor
  never promised.

### A residual the address cannot close

An Esplora instance serves exactly one network, and `PORTFOLIO_BITCOIN_NETWORK` says which
one this deployment reads. The provider refuses an address from another network offline. Two
gaps remain, and neither is detectable from the address:

- **`tb1` is testnet3, testnet4 and signet alike.** An operator pointing the base URL at
  signet while holding testnet4 addresses gets confident, wrong answers.
- **A legacy base58 address on regtest is indistinguishable from testnet**, because Bitcoin
  Core gives both the same version bytes, `0x6F` and `0xC4`. `bitcoin_network_of` answers
  `TESTNET` for it, so a regtest-configured provider refuses it; `bcrt1` is the spelling
  that reads as regtest.

The first is a wrong number and the second is a refusal, which is the direction this is
allowed to be wrong in.

The defaults below are conservative guesses, chosen so that being wrong costs seconds per
sync rather than getting us refused by a free public index. They are a policy object and a
capability integer rather than literals in the request path, so correcting them with a
measurement is a change to a value.

| Setting | Default | Basis |
|---|---|---|
| `DEFAULT_MIN_HOST_INTERVAL_MS` | 1000 | guess: 1 request/second to one host, from mempool.space's unpublished limit and its ban warning |
| `RetryPolicy.max_attempts` | 3 | guess |
| `RetryPolicy.base_backoff_ms` | 250 | guess |
| `RetryPolicy.max_backoff_ms` | 30_000 | guess; the ceiling exists for a server that asks for a day |
| `CONNECT_TIMEOUT_MS` | 5_000 | guess |
| `READ_TIMEOUT_MS` | 20_000 | guess; a batch read legitimately takes longer than a handshake |
| `WRITE_TIMEOUT_MS` | 10_000 | guess |
| `POOL_TIMEOUT_MS` | 5_000 | guess |

Those are module constants, and promoting one to a setting is a change an operator's
measurement should drive. The values an operator *does* set are these, and they are settings
because the answer differs per deployment rather than because a number was uncertain:

| Variable | Default | What it is |
|---|---|---|
| `PORTFOLIO_BITCOIN_ESPLORA_URL` | `https://mempool.space/api` | the instance tried first |
| `PORTFOLIO_BITCOIN_ESPLORA_FALLBACK_URL` | `https://blockstream.info/api` | tried when the first fails; blank means one instance only |
| `PORTFOLIO_BITCOIN_NETWORK` | `mainnet` | `mainnet`, `testnet` or `regtest`; must match what the URLs above serve |

Two scalars rather than one list, because pydantic-settings parses a `list[str]` out of the
environment as JSON and a self-hoster clearing one URL should not have to learn a syntax.
`docs/operations.md` carries the same table for whoever is editing `secrets.env`.

**A base URL is validated at startup, and a new provider's should be too.** `config`'s
`provider_url_violation` refuses a URL with no scheme, no host, or a scheme other than
`http`/`https`, because none of those can be requested and the failure does not arrive as
something a provider can translate. Measured on httpx 0.28.1: `mempool.space/api`,
`not a url` and `http://` all reach `client.get` as a bare `builtins.ValueError` from
inside `urllib` — past `except httpx.TransportError`, which is where `httpx` is supposed to
stop, and past a `health()` whose contract is that it never raises. A mistyped scheme like
`htp://` is quieter and worse: it *is* an `httpx.UnsupportedProtocol`, so it is caught and
reported as an unavailable chain on every sync forever while nothing mentions the typo.

The validator parses with `httpx.URL` rather than `urllib.parse` on purpose. The question
is not "is this a URL" but "will the client this is handed to accept it", and a check that
answers a different question is how a validator passes while the thing it guards fails.

Do not invent an endpoint path because a third-party wrapper uses it. Verify against the
vendor's own documentation, and record here what you confirmed and what you assumed, in
those words, **with the date you read it** -- an unverified fact and a fact verified two
years ago are different things, and only one of them says so.

## Not done yet, and who owns it

- **Lifespan wiring.** `build_http_client()` is process-wide by construction -- the rate
  limiter's state lives on the transport, which lives on the client, so two clients would
  each keep their own idea of the interval and it would silently become half of what it
  says. Nothing calls a provider yet, so nothing builds one at startup; creating and
  closing it in `main.py` today would be an unused connection pool held open for the life
  of the application. **#10 owns building it in the lifespan and closing it there.**
- **Tuning settings.** Every number in the first table above is still a module constant.
  Promoting one to a `PORTFOLIO_PROVIDER_*` setting is a change an operator's measurement
  should drive, not a guess made before anything has ever made a request.
- **A per-address cache.** Not the provider's: an instance is built per `registry.create()`
  call, so a cache on it would be dead on arrival, and a cached balance looks exactly like a
  read one -- which is the failure `ProviderUnavailableError` exists to prevent. **#10 owns
  it**, because the scheduler knows how often a read may repeat and the snapshot table is
  where a previous reading already lives.
- **The source-walk test over `providers/`.** `backend/tests/security/test_address_logging.py`
  walks the wallet modules and fails on a log call that could carry an address. The Bitcoin
  provider has no log call at all -- deliberately, since the transport's contract is the only
  one that is enforced rather than remembered -- so the walk is worth extending the day a
  provider needs one.
- **Network-aware address registration.** A wrong-network address is refused by the
  *provider*, at read time, not when the wallet is registered. Making registration
  network-aware changes #5's contract and needs a story for rows that already exist.
