# Adding a provider

What a new chain has to implement, what a new price source has to implement, what the
shared machinery already does for both, and -- kept separate on purpose -- which facts
about each vendor were confirmed against its published documentation, which were measured
against the live service, and which are still guesses.

Two kinds of provider live under `backend/src/portfolio/providers/`. A **chain provider**
reads balances from addresses (`providers/chains/`); a **price source** reads what an asset
costs (`providers/prices/`). Everything down to "Vendor facts" is about the first kind; the
"Price sources" section near the end is about the second, and says where the two differ.

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

**If your chain has more than one instance, that mapping decides what to raise, not when to
give up -- and you do not write either.** `providers/endpoints.py` owns both:

```python
self._instances = EndpointSet.configured(
    client,
    ((PRIMARY, settings.x_url), (FALLBACK, settings.x_fallback_url)),
    vendor="X",
)
body, start = await self._instances.read(path, LABEL, start)
```

`EndpointSet` drops blank URLs, recognises two spellings of one URL as one instance, tries
each in turn, classifies the **last** failure by the mapping above, and chains the exception
to that same failure's cause. `read` returns the index that answered; pass it back as
`start` and failover is sticky within your call and resets between calls.

It tries the next instance on *every* non-200. The rule it started with — stop on any 4xx,
because the second instance runs the same software and would refuse identically — sounds
right and is false for every refusal scoped to an instance rather than to a request: a ban
that is spelled 403, an auth proxy returning 401, a base URL missing its `/api` path
returning 404. Those are precisely the cases a second instance exists for, and stopping made
the fallback unreachable in exactly them. The misconfiguration is not lost by moving on; it
surfaces in `health`, which probes each instance in turn — that loop stays in your provider,
because only you know how to read your vendor's health document.

A 200 whose body will not parse is the exception and still stops. That decision is in your
provider rather than in `EndpointSet`, and deliberately: a non-200 is one instance declining
to answer, while a 200 we cannot read is a statement about our parser or the vendor's
schema, and only the provider knows what a body means. A second opinion would either repeat
it or hide it behind a number.

**This is why the third chain is a file rather than a refactor.** #7 wrote that loop inside
`chains/bitcoin.py` and review corrected it there; #8 extracted it rather than copying it,
because two copies of a rule that review has already corrected once is how the correction
gets un-made. `tests/providers/chains/test_bitcoin.py` passing untouched is what proved the
extraction changed no behaviour.

Do not raise a `ProviderError` from a transport or from a shared helper. The transport
implements `httpx.AsyncBaseTransport` and owes that interface its own exception types, and
translating a connection failure there while a 503 stayed a response would hand every
caller one concept in two shapes. Deciding what a failure *means* for a balance is a
judgement about the vendor, which is what a provider is.

Never invent a response for a request that got no answer. "The chain said nothing" and "the
chain said something unhelpful" must not collapse into the same zero.

### A read expressed as a `POST`, retried without making every `POST` retryable

Retries apply to `GET` and `HEAD` by default. If a read is expressed as a `POST` -- Kaspa's
batch balance call is -- opt in **per request**:

```python
from portfolio.providers.http import ADDRESS_BALANCES, ENDPOINT_EXTENSION, IDEMPOTENT_EXTENSION

await client.post(
    url,
    json=payload,
    extensions={ENDPOINT_EXTENSION: ADDRESS_BALANCES, IDEMPOTENT_EXTENSION: True},
)
```

**Do not widen `RetryPolicy.retry_methods` to include `POST`.** #6 proposed exactly that and
it is wrong: the policy lives on the transport, the transport is process-wide by
construction, and widening it would make *every* future `POST` retryable -- including an
exchange request that places an order, where a retry after a transport error can double a
trade. One provider's convenience would silently become another's duplicate fill. The
extension is deny-by-default and is checked with `is True`, so a stray truthy value cannot
opt a request in.

**Pass the body as `json=`, never as a stream.** `httpx` consumes a request stream on the
first attempt, so a retried streamed body replays as empty: the server answers about no
addresses at all and the sync reports zeros rather than an error. That failure passes every
assertion about exception types and is only visible in the *bytes of the second request*,
which is what `tests/providers/test_http.py` asserts.

In practice a provider does not write that call at all -- `EndpointSet.post` does. **But it
takes `idempotent: bool` as a required keyword with no default, and that is not ceremony.**

A shared helper that set `IDEMPOTENT_EXTENSION: True` for every caller would reintroduce
precisely the hazard the paragraph above rejects, one layer up and more quietly. Deny by
default would hold at the transport and be undone by the only `POST` helper anyone uses --
and `EndpointSet` is exactly what an exchange provider with a primary and a fallback will
reach for, at which point the failover loop double-submits an order after a transport error.

A default of `False` would not have been better: it would put the decision back in the
shared module, silently, where the call site cannot see it. Required and unspelled-able is
what keeps the answer next to the request it describes:

```python
await self._instances.post(BALANCES_PATH, ADDRESS_BALANCES, start, json=body, idempotent=True)
```

Say `True` only for a request that changes nothing at the vendor. Never for anything that
places, cancels or transfers.

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

The set this release ships is exactly four: `address_balance` for a single balance read,
`address_balances` for Kaspa's batch read, `block_tip_height` for the tip-height call
Esplora's `health()` makes, and `node_health` for the health document Kaspa's reads.

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
- **Which network an address is on, if the vendor serves one network per instance.** Both
  current vendors do. That question belongs in `domain/` beside the codec --
  `bitcoin_network_of` and `kaspa_network_of` are the two -- and the provider refuses a
  wrong-network address offline, before it builds a URL. The alternative is trusting an
  undocumented error response, and the failure it hides is the expensive one: a balance read
  from the wrong chain is a number, not an error.

  **How exact that check can be is a property of the chain, not of the effort put in.**
  `bitcoin_network_of` collapses testnet3, testnet4 and signet into one answer and cannot
  tell a legacy regtest address from a testnet one, so it records a residual nothing can
  close. `kaspa_network_of` has no such collapse: the three prefixes are folded into the
  40-bit checksum, so the same payload checksums differently on each network. Say which of
  the two your chain is; a reader comparing two modules will otherwise assume they are
  copies.
- **What "healthy" means for this vendor, which is rarely "it answered".** Esplora has only
  a tip height, so its provider parses it -- an instance serving an HTML holding page with a
  200 is unhealthy rather than healthy-and-wrong. Kaspa publishes a health document, so its
  provider reads it: a synced database *and* a node that is both synced and UTXO-indexed,
  because a synced node without the UTXO index passes a ping and cannot answer one balance.
  A reachable-but-unsynced index is the failure this project keeps meeting in new clothes --
  it returns balances that are stale and well-formed, and a wrong number is worse than an
  error.

  Whatever the document carries, **`ProviderHealth.detail` must not leak the vendor's
  topology.** Kaspa's health body names each backing node in `kaspadHost`; the provider's
  parser drops that field rather than the provider remembering not to render it, because a
  field that was never carried cannot be leaked by the next person writing a helpful
  message.
- **What the vendor's rate limit actually is.** See below: for all three current instances,
  nobody knows. One vendor enforces the limit it does not publish with a ban, and one sits
  behind a CDN and sends no rate-limit headers at all.

## Vendor facts, and the line between confirmed and assumed

The next person cannot tell a verified endpoint from a plausible one unless the difference
is written down, and will trust both equally.

### Confirmed against the published documentation, read on 2026-09-22

| | Bitcoin (Esplora) | Kaspa (kaspa-rest-server) |
|---|---|---|
| single address | `GET /address/:address` | `GET /addresses/{address}/balance` |
| batch | none documented | `POST /addresses/balances`, body `{"addresses": [...]}` |
| response | `chain_stats` / `mempool_stats`, each with `tx_count`, `funded_txo_count`, `funded_txo_sum`, `spent_txo_count`, `spent_txo_sum` | `{"address": ..., "balance": ...}`, and an **array** of those for the batch |
| units | satoshis | sompi, 1 KAS = 1e8 |
| documented errors | none, for any case | **422** on the balance endpoint; **503** on health |
| health | `GET /blocks/tip/height`, "the height of the last block", a plain integer body | `GET /info/health` -> `{"kaspadServers": [{"kaspadHost", "serverVersion", "isUtxoIndexed", "isSynced", "p2pId", "blueScore"}], "database": {"isSynced", "blueScore", "blueScoreDiff", "acceptedTxBlockTime", "acceptedTxBlockTimeDiff"}}`, documented as 503 when the database lags by around ten minutes or no node is synced |
| public instances | `https://blockstream.info/api` (also `/testnet/api`, `/signet/api`) and `https://mempool.space/api` (also `/testnet/api`) | one operator, and the document declares no `servers` block, so the base URL is ours to configure |

The Esplora rows are Blockstream's published `API.md` and mempool.space's REST
documentation; the two implement the same interface, which is what makes one a usable
fallback for the other. The Kaspa rows are the live OpenAPI document, read the same day.

Two consequences the design already reflects. The address is in the path on both, which is
why `request_target` logs a label instead of a path -- necessary, not defensive. And Esplora
documents no batch endpoint while Kaspa documents one, which is why
`max_addresses_per_call` is an integer.

### Measured against the live Kaspa service on 2026-09-23

The document is silent on all of these, so they were measured rather than guessed. Two of
them changed what was built; two changed nothing and are recorded because they are invisible
in the document.

- **No `ratelimit-*` or `x-ratelimit-*` header on any response**, from either the balance
  endpoint or `/info/health`. What the responses do carry is `Server: cloudflare`,
  `cf-cache-status` and `CF-RAY`, so the service sits behind a CDN and the realistic
  throttle is Cloudflare's: a 429 with `Retry-After`, which the transport has honoured since
  #6, or a 403 for a block, which the failover moves on from. `parse_rate_limit` is built
  anyway, because the criterion says "when present" and a self-hosted instance without a CDN
  may well send them -- **and it is therefore unexercised production code that looks
  tested**, which is recorded here and in its own docstring rather than left to be assumed.
- **`Cache-Control: public, max-age=8`** on the balance response *and on the error*, in
  front of a Cloudflare cache. A balance read can be served from an edge cache rather than
  from the index. Eight seconds is immaterial to a sync scheduled in minutes, so this
  changes no code; it is written down so that whoever next asks "why did two reads a second
  apart return the same number" finds the answer instead of rediscovering it against a CDN.
  It is also why the single-address parser checks the echoed `address`: a cache in front of
  an endpoint is exactly what answers about somebody else.
- **The public instance is mainnet-only by construction.** A `kaspatest:` address is
  answered 422, quoting the server's own rule: the path must match
  `^kaspa:[a-z0-9]{61,63}$`. The prefix is a literal in that regex, so one instance serves
  one network -- which makes a configured-network refusal a description of something real
  rather than something imagined.
- **The vendor does not check the checksum.** That regex is prefix, character set and
  length and nothing else, so a *mistyped* mainnet address that still matches it is accepted
  and answered with a balance -- `0`, for a wallet that does not exist. That is precisely
  the failure the address codecs were built to prevent: a typo that reports an empty wallet
  forever and looks no different from an empty one. **Our offline validation is strictly
  stronger than the vendor's**, and since this measurement that is a fact rather than a
  preference.

### The Kaspa batch ceiling is a guess, and the OpenAPI document is where it is missing

`MAX_ADDRESSES_PER_CALL` is **64**. The document declares `addresses` as an array of strings
with **no `maxItems`**, and the operation description names no ceiling; confirmed against
the live document on 2026-09-22. Sixty-four is large enough that any realistic portfolio is
one request and small enough that a request body stays a few kilobytes, and that is the
whole of the justification.

`chunk_addresses` sizes every call from the declaration, so correcting it is a change to a
constant. **The first real evidence will be a refused batch in production**, which is why
the refusal names *the size of the batch* and never its contents: the size is the number an
operator can act on, and the contents are the owner's holdings.

**A refusal does not imply a size problem, and saying so was a real defect.** The provider
first attached "lower `max_addresses_per_call`" to every `ProviderResponseError` out of the
failover loop -- which, by `EndpointSet._failure_for`, is *everything that is not a 429 and
not a 5xx*. The realistic refusal for this vendor is none of those things: it is the 403 a
CDN returns for a blocked host, and the spec predicts it by name. An operator behind one
would have been told, by the error and by `docs/operations.md` alike, to correct a constant
that was never wrong.

So the advice is attached only for `BATCH_TOO_LARGE_STATUSES` -- 413, and the 422 this
vendor documents, which is what its framework returns for request-body validation. Every
other refusal still names the size, because how many addresses were in flight is real
context, and offers no theory about the cause. **414 is deliberately not in that set**: the
batch travels in a `POST` body to a constant path, so no batch size can lengthen the URI,
and an entry that cannot fire is a branch no reader can check.

The general rule a new provider should take from this: **a helpful remedy attached to the
wrong condition is worse than no remedy**, because it spends the one hour somebody had.
Attach advice to the statuses that can actually produce the condition, and let the rest
carry facts only. `ProviderError.status` exists so that this can be decided on the status
rather than by reading a message written for an operator.

There is deliberately **no fallback from a refused batch to single reads**. A batch refused
for being too large is a configured value to correct rather than a path to code around --
and a silent fallback would turn one call into sixty-four at a vendor whose rate limit is
unpublished.

### The trap: the Kaspa OpenAPI document's examples are real mainnet addresses

Every example value in that document is a live mainnet address. **Do not copy one into a
test, a docstring, a comment or this file.** Rule 3 forbids a wallet address in this
repository at all, mainnet or not, and the temptation is at its strongest exactly here,
because the document hands you a string that is guaranteed to parse.

Test fixtures use `kaspatest:` vectors from published sources -- rusty-kaspa's own case
table and the Aspectron documentation -- and they live in `backend/tests/address_vectors.py`.
`tests/security/test_address_logging.py::test_fixtures_contain_no_mainnet_address` scans
every test file and this document for a mainnet address, which is what makes that a control
rather than a request.

The second half of the trap is subtler: the vendor does not verify checksums, so a mainnet
example that has been *retyped* by hand still gets a 200 and a balance of zero. A test built
on one would pass, look like it exercised the happy path, and prove nothing at all.

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
  sends one is unknown. For Kaspa it is the *likely* form a throttle takes, since the
  measurement above found a CDN in front and no `ratelimit-*` headers at all.
- **Any cap on the Kaspa batch size.** The endpoint takes a list; the documentation does not
  say how long a list, and declares no `maxItems`. See the section above: 64 is a guess and
  the refusal is written to name the size.
- **Kaspa's rate limit.** Nothing is documented anywhere, and no `ratelimit-*` header is
  sent. `DEFAULT_MIN_HOST_INTERVAL_MS` is shared, so Kaspa inherits one request per second
  per host -- acceptable because Kaspa batches, which is what makes the shared floor
  affordable for it.
- **Pagination and retention.** Neither matters for a balance read. Both will matter for
  transaction history, and neither has been checked for either vendor.
- **What `isUtxoIndexed` means when it is false.** The Kaspa provider requires it, on the
  reading that a synced node without a UTXO index cannot answer a balance query. If the
  field means something narrower, the provider reports unhealthy where the vendor reports
  healthy -- a false alarm rather than a false balance, which is the right direction to be
  wrong in, but it is a guess about a field's meaning.
- **That `mempool_stats` is always present.** The Bitcoin provider reads its absence as
  `pending=None` rather than as a zero, which is the safe reading of a field the vendor
  never promised.

### A residual the address cannot close -- for Bitcoin, and not for Kaspa

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

**Kaspa has no equivalent residual, and the contrast is worth reading rather than
assuming.** `kaspa`, `kaspatest` and `kaspadev` are three distinct prefixes and each one is
folded into the 40-bit CashAddr checksum, so the same payload checksums differently on each
network and no string can be read as two of them. `kaspa_network_of` is exact where
`bitcoin_network_of` is approximate. That is a property of the two chains' address formats,
not of how carefully the two functions were written, and a reader who assumes the second
module is a copy of the first will draw the wrong conclusion about both.

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
| `PORTFOLIO_KASPA_API_URL` | `https://api.kaspa.org` | the instance tried first |
| `PORTFOLIO_KASPA_API_FALLBACK_URL` | *(blank)* | tried when the first fails; blank is the default, because there is one public operator and no second to name |
| `PORTFOLIO_KASPA_NETWORK` | `mainnet` | `mainnet`, `testnet` or `devnet`; must match what the URLs above serve |

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

## Price sources, which are a different kind of provider

A chain provider answers a question about the owner's addresses. A price source answers a
question about the market, which has the same answer for everyone. They share a transport, a
rate limiter and an error hierarchy, and they differ in three ways worth stating before the
numbers:

- **A price source declares which pairs it can answer**, and the order for a pair is the
  global source order filtered by that declaration. There is no single failover chain,
  because the sources are four unrelated vendors rather than interchangeable instances of one
  API. `providers/prices/base.py` holds the loop; `providers/prices/registry.py` holds the
  order.
- **A partial answer is normal.** `EndpointSet` treats one endpoint answering about half a
  request as a correlation bug; `fetch_prices` treats one source answering three pairs of
  four as the ordinary case and asks the next source for the fourth.
- **No request path may reach one.** `backend/.importlinter`'s
  `prices-are-never-fetched-in-a-request` contract forbids any chain from
  `portfolio.api.routers` to `portfolio.providers.prices`, **without**
  `allow_indirect_imports` — so `router -> service -> source` is caught as a chain. The
  mechanical consequence is that `services/prices.py` (valuation) imports no provider and
  `services/price_refresh.py` is the only module in `services/` that does. That split is the
  guarantee; see both module docstrings.

### The measured monthly call budget

**One request per refresh. At an hourly refresh that is 24 a day and 24 × 30 = 720 a month**,
to one host. Measured on **2026-09-23**:

```
GET https://api.kraken.com/0/public/Ticker?pair=XXBTZUSD,XXBTZEUR,KASUSD,KASEUR
-> 200, {"error": [], "result": { ...four entries... }}
```

All four pairs come back from a single call, key-free, so the whole of a healthy refresh is
one request. The arithmetic in full, so it can be checked rather than believed:

| Quantity | Value | Where it comes from |
|---|---|---|
| requests per refresh, healthy | **1** | measured: Kraken batches all four pairs |
| refreshes per day | **24** | hourly, which is `STALE_AFTER` |
| days per month, for this budget | **30** | the conventional month |
| **requests per month** | **24 × 30 = 720** | one host, Kraken |
| a 31-day month | 24 × 31 = 744 | the worst case, still nowhere near any published limit |
| a year | 24 × 365 = 8,760 | |

**Kraken publishes no monthly quota for the public ticker at all**, and none was measured.
The shared `DEFAULT_MIN_HOST_INTERVAL_MS` floor of one request per second per host applies,
which is about 86,400 requests a day if anything ever wanted them — three orders of magnitude
above what this needs.

**The budget on a bad day is bounded and worth knowing.** If Kraken fails, the fallbacks cost
more because they are not batched: Coinbase is one request per pair for the two BTC pairs, the
Kaspa endpoint is one request for KAS/USD, and KAS/EUR has no key-free fallback at all. So a
refresh with Kraken down costs at most **3 requests to three different hosts** (plus one
failed Kraken attempt), or 4 with CoinGecko keyed. It never costs more than one request per
pair per source.

**The issue's premise about the budget turned out not to hold, and the conclusion still
does.** #9 was written around CoinGecko's Demo quota — roughly 10,000 calls a month, about 13
an hour — and concluded that no request path may call a price API. Once the primary is
key-free and batched, that quota stops being the binding constraint. The rule stands for two
better reasons: a request path that calls a price API inherits the vendor's **latency** (a
dashboard that renders in 80 ms would block on a third party) and its **outages** (a vendor
having a bad afternoon would take the portfolio page down with it). Quota was the weakest of
the three arguments and is the only one that changed.

### Coverage, per pair, and what each source is

| Pair | Order | Why |
|---|---|---|
| BTC/USD | Kraken, Coinbase, CoinGecko¹ | all three list it |
| BTC/EUR | Kraken, Coinbase, CoinGecko¹ | all three list it |
| KAS/USD | Kraken, Kaspa, CoinGecko¹ | Coinbase does not list KAS |
| KAS/EUR | Kraken, CoinGecko¹ | Coinbase does not list KAS; the Kaspa endpoint has no EUR |
| anything else | nothing | refused without a request |

¹ only when `PORTFOLIO_COINGECKO_API_KEY` is set. With no key the source is **not in the
list and not constructed** — criterion 5's "absent, not skipped" — and its class refuses to
be built without one, so there is no object holding a blank credential.

### Confirmed against the live services on 2026-09-23

| Source | Endpoint | BTC/USD | BTC/EUR | KAS/USD | KAS/EUR | Key | Price type |
|---|---|---|---|---|---|---|---|
| Kraken | `GET /0/public/Ticker` | yes | yes | yes | yes | none | **string**, in `c[0]` |
| Coinbase | `GET /v2/prices/{pair}/spot` | yes | yes | **404** | **404** | none | **string**, `data.amount` |
| Kaspa | `GET /info/price` | — | — | yes | **no** | none | **JSON number** |
| CoinGecko | `GET /api/v3/simple/price` | doc | doc | doc | doc | Demo | **not measured** |

- Kraken's last traded price is `c[0]`, where `c` is `[price, lot volume]`. The envelope is
  `{"error": [], "result": {...}}` and **a failure is reported in `error` with a 200 status**,
  so a status check alone would read an error document as "no prices".
- Coinbase echoes `base` and `currency` in its body, and the parser checks both against what
  it asked for. The pair is in the **path**, so a mis-keyed cache entry is one step from
  attaching one asset's price to another.
- **The Kaspa body is `{"price": 0.04228645}` and it names no currency.** See below.
- **No vendor returns a quote timestamp**, on any of the three measured endpoints. `as_of` is
  therefore the time *we observed* the price, and the column, the dataclass and the docstrings
  all say so rather than implying otherwise. A vendor that starts supplying one can populate
  that field more honestly without a migration.

### The Kaspa price endpoint's currency is an assumption, not a fact

The body is `{"price": ...}` and nothing else. The documentation names no quote currency. USD
is an inference from the number's magnitude against the market on the day it was measured,
which is not evidence.

Three mitigations, and they are the whole of the answer:

1. It is **last** for the one pair it can answer — KAS/USD is `Kraken, Kaspa, CoinGecko` — so
   the guess is only used when a source that *states* its currency has already failed.
2. It is **absent** from KAS/EUR entirely. It never converts and never infers a second
   currency from the one it assumed.
3. The assumption is named at the call site (`ASSUMED_CURRENCY` in
   `providers/prices/kaspa.py`), in that module's docstring, and here.

Using a price whose currency is a guess to value somebody's holdings is exactly the failure
criterion 3 describes. The blast radius is one asset, in one currency, only when Kraken and
CoinGecko are both unavailable — but it is still a guess being used to value money, and it is
recorded as one.

### CoinGecko is the one parser written from documentation rather than a response

**Its response shape was not measured**, because measuring it needs a Demo key and rule 3
forbids this repository from containing one. Read from the vendor's documentation on
2026-09-23:

- Demo root `https://api.coingecko.com/api/v3/`, distinct from the Pro root
  `https://pro-api.coingecko.com/api/v3/`.
- Demo key header `x-cg-demo-api-key`; the Pro header is `x-cg-pro-api-key`.
- `GET /api/v3/simple/price` takes `vs_currencies` (required) and `ids`, both comma-separated,
  plus a `precision` of `0`–`18` or `full`. This application sends `full`, because the default
  rounds and a price rounded before it reaches us cannot be un-rounded.
- The response is keyed by coin id and then by lower-case currency:
  `{"bitcoin": {"usd": 86123.45, "eur": 79211.02}}` — **JSON numbers, not strings**.
- "Each successful request (HTTP 200) deducts 1 credit from your monthly quota."

**The Demo plan's numbers — 10,000 calls a month, 100 a minute — come from the issue, not from
a page read here.** The authentication documentation says credits and rate limits depend on
the plan and points at a pricing page. Both figures are far above an hourly refresh either
way, and this source is only asked when the primary has already failed.

So this is the one parser in the package that meets a real server for the first time on the
day it is needed. A response that differs from the shape above is refused as untrustworthy
rather than mis-parsed, which is the right direction to be wrong in; it is still a refusal
that arrives in production rather than in a test.

**The key travels in a request header and never in the query string.** Both spellings are
documented and they are not equivalent: a key in a query string is recorded by the vendor's
access log, by every intermediary, and by anything that renders a URL. `providers/http.py`
warns by name that `strip_query` meets the letter of this repository's logging rule and leaks
anyway. The header is passed per request through `EndpointSet.read`, never set on the shared
client — where it would be sent to every host every other provider talks to.

### The float boundary, which is what this change was actually about

Two of the four sources send a price as a JSON **number**. `json.loads` turns
`0.04228645` into a `float` before any application code runs, and the digits the vendor sent
are gone by then — no care afterwards recovers them. Rule 2 is not "do not write the word
`float`"; it is "do not let a monetary value pass through binary floating point", and the only
place that can be decided is the parser.

`providers/base.decode_json` therefore passes **`parse_float=Decimal`**, so a JSON number
arrives built from the literal text on the wire. It is fixed rather than a parameter: an
argument would let a call site ask for the double back, and there is no vendor for which the
double is the more faithful answer.

It is a **shared** decoder change and the blast radius is every provider, the two balance
providers included. Their parsers demand an `int` and `Decimal("1.0E+8")` is no more an `int`
than `1.0e8` was, so a balance rendered with a decimal point is refused exactly as before —
with the type in the message reading `Decimal` instead of `float`. The existing Bitcoin and
Kaspa suites are the control for that and were run untouched.

### What a price source must implement

| Member | Kind | What it must do |
|---|---|---|
| `name` | property | The vendor's brand, lower case, as written to `prices.source`. **Never a host.** |
| `pairs` | property | Every `(symbol, currency)` it can answer. A declaration, so an ineligible pair costs no request. |
| `fetch` | async method | Read the requested pairs in as few calls as it can. A partial answer is allowed; an answer about a pair nobody asked for is a refusal. |

`PriceSource` is a `typing.Protocol` and is **not** `@runtime_checkable`, for the reason
`ChainProvider` is not.

Two more rules a new source inherits rather than decides:

- **Prices go through `require_price`**, which is the one boundary deciding what counts as a
  price: a JSON string or a `Decimal` from the shared decoder, positive, finite. Four vendors,
  one rule.
- **A new endpoint label goes in `ENDPOINT_LABELS`** in `providers/http.py`, in the same
  change as the call site that uses it. Membership is the gate; an unlisted label renders as
  `<unlabelled>` and the request becomes invisible in a log.

### Prices in the database

One table, `prices`, one row per `(asset_id, quote_currency)` — four today. `amount` is
`NumericText(12)` and **never `sqlalchemy.Numeric`**, which round-trips through a C double on
SQLite. Twelve decimal places serve a sub-cent asset and a five-figure one in the same column:
KAS was quoted near `0.042` and BTC near `86,000` on the day this was measured.

**Money is never aggregated in SQL.** `SUM`, `ORDER BY` and `<` on a `TEXT` money column all
apply SQLite's numeric affinity, which is the double the column type exists to avoid — applied
to every row at once. `repositories/prices.py` has no method that totals, sorts by price or
compares one; the valuation service loads the rows and sums them in Python.

**Staleness is computed at read time from an injected clock and is never stored.**
`STALE_AFTER` is one hour, matching the refresh interval #10 will schedule. A stored
`is_stale` boolean would be wrong one second after it was written and would need a background
job whose only purpose was to keep a derived field true. A stale price is still returned, with
its age visible: the last known price is better information than none, which is the same
argument `ProviderUnavailableError` makes about a balance.

**A missing price is a reason and never a zero.** `lookup_price` returns a `Price` or a
`PriceUnavailable`, and `value_portfolio` returns the total it could compute, the holdings it
could not price, and a `complete` flag. A portfolio silently showing 0 is worse than one
showing an error, because it is believed.

**That rule is enforced at the column as well as at the row**, because review found a path
that defeated it. `require_price` refuses a price of zero or below, but it runs *before* the
value is rounded to the column's twelve places — so a positive price under half of one unit
in the last place was accepted, stored as `0.000000000000`, and produced a portfolio total of
zero marked `complete`. No missing row for a valuation to notice, and no reason to report.

Closed in two places, which is the shape worth copying:

- **`NumericText` refuses a non-zero amount that rounds away to nothing.** That belongs to
  the column, not to prices — a fee, a fill or a cost basis added later meets the same
  boundary — and it is a `ValueError` beside the existing refusal for too many digits *before*
  the point. A true zero still binds. The message names the scale and not the amount, because
  this type will eventually hold a quantity and a quantity is the owner's holdings.
- **`require_price` refuses a price outside what the column can store, in both directions.**
  Too large by `MAX_PRICE_INTEGER_DIGITS`, or so fine that rounding it leaves zero. Doing it
  here makes an implausible number an ordinary vendor error: the failover passes the source
  over, the other pairs are kept, and the pair falls to the next source or becomes a reason.
  Leaving it to the column would surface as a `ValueError` out of a repository — a traceback
  from an operator's command, and a whole refresh lost to one bad number.

The general rule: **a value a money column would silently transform is refused by the column,
and a value a vendor should never have sent is refused by the parser.** The first protects
every writer; the second keeps a vendor's mistake on the vendor's error path.

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
  walks the wallet modules and fails on a log call that could carry an address. Neither
  provider has a log call at all -- deliberately, since the transport's contract is the only
  one that is enforced rather than remembered -- so the walk is worth extending the day a
  provider needs one.
- **Network-aware address registration.** A wrong-network address is refused by the
  *provider*, at read time, not when the wallet is registered. Making registration
  network-aware changes #5's contract and needs a story for rows that already exist.
- **The Kaspa batch ceiling.** 64 is a guess; see above. The first evidence will be a
  refused batch in production, and the refusal is written to carry the size so that the
  evidence is actionable when it arrives.
- **Building the price sources in the lifespan, and scheduling a refresh.** #9 ships
  `refresh_prices()` as a service with no caller in the running application, plus a
  `portfolio refresh-prices` command so the measured call budget can be checked by hand
  before anything automates it. **#10 owns the scheduler**, and a scheduler invented in #9
  would have been a second one to delete.
- **The price source base URLs are module constants, not settings.** Kraken, Coinbase and
  CoinGecko each have exactly one correct host and no self-hosting story, so a
  `PORTFOLIO_*_URL` for them would be a variable with one right value plus a validation path
  and a row in the operations table. The Kaspa price source deliberately reuses
  `PORTFOLIO_KASPA_API_URL`, since it is the same server the balances are read from.
  Promoting the other three is a change a real need should drive.
- **CoinGecko's parser has never met its vendor.** Its response shape is documentation
  rather than measurement, because measuring it needs a key this repository must not
  contain. It is the one parser here that meets a real server for the first time on the day
  it is needed, and the sections above say so rather than leaving it to be assumed.
- **The Kaspa price endpoint's currency is assumed.** USD, inferred from magnitude. Confined
  to one pair, placed behind every source that states its currency, and written down in
  three places. If the vendor ever names a currency, `ASSUMED_CURRENCY` is deleted rather
  than edited.
- **Kraken is a single point of failure for KAS/EUR**, the only key-free source for that
  pair. Losing it means that pair falls to CoinGecko or to a reason.
- **`parse_rate_limit` has no production exerciser.** Measured on 2026-09-23: neither Kaspa
  endpoint sends a `ratelimit-*` header, and Esplora was never claimed to. It is tested
  against synthesised headers only, which is to say it is code that looks tested and is not
  known to work against any real server. It stays because the criterion is explicit and a
  self-hosted index without a CDN may send them -- recorded here because unexercised code
  that looks tested is how a green suite lies.
