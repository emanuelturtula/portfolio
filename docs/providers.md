# Adding a provider

What a new chain has to implement, what a new price source has to implement, what a new
exchange has to implement, what the shared machinery already does for all three, and --
kept separate on purpose -- which facts about each vendor were confirmed against its
published documentation, which were measured against the live service, and which are still
guesses.

Three kinds of provider live under `backend/src/portfolio/providers/`. A **chain provider**
reads balances from addresses (`providers/chains/`); a **price source** reads what an asset
costs (`providers/prices/`); an **exchange provider** reads the spot fills on the owner's
account at a venue (`providers/exchanges/`). Everything down to "Vendor facts" is about the
first kind; the "Price sources" section near the end is about the second, and the "Exchange
providers" section after it is about the third. Each says where it differs.

Read `backend/src/portfolio/providers/base.py` alongside this. The docstrings there are the
reasoning; this is the checklist.

**Every vendor fact here carries one of three words**, in the summary table at the top of
each provider's section and in the detail beneath it:

- **confirmed**: read in the vendor's published documentation, on the date given;
- **measured**: seen in an answer from the live service, on the date given;
- **unverified**: neither. The row says what would settle it, and who is placed to.

A fact nobody has read or seen is unverified, however plausible. The summary tables are under
"Vendor facts" for Bitcoin and Kaspa, under "Price sources" for the four price sources, and
under each venue's own heading for Bitget and BingX.

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

`portfolio.providers.http.build_http_client()` returns an `httpx.AsyncClient` that already
carries connect/read/write/pool timeouts and does not follow redirects, and whose transport
carries bounded retry with full jitter, `Retry-After` handling and a per-host rate limiter. A
provider takes the client in its constructor and does not build its own.

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
the fallback unreachable in exactly them. The misconfiguration is not lost by moving on:

- the transport logs a 4xx or 5xx, and a request that got no answer, at error, as
  `provider_request_failed` with the host and the label, whether or not the next instance
  answers. A 3xx is the exception: it fails over, and is logged only at debug;
- `health()` probes each instance in turn and says which one failed. That loop stays in your
  provider, because only you know how to read your vendor's health document.

**Nothing in production calls `health()` yet.** `GET /api/health/detail` reads recorded
outcomes and asks no vendor (`services/health.py`), so today the log line is where a broken
instance behind a working one shows.

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

The transport logs `"{scheme}://{host}/{label}"` and **never the path**. Both chain vendors
put the address in the path, so a log line built from the URL would disclose exactly what
the wallet registry refuses to disclose.

**A label reaches the log only if it is a member of `ENDPOINT_LABELS`.** Anything else --
including a perfectly well-shaped string -- renders `"<unlabelled>"`. That is the completion
of a residual #6 recorded and #7 closed: the gate used to be a *pattern*, and a truncated
address is lower-case, alphanumeric and under 32 characters, so it matched the pattern and
reached the log. Membership in a frozen set cannot be satisfied by accident.

The set this release ships is exactly nine: `address_balance` for a single balance read,
`address_balances` for Kaspa's batch read, `block_tip_height` for the tip-height call
Esplora's `health()` makes, `node_health` for the health document Kaspa's reads,
`asset_price` and `asset_prices` for the price reads (#9), `exchange_fills` and
`exchange_symbol` for Bitget's signed fills read and its public symbol lookup (#13), and
`exchange_balances` for either venue's signed read of its spot balances (#104).

So a new endpoint is two lines, not one: the constant, and its name in `ENDPOINT_LABELS`.
The same shape as `PUBLIC_API_PATHS` in rule 8 -- the default says nothing, and saying more
about an endpoint is a visible edit to a named constant. The label must still match
`ENDPOINT_LABEL`'s pattern, which a test asserts over the set's contents.

`strip_query(url)` exists separately and removes the query string, the fragment and any
userinfo. It is the rule `CLAUDE.md` states, written for the exchange that signs its requests
in the query string. **No provider calls it.** That exchange is BingX, and its requests are
logged through `request_target` like every other, which carries neither the path nor the
query. `strip_query` is not sufficient for a chain provider either: reaching for it to log a
chain request would meet the letter of the rule and leak the address anyway.

Four further rules, none of them optional:

- **Never log a response body.** An error body from a public index can echo the request,
  which is to say the address.
- **Never log a URL you built yourself.** The transport is the only thing enforcing the log
  contract, and a provider with a log call of its own bypasses all of it.
- **Never turn the `httpx` logger back up.** `configure_logging` holds `httpx` and
  `httpcore` at WARNING, because `httpx.AsyncClient.send` logs every request at INFO with
  the full URL -- path and query string -- through the standard library, above the
  transport. Since #23 such a record does pass the root formatter's redaction, which
  replaces addresses, extended keys and a URL's query by pattern. That is the second layer,
  not a licence: a pattern catches only the shapes it knows, and a handler added beside the
  root one sees the record unredacted (spec 030, R10). Use the transport's own
  `provider_request` line, or add a temporary field to it; both are address-safe by
  construction.
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

### The two chain providers at a glance

The endpoints are read from `providers/chains/bitcoin.py` and `providers/chains/kaspa.py`.
The statuses are the ones the sections below establish, and each row's detail is there.

#### Bitcoin: Esplora, at mempool.space and blockstream.info

| | What the code uses or assumes | Status |
|---|---|---|
| balance read | `GET /address/{address}`, one address per call, labelled `address_balance` | **confirmed** 2026-09-22, from Blockstream's `API.md` and mempool.space's REST documentation |
| batch read | none; `max_addresses_per_call` is 1 | **confirmed** 2026-09-22 and 2026-10-03: neither documents one |
| extended keys | derived locally; neither vendor takes a key or a descriptor | **confirmed** 2026-10-03 |
| "used", for a key's scan | `tx_count`, in `chain_stats` and in `mempool_stats` | **confirmed** 2026-10-03 |
| health | `GET /blocks/tip/height`, labelled `block_tip_height`. Nothing in production calls it | **confirmed** 2026-09-22 |
| base URLs | `https://mempool.space/api` first, `https://blockstream.info/api` second | **confirmed** 2026-09-22 |
| rate limit, mempool.space | no number published. Exceeding it is a 429, and exceeding it repeatedly may end in a ban | **confirmed** 2026-09-22, read again 2026-10-03 |
| rate limit, blockstream.info | none documented | **confirmed** 2026-09-22, read again 2026-10-03 |
| our pacing | one request a second per host, `DEFAULT_MIN_HOST_INTERVAL_MS` | **unverified**: a guess from the ban warning. A 429 in a production log would settle it, and the operator is the one to see it |
| retention or history window | none needed: a balance read asks for no history | **unverified** for both vendors, and moot until something reads transaction history. No issue owns it |
| error statuses | none documented, so every mapping is by status alone | **unverified**: neither vendor documents one. An address is validated offline, so the case is not asked |
| an address never used | assumed to answer in the documented shape, with zeros | **unverified**. A 404 instead would fail the scan as `response`, loudly; no issue owns it |
| `mempool_stats` | read as `pending=None` when it is absent | **unverified** that it is always sent |
| `Retry-After` | honoured when sent | **unverified** that either vendor sends one |

#### Kaspa: kaspa-rest-server, at api.kaspa.org

| | What the code uses or assumes | Status |
|---|---|---|
| single read | `GET /addresses/{address}/balance`, labelled `address_balance`, for a chunk of one address | **confirmed** 2026-09-22, from the live OpenAPI document |
| batch read | `POST /addresses/balances`, body `{"addresses": [...]}`, labelled `address_balances`, declared idempotent | **confirmed** 2026-09-22 |
| batch ceiling | 64, `MAX_ADDRESSES_PER_CALL` | **unverified**: the document declares no `maxItems` (confirmed 2026-09-22). A refused batch in production, a 413 or a 422 naming its size, would settle it; the operator is the one to see it |
| health | `GET /info/health`, labelled `node_health`. Nothing in production calls it | **confirmed** 2026-09-22 |
| errors | 422 on the balance endpoint, 503 on health | **confirmed** 2026-09-22 |
| base URL | `https://api.kaspa.org`, and no fallback by default | **confirmed** 2026-09-22: one operator, and the document declares no `servers` block |
| rate limit | none documented, and no `ratelimit-*` or `x-ratelimit-*` header sent | **confirmed** absent from the document 2026-09-22; headers **measured** absent 2026-09-23 |
| our pacing | one request a second per host, shared with the price read | **unverified**: a 429, or a CDN's 403, in a production log would settle it; the operator |
| what is in front | Cloudflare, with `Cache-Control: public, max-age=8` on a balance answer and on an error | **measured** 2026-09-23 |
| networks | the public instance serves mainnet only; a test-network address is a 422 | **measured** 2026-09-23 |
| checksums | the vendor does not check them | **measured** 2026-09-23 |
| retention or history window | none needed: a balance read asks for no history | **unverified**, and moot until something reads transaction history. No issue owns it |
| `isUtxoIndexed` | required true for a node to count as usable | **unverified** what the field means when false. The vendor's documentation or source would settle it; no issue owns it |

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
every Python file under `backend/tests/` for a mainnet address, and
`tests/providers/test_documentation.py` refuses the mainnet prefixes in this document. Those
two are what make it a control rather than a request.

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

## Extended public keys

Spec 031. A Bitcoin wallet can be registered by its account extended public key instead of
by single addresses, and the Esplora provider then reads every address the key derives. The
public APIs cannot do this for us, so the derivation is ours.

### Why the vendors cannot answer it: confirmed on 2026-10-03

Read again on **2026-10-03**, from Blockstream's published `API.md` and mempool.space's REST
documentation, and recorded with the date for the reason the section above gives:

- **Neither documents an endpoint that takes an extended key or a descriptor.** Every address
  endpoint takes one address (or one script hash), and neither offers a lookup of many
  addresses at once. So a key is derived locally and each address read with the same
  `GET /address/:address` a registered address uses.
- **`tx_count` is in both `chain_stats` and `mempool_stats`**, beside the four sums the balance
  read already used. That is what "used" is read from (below).
- mempool.space still states that exceeding its limits returns 429 and that repeatedly
  exceeding them may result in a ban, and still publishes no numbers. Blockstream documents no
  limit. The one-second floor per host stands, and a scan inherits it.

**Not documented, and therefore assumed:** what either vendor answers for an address that has
never been used. The parser requires the documented shape -- the address echoed, both sums and
`tx_count` -- and a never-used address is assumed to come back in that shape with zeros.
If an instance answered a never-used address with a 404 instead, the failover would treat it as
a refusal and ask the second instance. When every instance has been tried, the chain's failure
is classified by the last instance asked (`EndpointSet._failure_for` and its caller):

- a 429 is `rate_limited`;
- a 5xx, or no answer at all, is `unavailable`;
- a 404, or any other status that is neither 200 nor one of the above, is `response`.

So if the second instance also answers 404, the scan fails as `response`, and it fails as
`unavailable` only if that instance could not be reached. Either way it is a loud failure,
never a wrong number, which is the direction this is allowed to be wrong in.

### What is derived

The script type comes from the key's version bytes (SLIP-0132), and so from its prefix, and
from nothing else. This document names the test-network prefixes and the mainnet version
bytes, never the mainnet prefixes themselves: `tests/providers/test_documentation.py` keeps
mainnet material out of the page a provider author copies from, and the three mainnet
prefixes are in that list. `docs/operations.md`, section 8, gives them by name for the owner.

| Script | Test-network prefix | Mainnet version bytes | Derivation below the key |
|---|---|---|---|
| P2PKH (BIP44) | `tpub` | `0x0488B21E` | `/0/i` and `/1/i` |
| P2SH-P2WPKH (BIP49) | `upub` | `0x049D7CB2` | `/0/i` and `/1/i` |
| P2WPKH (BIP84) | `vpub` | `0x04B24746` | `/0/i` and `/1/i` |

- **The P2PKH-version caveat.** Many wallets export a segwit account's key with the P2PKH
  version, `0x0488B21E` (the BIP32 default) on mainnet. Such a key derives P2PKH addresses
  only, so the wallet reads as zero. The remedy is the owner's: export the key again with the
  P2WPKH version (or the P2SH-P2WPKH one). Guessing the script type from what the chain
  reports would mean reading three times the addresses to find out, and still guessing.
- **No multisig and no taproot.** `Ypub`, `Zpub`, `Upub` and `Vpub` are refused by their
  prefix, and there is no SLIP-0132 prefix for taproot to accept.
- **Private keys are refused by their prefix**, wherever it stands: on every chain, before
  anything else is read, decoded or length-checked, and named `private_key`
  (`looks_like_private_key`). A value is refused when either holds:
  - ignoring surrounding whitespace and Unicode format characters (zero-width space, word
    joiner, directional marks, a byte-order mark), it starts with one of
    `PRIVATE_KEY_PREFIXES`;
  - with those format characters removed, a private-key-shaped run appears anywhere in it
    (`PRIVATE_KEY_RUN_PATTERN`): a private prefix that does not continue a Base58 run,
    followed by 100 or more Base58 characters. No address contains a Base58 run that long,
    and the left boundary keeps the run off a public key's own body.

  A key glued directly onto other Base58 text is not such a run. It is refused for another
  reason, and it is never stored. Private keys are never needed, and the redaction and
  secret-scanning rules cover them too.
- **The depth is not enforced.** Derivation is always `/branch/index` below the key as given,
  which suits an account key at depth 3 and Electrum's depth-1 export alike.
- **A key is stored by its canonical form** (`canonical_extended_key`): re-serialised at depth
  0, with a zero parent fingerprint and child number 0, keeping the version, chain code and
  public key. Two exports of one account that differ only in those three fields derive the
  same addresses, so the second is refused as a duplicate rather than doubling the total. The
  version is kept, so a P2WPKH and a P2PKH version over the same bytes are two wallets. The
  provider is handed the canonical form; the owner only ever sees the key as typed, masked.
- Derived addresses are encoded for `PORTFOLIO_BITCOIN_NETWORK`: `bc`, `tb` or `bcrt` for
  bech32, `0x00`/`0x05` on mainnet and `0x6F`/`0xC4` on both test networks for Base58. A key of
  the other family is refused as `wrong_network` before any request for that key. The
  chain's address wallets are read first, as one batch, so the chain may already have made
  requests by then; it fails as a whole either way.

The cryptography is in `domain/`: `secp256k1.py` (decompression, addition, multiplication),
`ripemd160.py`, and `extended_keys.py` (parsing, BIP32 public derivation, encoding, the gap
arithmetic). It is pure Python with no new dependency, pinned by the published BIP32, BIP49 and
BIP84 vectors in test-network form. RIPEMD-160 is not taken from `hashlib`, because whether
`hashlib` has it depends on the OpenSSL build.

### The scan: gap limit, cap and cost

- **Gap limit 20 per branch**, BIP44's documented value. A branch is complete when its last 20
  addresses by index are unused; one with nothing used is complete at 20.
- **Used** means `chain_stats.tx_count > 0`, or `mempool_stats.tx_count > 0` when that object
  is present. An address emptied by a spend is used with a zero balance, which is why the
  balance cannot be the test. An address once recorded as used stays used, even if an instance
  that has pruned its history later reports nothing for it.
- **Incremental.** The addresses a scan finds are persisted in `derived_addresses`, in the same
  per-chain commit as the wallet's snapshot. The next scan derives only above the highest
  persisted index on each branch -- but **reads every persisted address on every sync**, used
  or not, because funds can arrive at any of them.
- **Capped at 1000 addresses per branch** (`MAX_ADDRESSES_PER_BRANCH`). A branch that would pass
  it fails the read with `ProviderResponseError` and a fixed sentence. The plausible cause is an
  instance reporting history for every address, and the alternative is a scan without end.
  A failed scan persists nothing, so such an instance costs about 1000 requests on every sync
  until it is fixed or replaced, and the Bitcoin chain stays failed meanwhile -- loud, by design.
- **An index BIP32 gives no key is skipped**: never read, never persisted, never counted toward
  the gap. The probability is below 2^-127 per index.
- **Cost.** A first scan is at least 40 requests -- 20 per branch -- and the requests are
  sequential through the shared client, so with the one-second floor per host it takes at
  least 40 seconds, plus about one request for each used address. Every later sync costs one
  request per persisted address. All of it goes through the same endpoint label, retry policy
  and host limiter as a registered address's read, and failover is sticky for the whole scan.

**The disclosure is larger, and stated plainly.** A key's whole run of addresses, used and
unused, is sent to the configured instance on every sync, from one IP and in index order. That
tells the instance more than a single address does. Section 8 of `docs/operations.md` already
says what running your own Esplora instance buys; it buys more here.

`EsploraProvider.scan_extended_key` is the implementation, and `ExtendedKeyScanner` in
`providers/base.py` the protocol. A future provider that can scan a key says so by satisfying
it; the balance sync checks at run time and fails a chain as `internal` if a key wallet meets a
provider that cannot.

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
- **An answer that is wrong rather than incomplete is discarded whole.** Two checks in the
  loop, both applied to the response rather than trusted to the four parsers: a source that
  answers about a pair nobody asked for has proved its correlation is broken, and a source
  whose amount the `prices` column cannot hold has produced something no later layer can
  store. Either way the source is passed over as though it had not answered, and the
  outstanding pairs go to the next one. Each parser checks the same things for its own
  document; the duplication is deliberate, because a rule enforced in four places is a rule
  one of them can drop, and the fifth source nobody has written yet is the one that would.
  The second check also keeps a value problem on the vendor's error path: without it an
  unstorable amount reaches `NumericText`, and the `ValueError` rolls back every pair that
  had already succeeded in the same refresh.
- **No request path may reach one.** `backend/.importlinter`'s
  `prices-are-never-fetched-in-a-request` contract forbids any chain from
  `portfolio.api.routers` to `portfolio.providers.prices`, **without**
  `allow_indirect_imports` — so `router -> service -> source` is caught as a chain. The
  mechanical consequence is that `services/prices.py` (valuation) imports no provider and
  `services/price_refresh.py` is the only module in `services/` that does. That split is the
  guarantee; see both module docstrings.

### Each source at a glance

The endpoints are read from the four modules under `providers/prices/`. No source is asked for
history, so none has a retention window to record: each is asked for the current price only.

#### Kraken, the primary

| | What the code uses or assumes | Status |
|---|---|---|
| endpoint | `GET https://api.kraken.com/0/public/Ticker?pair=XXBTZUSD,XXBTZEUR,KASUSD,KASEUR`, labelled `asset_prices` | **measured** 2026-09-23 |
| what one call answers | all four pairs, with no key | **measured** 2026-09-23 |
| the price | a string, `c[0]`; a failure arrives in `error`, on a 200 | **measured** 2026-09-23 |
| the keys of `result` | the pair codes asked for. An entry under any other key is refused, never matched by position | **measured** 2026-09-23 for these four; the documentation does not promise it |
| rate limit | public endpoints are limited per IP address, and calling them once a second or less stays within the limit. No monthly quota is stated | **confirmed** 2026-10-04, Kraken's support article on API rate limits |

#### Coinbase, the first fallback for bitcoin

| | What the code uses or assumes | Status |
|---|---|---|
| endpoint | `GET https://api.coinbase.com/v2/prices/{BASE}-{QUOTE}/spot`, one pair per call, labelled `asset_price` | **measured** 2026-09-23; **confirmed** 2026-10-04 |
| pairs | BTC/USD and BTC/EUR; KAS is a 404 in either currency | **measured** 2026-09-23 |
| key | none | **measured** 2026-09-23; **confirmed** 2026-10-04 |
| the price | a string, `data.amount`, beside `data.currency` | **measured** 2026-09-23; **confirmed** 2026-10-04 |
| `data.base` | required, and checked against the pair asked for | **measured** 2026-09-23 only: the documented example, read 2026-10-04, shows `amount` and `currency` and no `base`. If it stopped arriving, every bitcoin answer here would be refused and fall to the next source. No issue owns it |
| rate limit | none known | **unverified**: the documentation, read 2026-10-04, gives 10,000 requests an hour per API key or OAuth user, and nothing for a call with neither. A 429 in a production log would settle it; the operator |

#### The Kaspa server's price endpoint, the last key-free source for KAS/USD

| | What the code uses or assumes | Status |
|---|---|---|
| endpoint | `GET /info/price` on `PORTFOLIO_KASPA_API_URL`, then its fallback, labelled `asset_price` | **measured** 2026-09-23, on `https://api.kaspa.org` |
| key | none | **measured** 2026-09-23 |
| the price | a JSON number, `{"price": ...}`, decoded as a `Decimal` | **measured** 2026-09-23 |
| the currency | USD, `ASSUMED_CURRENCY` | **unverified**: the body and the documentation name none. Only the vendor naming one would settle it; no issue owns it |
| rate limit | nothing read and nothing measured for this path | **unverified**. It shares the host's one-a-second floor with the balance reads |

#### CoinGecko, only when `PORTFOLIO_COINGECKO_API_KEY` is set

| | What the code uses or assumes | Status |
|---|---|---|
| endpoint | `GET https://api.coingecko.com/api/v3/simple/price`, with `ids`, `vs_currencies` and `precision=full`, labelled `asset_prices` | **confirmed** 2026-09-23. Never called from this repository |
| key | the Demo header `x-cg-demo-api-key`, per request | **confirmed** 2026-09-23 |
| the response | keyed by coin id, then by lower-case currency; JSON numbers | **confirmed** 2026-09-23. **Unverified** against a real answer: the first refresh that reaches it with a key is the first measurement, and the operator sees it |
| rate limit | Demo plan: 10,000 call credits a month and 100 calls a minute; each 200 costs one credit | **confirmed** 2026-10-04 for the two numbers, from the pricing page; 2026-09-23 for the credit rule |

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
Its support article on API rate limits, read on **2026-10-04**, says the public endpoints are
limited per IP address and that calling them once a second or less stays within the limit.
The shared `DEFAULT_MIN_HOST_INTERVAL_MS` floor of one request per second per host is that
same rate, which is about 86,400 requests a day if anything ever wanted them — three orders
of magnitude above what this needs.

- Kraken's support article: https://support.kraken.com/articles/206548367-what-are-the-api-rate-limits-

**The budget on a bad day is bounded and worth knowing.** If Kraken fails, the fallbacks cost
more because they are not batched: Coinbase is one request per pair for the two BTC pairs, the
Kaspa endpoint is one request for KAS/USD, and KAS/EUR has no key-free fallback at all. So a
refresh with Kraken down costs at most **3 requests** beyond the failed Kraken call: two to
Coinbase and one to the Kaspa server, which is two hosts. With CoinGecko keyed it is 4, the
fourth to a third host. It never costs more than one request per pair per source.

These count calls as a source makes them. The transport may spend up to three attempts on a
call that meets a 429, a 5xx or no answer, the failed Kraken call included.

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

### Measured against the live services on 2026-09-23

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
- Coinbase's documentation, read on **2026-10-04**, agrees on the path, on needing no key and
  on `data.amount`. Its example shows `amount` and `currency` and **no `base`**, so the check
  on `base` rests on the measurement alone. Its rate-limit page gives 10,000 requests an hour
  per API key or OAuth user, and says nothing of a call made with neither. Sources:
  https://docs.cdp.coinbase.com/coinbase-business/track-apis/prices and
  https://docs.cdp.coinbase.com/coinbase-app/api-architecture/rate-limiting
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

**The Demo plan's numbers — 10,000 calls a month, 100 a minute — came from the issue, and the
pricing page, read on 2026-10-04, states the same**: 10k call credits a month and 100 calls a
minute. The authentication documentation says credits and rate limits depend on the plan and
points at that page. Both figures are far above an hourly refresh, and this source is only
asked when the primary has already failed.

- Pricing page, read on 2026-10-04: https://www.coingecko.com/en/api/pricing

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
`STALE_AFTER` is one hour, matching `PORTFOLIO_PRICE_REFRESH_INTERVAL_MINUTES`, whose default
is sixty. The two are a pair: lengthening one without the other marks every price stale most
of the time.

**A known, accepted consequence of the pair:** because `as_of` is stamped at the start of a
refresh, every price reads stale for as long as one refresh takes, once an hour -- seconds,
paced by the limiter. It errs toward "stale", the direction #9's `as_of` argument chose, and
closing it would need a threshold longer than the interval, which would let a missed refresh
go unflagged. Recorded at `STALE_AFTER` as well, so it is not re-discovered as a bug.

A stored
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

## Exchange providers, which sign their requests and fail in more ways

**The seam landed in #12, and two venues are behind it: Bitget (#13) and BingX (#14).**
#15's sync drives every configured venue -- see "The exchange sync" below. The seam
is the vocabulary every venue needs before it can be written without inventing its own: what a fill is, what a venue can do, and
why a call failed. Read `providers/exchanges/base.py` and `providers/exchanges/errors.py`
alongside this; the docstrings there are the reasoning. The Bitget and BingX sections below
record what each venue's documentation confirmed, what it left open, and -- for BingX -- what
a live probe found where the documentation was wrong.

An exchange differs from the other two kinds in the ways that shape everything below:

- **Every call is signed with the owner's credentials**, so a failure can mean a revoked key,
  a key without read permission, a throttle, an outage, or a window older than the venue
  keeps -- and those need five different reactions.
- **The answer is a stream of executions, not one number.** It arrives in pages, and the sync
  must commit a checkpoint between pages, so the seam is a page rather than a generator.
- **The amounts are the owner's holdings**, like a balance and unlike a price, so no message
  anywhere in the seam quotes one.

### The shape

`ExchangeProvider` in `portfolio.providers.exchanges.base`, four members:

| Member | Kind | What it must do |
|---|---|---|
| `capabilities` | property | Return an `ExchangeCapabilities`. Constant for the life of the instance. |
| `fetch_fill_page` | async method | Read one page of fills inside a `FillWindow`, from a cursor, for a symbol when the venue requires one. Returns a `FillPage` built with `assemble_fill_page`. |
| `candidate_symbols` | async method | The symbols worth asking about, for a venue with `requires_symbol`; an empty sequence otherwise. |
| `fetch_balances` | async method | Read what the spot account holds now (#104). Returns the `AssetBalance`s built with `assemble_balances`: one per asset, zeros left out, sorted. |

**Not `@runtime_checkable`**, for the reason `ChainProvider` is not. A fake proves
conformance with a module-level `_CONFORMS: ExchangeProvider = FakeExchangeProvider()` that
`mypy --strict` checks. **A provider raises the seven exchange error classes and nothing
else**: a `ProviderResponseError` from `decode_json`, an `httpx.TransportError` or a
`KeyError` from a parser is translated at the provider's boundary, `from` the original.

`candidate_symbols` was put in the protocol before either venue needed it, so that #14 would
not change a contract #13 already implemented. Neither wired venue needs it: both answer every
symbol in one query, and both return `()`.

### What a venue declares

| `ExchangeCapabilities` field | What the sync does with it |
|---|---|
| `exchange_key` | an `ExchangeKey` (`bingx`, `bitget`) -- the value `exchange_accounts.exchange_key` admits |
| `retention` | `clamp_to_retention` moves the oldest request inside it; `None` means the venue keeps everything |
| `max_query_window` | the longest `FillWindow` one request may cover; `assemble_fill_page` refuses a longer one |
| `page_size` | the most fills one page may carry; more is a refusal |
| `cursor_kind` | `trade_id_before`, `trade_id_after`, `time` or `none` -- how the next page is asked for |
| `rate_limit` | a `RateLimit(max_requests, per_ms)`; `min_interval_ms` rounds **up**, so 3 per 1000 ms is 334. **Nothing reads it yet**: the sync does not pace by it, and the transport's one request a second per host is stricter than either venue's declaration |
| `requires_symbol` | whether fills can only be listed per symbol |

`ExchangeCapabilities` refuses a page size below one and a zero or negative query window or
retention. `RateLimit` refuses a field below one **itself, at construction**, because a rate
limit of zero requests would divide by zero the first time anything asked for its interval.

### `NormalizedFill`: the one shape every venue's fill is translated into

`external_trade_id`, `external_order_id`, `symbol` (the venue's spelling), `base_asset`,
`quote_asset`, `side` (a `FillSide`), `quantity`, `price`, `quote_quantity`,
`quote_quantity_derived`, `fee_amount`, `fee_asset`, `executed_at`, `raw_payload`.

**It refuses what the column would transform**, with `ExchangeSchemaError`: an amount that
is not a finite `Decimal`, a quantity, price or quote quantity at or below zero, an amount
with more than 20 digits before the point, and -- the point of the type -- **an amount with
more than `FILL_SCALE` (18) fractional digits.** `NumericText` would round that silently,
which is right for a price and wrong for a quote quantity stored "as reported". The test is
`quantize(value, FILL_SCALE) != value`, so trailing zeros are not a false refusal. It also
refuses a blank trade id, symbol or asset, a `side` that is not a `FillSide`, a naive
`executed_at` or one with no UTC spelling (`datetime.min` at a positive offset, which neither
the `UtcDateTime` column nor an accounting `EventKey` can hold), and a `fee_asset` of `None`
beside a non-zero fee. `fee_amount` is signed: positive is paid, negative a rebate.

**It refuses a fill the accounting engine cannot replay**, whatever its fields (spec 020): a
`base_asset` equal to the `quote_asset`; a fee paid in the asset received that consumes
everything received; and a rebate in the asset given that is at least everything given. Each
leaves the trade with nothing received to carry a cost or nothing given to take it from. The
fill log is append-only, so it must only ever hold rows the engine can replay: a stored row
that cannot become a `Trade` would stop every position being computed, on every recompute
after it. `trade_shape_problem` in `portfolio.domain.accounting` decides all three, and it is
the same function `Trade` refuses them with, so the two cannot drift apart. A zero fee is no
fee leg, so a zero fee naming the received asset is accepted. No venue is known to send any of
these shapes; one that did would stop its account's sync with a schema error, which is the
contract for any fill the application cannot account for.

**Every text field must encode as UTF-8.** `"\ud800"` is valid JSON -- an escape for a lone
surrogate -- and `json.loads` returns it as a `str` that passes every string check until
something encodes it: the database driver inserting the fill, or the signing helper building
the next request from a cursor. Both fail with a bare `UnicodeEncodeError`, outside the
taxonomy. `NormalizedFill` refuses such text in every field, `external_order_id` included,
and `assemble_fill_page` refuses it in `cursor` and `next_cursor`, each as an
`ExchangeSchemaError` naming the field. A provider that builds a request from any other
venue-supplied string -- a symbol out of `candidate_symbols`, say -- must check it the same
way before signing.

Four rules a provider inherits rather than decides:

- **`quote_quantity` is as reported.** When a venue omits it, call
  `derive_quote_quantity(quantity, price)` and set `quote_quantity_derived=True`. Never
  recompute a reported one: a one-unit disagreement with the venue's rounding haunts every
  reconciliation after it. The derivation multiplies exactly (`domain.money.multiply`) and
  rounds once, at 18 places, whatever the calling thread's decimal context says.
- **`external_trade_id` must be unique per account across every symbol.** It is the key of
  `uq_exchange_fills_account_trade`. A venue whose ids are unique only within a symbol must
  namespace them, `BTC-USDT:12345`, or two different fills become one and the second is
  dropped without a word. BingX's ids are probably per symbol, so it namespaces them (#14).
- **Amounts go through `require_fill_amount(value, field=...)`** -- a JSON string holding a
  plain decimal number, a `Decimal` from `decode_json`, or an `int`. A `bool`, a `float`,
  whitespace, underscores, Unicode digits, `NaN` and `Infinity` are refused, and so is a
  number longer than `MAX_AMOUNT_DIGITS` (100) digits written out in full -- five thousand
  decimal places is valid JSON, and without the bound it reached the interpreter's
  4300-digit conversion limit as an untyped `ValueError`. From `require_fill_amount`
  through `derive_quote_quantity` to `NormalizedFill`, every refusal is an
  `ExchangeSchemaError`. It is the exchange counterpart of `require_price`.
- **`raw_payload` is `encode_raw_payload(fill_object)`**: canonical JSON, keys sorted, no
  whitespace. The promise is that every `Decimal`'s sign, digits and exponent survive and
  every other leaf keeps its type, so `decode_json(encode_raw_payload(d)) == d` and
  `0.00012300` comes back as `0.00012300`. It is a promise about the number rather than the
  text: `1.5e1` decodes to `Decimal("15")` and is written `15E0`, because a bare `15` would
  decode as an `int`. Pass **the venue's fill object, never the envelope or the request**
  -- those are where a key or a signature could be. An object nested more than
  `MAX_RAW_PAYLOAD_DEPTH` (32) levels is an `ExchangeSchemaError`, at the same depth on
  every platform; anything `decode_json` could not have produced is a `TypeError`, a
  provider bug rather than a vendor's.

Timestamps: `datetime_from_epoch_ms(value)` takes an `int` or a digit string and returns an
aware UTC `datetime` as `EPOCH + timedelta(milliseconds=value)`. The obvious
`datetime.fromtimestamp(ms / 1000)` is a float division in `providers/` and fails the ban.
`epoch_ms(moment)` is the inverse for building a request, and refuses a naive `datetime`.

**Milliseconds are the granularity of the seam.** A venue is asked in epoch milliseconds --
`epoch_ms` floors -- and answers in them, so a window bound with microseconds would be sent
as the start of its millisecond, and a venue correctly returning a fill from earlier in that
millisecond would have its page refused for answering outside a window it was never told
about. So `FillWindow` refuses a bound that is not a whole millisecond (`ValueError`),
`ExchangeCapabilities` refuses a `max_query_window` or `retention` that is not a whole
number of milliseconds, and `clamp_to_retention` floors `effective_since`. Build every bound
with `floor_to_millisecond(moment)`, which keeps the zone and moves the instant back to the
start of its millisecond: for the start of a window, flooring asks for slightly more and
never less. Fills from `datetime_from_epoch_ms` are already on the grid, so fills and bounds
compare on one grid.

### The page contract, enforced by construction

A provider parses its response into `NormalizedFill`s and hands them to
`assemble_fill_page(window, fills, capabilities=..., cursor=..., next_cursor=..., symbol=...)`.
It is the `align_balances` of this seam:

| Case | Outcome |
|---|---|
| the window is longer than `max_query_window` | `ValueError` -- the caller's mistake |
| `symbol` given and not `requires_symbol`, or missing and required | `ValueError` |
| a fill executed outside `[since, until)` | `ExchangeSchemaError` -- an answer about something not asked |
| `symbol` given and a fill is for another symbol | `ExchangeSchemaError` -- the same, per symbol |
| two fills in the page share an `external_trade_id` | `ExchangeSchemaError` |
| more fills than `page_size` | `ExchangeSchemaError` |
| `cursor` or `next_cursor` that does not encode as UTF-8 | `ExchangeSchemaError` -- it would fail while signing the next request |
| `next_cursor` equal to `cursor` (and not `None`) | `ExchangeSchemaError` -- pagination stopped advancing |

`FillWindow(since, until)` is **half-open**: a fill at `since` is in and one at `until` is
not, so windows laid end to end count no instant twice. The last row is also available on its
own as `require_cursor_advanced(cursor, next_cursor)`. It catches a venue repeating a cursor;
it cannot catch one cycling between two, which needs the history only the sync loop has:
#15's sync keeps the cursors it has sent within one window in one run and refuses a
`next_cursor` it has already sent, as a schema error.

`clamp_to_retention(requested_since, now=..., capabilities=...)` returns a `RetentionClamp`
with both instants and a derived `clamped`: `effective_since = max(requested_since,
now - retention + RETENTION_MARGIN)`, never later than `now`, floored to the millisecond.
`clamped` means the request was moved forward; a request that was only floored is not
clamped. **It never raises for a
request older than retention**; surfacing both dates is #13's criterion, decided here once.
`RETENTION_MARGIN` is five minutes and **a guess**: without it the oldest window is at the
edge when computed and past it when the request lands.

### The balance contract, enforced the same way (#104)

`fetch_balances()` answers what the venue's **spot account** holds now: one `AssetBalance`
per asset, the total held, zeros left out, sorted by asset. It exists so that the quantity
the accounting engine replays from fills can be compared with what is actually held
(`docs/specs/025-holdings-reconciliation.md`). The sync calls it after an account's fills
have synced; no request path does.

`AssetBalance(asset, quantity)` refuses, as `NormalizedFill` does and through the same
check, what `NumericText(FILL_SCALE)` would transform: a `quantity` that is not a finite
`Decimal`, has more than `MAX_FILL_INTEGER_DIGITS` digits before the point, or more than
`FILL_SCALE` after it. It also refuses a negative `quantity`, and an `asset` that is blank
or does not encode as UTF-8. Zero is accepted. `asset` is kept exactly as given: a provider
whose venue spells an asset differently in its balances than in its fills normalises before
building one, because the comparison joins the two by name.

A provider hands what it parsed to `assemble_balances(items)`:

| Case | Outcome |
|---|---|
| two entries name the same asset | `ExchangeSchemaError` |
| an entry whose quantity is zero | dropped |
| everything else | kept, sorted by asset |

**A duplicate is refused, not summed.** Whether a venue naming an asset twice means two
parts of one holding or one holding listed twice cannot be told, and the two answers differ
by the whole balance. The check runs before the zeros are dropped. The message names no
asset, no amount and no count.

**The total is the provider's decision**, recorded in its section below: which of a venue's
fields are added. The rule for a field that may or may not overlap another is to leave it
out. The comparison treats what is held as a lower bound, so a balance read too low can
hide a finding and can never invent one.

**Only the spot account is read.** Earn, futures, margin and funding accounts are not, at
either venue.

### The error taxonomy sits inside the existing hierarchy

Seven classes in `providers/exchanges/errors.py`, each also the `ProviderError` subclass
whose meaning it shares, so `except ProviderUnavailableError` still means "transient" and
sees an exchange outage too. `except ExchangeError` catches everything an exchange provider
may raise.

| Class | Also a | Retry? | Default statuses |
|---|---|---|---|
| `ExchangeUnavailableError` | `ProviderUnavailableError` | later | 408, any 5xx, a transport failure |
| `ExchangeRateLimitedError` | `ProviderRateLimitedError` | later, after `retry_after_ms` | 429 |
| `ExchangeAuthError` | `ProviderResponseError` | no -- fix the key | 401, 403 |
| `ExchangeInsufficientScopeError` | `ExchangeAuthError` | no -- grant read permission | none; a venue maps its own code |
| `ExchangeInvalidRequestError` | `ProviderResponseError` | no | any other 4xx |
| `ExchangeRetentionWindowError` | `ExchangeInvalidRequestError` | #15 clamps further | none; a venue maps its own code |
| `ExchangeSchemaError` | `ProviderResponseError` | no | anything unclassified, a 200 with an unmapped code included |

**No constructor except `ExchangeSchemaError`'s takes a message.** Each class builds its
message from a fixed per-class summary plus `(HTTP <status>, venue code <code>)`, and its
constructor has no parameter free text could be passed through -- so "an auth error never
includes the response body" is a property of the type, not a convention at every raise. The
venue's `msg` field is never carried anywhere: it is exactly the field that echoes request
parameters. `ExchangeSchemaError(detail)` is the exception, because a parser has to say which
field was wrong; **a detail names a field and a rule, never a value.**

**A venue code is carried only if it cannot be anything else.** `venue_code_of(raw)` keeps an
`int` or a string of one to ten ASCII digits, optionally negative, and returns `None` for
everything else -- a `bool`, a longer number, a string with any other character. Every
exception constructor passes its code through the same function. Ten digits cannot be a key,
a signature or an address.

### The error map is data, and the lookup order is fixed

A venue declares what differs from the defaults, once, at import:

```python
ERROR_MAP: Final = build_error_map(
    {
        (None, "11111"): ExchangeAuthError,
        (400, "22222"): ExchangeRetentionWindowError,
    }
)
```

The codes above are placeholders, not either venue's. `build_error_map` refuses a status
outside 100-599, a code `venue_code_of` would change, the key `(None, None)` and a value that
is not a strict `ExchangeError` subclass -- a `ValueError` when the module loads, not a
misclassification in production -- and returns a read-only mapping.

`classify_error(status, venue_code, error_map)` resolves in this order, first match wins:

1. `(status, code)` -- exact;
2. `(None, code)` -- the code under any status, for in-band errors that arrive on a 200;
3. `(status, None)` -- the venue's own reading of a status;
4. `STATUS_FALLBACKS`: 401 and 403 are auth, 408 unavailable, 429 rate-limited;
5. any other 4xx is an invalid request, any 5xx unavailable;
6. anything else -- a 200 with an unmapped code, a 3xx -- is a schema error.

Steps 4 to 6 are the same for every venue, so a venue's map lists only what differs. 403
defaults to auth rather than scope because a CDN block and an IP allowlist also arrive as 403
and "fix the key" covers them. Step 6 is deliberate: an in-band code nobody mapped is an
answer we do not understand, and it fails loudly rather than retrying.

A provider raises the result of
`exchange_error(status, venue_code, error_map=..., retry_after_ms=...)`, which takes no body,
no message and no URL, by signature. Parse `Retry-After` with the existing
`parse_retry_after` and pass it unconditionally; only `ExchangeRateLimitedError` keeps it.

**Never call `response.raise_for_status()` in an exchange provider, and never chain `from`
an `httpx.HTTPStatusError`.** Its message is `Client error '401 Unauthorized' for url
'...'` with the **full** URL -- path and query string, and for a venue that signs in the
query string, the signature and the key that produced it. Review demonstrated that
signature reaching a JSON log line: `logger.exception` renders the traceback through
`format_exc_info`, and a chained cause is part of the traceback, so the message this
taxonomy keeps free of the body still carries the URL one link down the chain. Read
`response.status_code` and build the error with `exchange_error`; chain `from` the
`httpx.TransportError` for a transport failure, whose message carries no query, and
`from None` everywhere else. `decode_json` raises nothing but `ProviderResponseError` -- a
number too large for `Decimal` included, since #12 -- and a provider translates that to
`ExchangeSchemaError` `from None` as well: the cause is a parser error about the body, and
the body is what an exchange provider must not repeat.

### Signing and credentials

`hmac_sha256_hex(secret, message)` and `hmac_sha256_base64(secret, message)` in
`providers/exchanges/signing.py` take the secret as a `SecretStr` and unwrap it inside, so no
provider holds the raw secret in a local. Both UTF-8 encode key and message; hex is lower
case and Base64 is the standard padded alphabet. They are verified against RFC 4231's
published vectors, which confirms the primitive and nothing about any venue: **which string a
venue signs, and which encoding it wants back, each venue's section records**, with golden
vectors computed outside the code. Bitget signs a pre-hash and wants Base64; BingX signs its
sorted query and wants hex. A signature authorises its request for the length of the receive
window, and BingX carries it in the query string -- which is why the transport logs
`request_target` and never a path or a query.

`Credentials(api_key, api_secret, passphrase=None)` in `providers/exchanges/credentials.py`
holds every field as a `SecretStr`, the API key included, because rule 3 names API keys. It
refuses a plain `str` (`TypeError`) and a blank value (`ValueError`), naming the field and
never the value. Its `__repr__` and `__str__` are fixed --
`Credentials(api_key=<redacted>, api_secret=<redacted>, passphrase=None)` -- and say whether a
passphrase exists, which is configuration rather than a secret. **Credentials are read from
the environment, never persisted, never returned by an endpoint and never logged.** No column
of either exchange table holds secret material, and a test walks both tables and every
dataclass the seam returns to keep it that way.

### Exchanges in the database

Migration `0006_exchanges` creates two tables.

- **`exchange_accounts`**: `user_id` (cascade from `users`), `exchange_key` (checked against
  the two venues), `created_at`, and `UNIQUE (user_id, exchange_key)` -- one set of
  credentials per venue in the environment means one account per venue. #15's
  `0007_exchange_sync` adds the sync state: `sync_status`, `requested_since`,
  `effective_since`, `planned_until` and `last_synced_at`.
- **`exchange_fills`**: every `NormalizedFill` field, plus `ingested_at` (our clock, beside
  the venue's `executed_at`). The four amounts are `NumericText(18)`.
  `UNIQUE (exchange_account_id, external_trade_id)` as `uq_exchange_fills_account_trade` is
  what #15's `ON CONFLICT DO NOTHING` stands on. Since `0007_exchange_sync` two triggers make
  the table append-only: an `UPDATE` or a `DELETE` of a fill is aborted by the database.

Three decisions worth knowing before adding a column:

- **`CHECK (external_trade_id <> '')` is what makes the unique constraint mean anything.** Two
  empty ids collide, and under `ON CONFLICT DO NOTHING` the second fill vanishes.
- **No `CHECK` on an amount, deliberately.** `quantity > 0` on a `TEXT` column is a comparison
  SQLite performs by numeric affinity -- the float coercion rule 2 forbids, inside the
  database. Signs and scale are enforced by `NormalizedFill`, in Python, where they are exact.
- **The account foreign key is `ON DELETE RESTRICT`.** Fills are the history a cost basis is
  computed from; deleting an account must not take that history with it.

`NumericText`'s too-large refusal stopped quoting the amount in the same change: it named the
value while the type only held prices, and a fill quantity is the owner's holdings.

### Bitget, the first venue (#13)

`providers/exchanges/bitget.py` holds `BitgetProvider`, which reads the owner's spot fills
through Bitget's **Classic (v2) API** with a signed, read-only key, and
`providers/exchanges/registry.py` holds `exchange_providers(client, *, settings=None)`, the
table of configured venues. The module docstring is the reasoning; this section is the record.
Since #104 it reads the spot account's balances as well; "The balance read", at the end of
this section, is that record.

#### Bitget at a glance

The endpoints are read from `providers/exchanges/bitget.py`; the statuses are the ones the
subsections below establish.

| | What the code uses or assumes | Status |
|---|---|---|
| fills | `GET https://api.bitget.com/api/v2/spot/trade/fills`, signed, no `symbol`, labelled `exchange_fills` | **confirmed** 2026-09-25, Get Fills |
| symbol info | `GET /api/v2/spot/public/symbols?symbol=X`, unsigned, once per symbol per provider instance, labelled `exchange_symbol` | **confirmed** 2026-09-25, Get Symbol Info |
| balances | `GET /api/v2/spot/account/assets?assetType=hold_only`, signed, labelled `exchange_balances` | **confirmed** 2026-10-01, Get Account Assets |
| rate limit | fills: 10 a second per UID, and 6000 a minute per IP overall. Symbol info: 20 a second per IP. Balances: 10 a second per UID | **confirmed** 2026-09-25 and 2026-10-01. The transport's one request a second per host is what paces |
| retention | 90 days, declared as such | **confirmed** 2026-09-25. Whether `40704`'s "last three months" is shorter is **unverified**: a `40704` on a first sync's oldest window would show it, and the owner's first sync is that check |
| query window | 90 days documented, 30 declared | **confirmed** 2026-09-25 |
| page size | 100 | **confirmed** 2026-09-25 |
| account type | Classic, which v2 is documented for. A UTA account reads `GET /api/v3/trade/fills` | the owner's account **measured** Classic 2026-09-25, in the app. UTA support is #76 |
| live answers | none seen from this repository: written from the documentation alone | **unverified**. The inclusive bounds, the order within a page, the fee's sign, BGB fees and `cTime`'s unit are designed around, below. The owner's first sync is the first measurement |

#### Confirmed against Bitget's documentation on 2026-09-25

Every old `https://www.bitget.com/api-doc/...` URL now redirects to the UTA introduction. The
Classic documentation lives under `/docs/catalog/classic-*` and `/docs/classic/*`, with a
static copy under `/legacy-docs/classic/...` whose content matches.

| Fact | Documented as | Source |
|---|---|---|
| endpoint | `GET /api/v2/spot/trade/fills` on `https://api.bitget.com` | Get Fills, REST intro |
| parameters | `symbol`, `orderId`, `startTime`, `endTime`, `limit`, `idLessThan`, **all optional** (`symbol` "changed from required to optional" on 2025-03-27, per the classic changelog) | Get Fills |
| cursor | `idLessThan` takes the **`tradeId`** and "requests the content on the page before this ID (older data)" | Get Fills |
| filters compose | "the verification order for returned results is: `id` > `startTime` + `endTime` > `idLessThan`": the range narrows first, the cursor pages inside it | Classic intro |
| page size | `limit` defaults to 100, at most 100 | Get Fills |
| span | "The interval between startTime and endTime must not exceed 90 days" | Get Fills |
| retention | "It only supports to get the data within 90days" -- 90 days, older data only as a download from the website | Get Fills |
| rate limit | 10 requests/s per UID (1/s for a copy-trading trader); 6000/min per IP overall, and exceeding that "takes 5 minutes to recover" | Get Fills, REST intro, FAQ Q9 |
| envelope | `{"code": "00000", "msg": "success", "requestTime": ..., "data": [...]}`; `data` an array, no cursor object | Get Fills |
| fill fields | all strings: `userId`, `symbol`, `orderId`, `tradeId`, `orderType`, `side`, `priceAvg`, `size`, `amount`, `feeDetail{deduction, feeCoin, totalDeductionFee, totalFee}`, `tradeScope`, `cTime`, `uTime` | Get Fills |
| headers | `ACCESS-KEY`, `ACCESS-SIGN`, `ACCESS-TIMESTAMP` (epoch milliseconds), `ACCESS-PASSPHRASE`, `Content-Type: application/json`, `locale` (`en-US` or `zh-CN`) | REST intro |
| signature | pre-hash `timestamp + METHOD + requestPath + "?" + queryString + body`, body empty for a GET; HMAC-SHA256, **Base64** | REST intro |
| clock | the timestamp must be "within 30 seconds of the API server time"; `40008` expired, `40005` invalid | REST intro, error codes |
| error codes | every code in `BITGET_ERROR_MAP`, with its message; **no code is tied to an HTTP status** | error codes |
| deploy errors | `45001`, `40725`, `40808`, `40015` occur during the Tuesday and Thursday releases, and "users can retry" | FAQ Q13 |
| symbol info | `GET /api/v2/spot/public/symbols?symbol=X`, public, 20 requests/s per IP, answering `baseCoin`, `quoteCoin` and `status` (`offline`, `gray`, `online`, `halt`) | Get Symbol Info |

Sources, each read on 2026-09-25:

- Get Fills: https://www.bitget.com/docs/catalog/classic-spot-trade/classic-spot-trade#get-fills
  (static copy: https://www.bitget.com/legacy-docs/classic/spot/trade/Get-Fills)
- REST intro: https://www.bitget.com/docs/classic/rest-api
- error codes: https://www.bitget.com/docs/classic/error-code/restapi
- Classic intro: https://www.bitget.com/docs/classic/Introduction
- Get Symbol Info: https://www.bitget.com/docs/catalog/classic-spot-market/classic-spot-market#get-symbol-info
- UTA upgrade guide: https://www.bitget.com/docs/classic/uta-api-upgrade-guide
- UTA auto-migration notice: https://www.bitget.com/support/articles/12560603893840
- FAQ: https://www.bitget.com/docs/classic/faq

#### Not documented, and treated as not known

Each of these is a question the documentation leaves open. The provider is written so that
every one of them either cannot matter or fails loudly, never so that a guess decides it.

| Not documented | What the provider does |
|---|---|
| whether `startTime` and `endTime` are **inclusive** | widens the request by a millisecond and drops the two edge milliseconds after parsing: right under all four readings |
| the order of fills **within a page** | the next cursor is the smallest `tradeId` on the page, which is right under any order |
| that `tradeId` is numeric, or unique across symbols | requires canonical digits of at most 64 bits; anything else fails the page. A collision within a page is refused; across windows, #15 refuses a same-id fill that differs as a `conflict` |
| what `size` and `amount` are measured in | base and quote, as the example's arithmetic shows (`13000 x 0.0007 = 9.1`) |
| **the fee's sign** | the REST example is negative for a fee paid; the WebSocket fill channel reports it positive. Negated, and a positive `totalFee` refused |
| what `totalFee` and `totalDeductionFee` mean when the fee is paid in **BGB** | a `deduction` other than `"no"` is refused |
| `cTime`'s unit | described as "Unix second timestamp" while the example is thirteen-digit milliseconds. Read as milliseconds; a seconds value lands in 1970 and the page is refused |
| whether a delisted pair is still answered by the symbol endpoint | `status` lists `offline`, which suggests it is. If not, that page fails |
| any golden signature vector | the documentation's samples use an empty secret and print no output. The tests' vectors are computed outside this code, with `openssl` and the pre-hash of Bitget's official Python SDK |

#### v2 against the Unified Trading Account (UTA)

Bitget has two account systems, and an API key belongs to one of them.

- **Classic "is in maintenance mode and receives only essential updates"** (Classic intro). No
  retirement notice or date for v2 was found. v1 is gone: "we will officially discontinue
  external access to the V1 API on November 28, 2025" (V1 deprecation notice, 2025-09-25).
- **A UTA account reads fills from `GET /api/v3/trade/fills`**, which differs in every
  dimension that matters: `category=SPOT`, an opaque `cursor` taken from the previous response
  in place of `idLessThan`, "the time range between startTime and endTime must not exceed 30
  days", 20 requests/s per UID, and differently named fields. The UTA upgrade guide maps v2
  fills to it and says an existing v2 key "automatically gains UTA access".
- **Whether a UTA account's key can still call v2 is stated only for brokers.** "API Keys for
  UTA Unified Trading Accounts cannot access Classic Account API endpoints" is in a notice
  addressed to broker partners and their clients (Broker UTA API upgrade notice, 2026-06-17).
  For a retail UTA account the documentation implies v3, through the upgrade guide, and does
  not state that v2 is refused. **What a v2 call with such a key returns is not documented**:
  a refusal is expected, but an empty success would be indistinguishable from a window with no
  trades, and would let #15 advance past history it never read. It does not change the
  decision below: the owner's account is Classic, and v2 is documented for Classic. It is why
  the operator is told to stay Classic, and why a `null` fills `data` is refused.
- **Since 2026-09-15 Bitget has been migrating eligible Classic accounts to UTA
  automatically**, and an account linked to an API key is not eligible. A main account can
  switch back; a sub-account cannot (the auto-migration notice).
- **The owner's account was confirmed Classic on 2026-09-25**, from the app: separate Spot,
  Futures and Margin tabs, and a banner offering the upgrade.

So v2 is right for this owner, and v2 is all this provider speaks. UTA support is #76, and `docs/operations.md` section 12 tells the operator not to accept the upgrade.

The three sources below were read on 2026-09-25 by the tech lead for spec 014, not re-read
for this section; everything else in this section was:

- V1 deprecation notice: https://www.bitget.com/support/articles/12560603838361
- Broker UTA API upgrade notice: https://www.bitget.com/support/articles/12560603886018
- UTA Get Fill History: https://www.bitget.com/docs/catalog/trading/order-management#get-fill-history
  (static copy: https://www.bitget.com/legacy-docs/uta/trade/Get-Order-Fills)

#### Decisions

**Capabilities.** `retention` is the documented 90 days. **`max_query_window` is 30 days,
below the documented 90, on purpose**: the request is widened by a millisecond, so a 90-day
window would be sent as 90 days and a millisecond, and a venue that states its limit in days
may measure it by the calendar. Thirty days is also UTA's limit, so the follow-up does not
change the number #15 plans around. `page_size` 100, `cursor_kind` `trade_id_before`,
`rate_limit` 10 per 1000 ms (declarative; the transport's floor of one request a second per
host is stricter), `requires_symbol` false and `candidate_symbols()` empty -- `symbol` is
optional, so one query covers every symbol. If "90 days" turns out to be three calendar
months, the oldest window is refused with `40704`, which is `ExchangeRetentionWindowError`,
and #15 steps that window a day later, up to three times per window per run.

**The request.** Query keys in ascending order -- `endTime`, `idLessThan` (only with a
cursor), `limit=100`, `startTime` -- every value ASCII digits, and the URL sent with exactly
the string that was signed, never through `params=`. `startTime = epoch_ms(since) - 1` (never
below 0) and `endTime = epoch_ms(until)`. The fills call sends the four `ACCESS-*` headers,
`Content-Type` and `locale: en-US`; the symbol call sends only `locale` and no credential.

**The window is widened, then filtered.** Sending `[since, until)` as it stands loses the fills
at `since` if `startTime` is exclusive; sending `until - 1` loses the ones at `until - 1` if
`endTime` is exclusive -- the same fills, at the same boundary, on every run. Asking for a
millisecond more on each side covers `[since, until)` under all four readings. After parsing,
a fill at exactly `since - 1 ms` or exactly `until` is dropped: under an inclusive reading it
belongs to a neighbouring window, which fetches it. A fill anywhere else outside the window is
not dropped, and `assemble_fill_page` refuses the page. The page-size check counts the
**raw** page, before the drop, so a 101-fill page cannot hide behind a dropped edge fill.

**The cursor is the smallest trade id, and it must strictly decrease.** `idLessThan` takes a
`tradeId`, never an `orderId`. `next_cursor` is the smallest `tradeId` on a raw page of 100
fills and `None` on a shorter page. With a cursor, every fill must have `tradeId < cursor`, or
the page is an `ExchangeSchemaError`: the venue ignored the cursor. Each cursor is then strictly
below the one before it, and a strictly decreasing sequence of positive integers is finite,
so **pagination terminates by construction** -- a repeated cursor and a cycle alike, which
`require_cursor_advanced` alone cannot promise. A `tradeId` must match `\A[1-9][0-9]{0,18}\Z`
and be at most `2**63 - 1`, so string and numeric identity agree and `int()` never sees more
than nineteen digits.

**The fee is negated.** The documented example buys 0.0007 BTC with `totalFee` `-0.0000007`
BTC -- 0.1%, a fee paid, reported negative -- and `NormalizedFill` counts a fee paid as
positive, so `fee_amount = -totalFee`, computed with `copy_negate()` so the calling thread's
decimal context cannot round it, and a zero fee is a positive zero. **A positive `totalFee` is
refused**: the WebSocket channel reports the field positive, and if REST ever did, reading it
as a rebate would record every fee as income without a word. A real rebate needs a rule
written from a real fill.

**BGB deduction is refused.** A `feeDetail.deduction` other than `"no"` fails the page, naming
the field. An owner who pays fees in BGB sees every page fail until a real fill shows what the
fields hold then; a loud failure is the intended direction.

**The symbol cache.** `NormalizedFill` needs a base and a quote asset, and `"BTCUSDT"` does not
say where one ends. Splitting it with a list of quote coins is a guess that fails on the first
new one, so the public symbol endpoint is asked -- once per distinct symbol per provider
instance, labelled `exchange_symbol`. A symbol must match `\A[A-Z0-9]{1,40}\Z` **before** a
URL is built from it. The answer must hold exactly one entry, for the symbol asked, with
non-blank `baseCoin` and `quoteCoin`; an answer about another symbol is refused, not used.
**An auth refusal from this endpoint is reported as `ExchangeUnavailableError`**, with its
status and venue code kept: the answer is classified like the fills call's, so a 401 or 403
would otherwise be `ExchangeAuthError`, and #15 marks the account `auth_failed` for that. No
credential is sent to this public endpoint, so the refusal cannot be about the key; a CDN or
firewall block is the plausible cause, and it is transient.

**Classification.** A success is HTTP 200 **and** a JSON object whose `code` is the string
`"00000"`. Any other status is `exchange_error(status, code)`, with the code read from the body
only when the body is a JSON object -- a 502 carrying HTML is unavailable, not a schema error.
A 200 whose body does not parse is a schema error; a 200 with any other code goes through the
map, and an unmapped one is a schema error. `raise_for_status()` is never called. A response
that declares `Content-Encoding: gzip` or `deflate` and whose body does not decompress makes
`client.get` raise `httpx.DecodingError` -- a `RequestError`, **not** a `TransportError` --
before any status is known; it is `ExchangeUnavailableError` with no cause and no context,
since a corrupt compressed body from an intermediary is most plausibly transient. (The chain
and price providers still let it escape; that is #75.)

**A fills answer whose `data` is `null` is refused, and that was decided twice.** The
documented empty result is `"data": []`; `null` under `"00000"` is not documented. Reading it
as an empty page was tried and reversed on review. If the venue ever answered that way to a
call it should have refused -- a key on an account upgraded to UTA is the plausible case, and
what v2 returns to one is not documented -- every window would read as empty, #15 would
advance its checkpoints past them, and once they aged out of the 90-day retention the history
would be gone. A refusal costs one fix after the first real sync; a silent empty history is
permanent. The symbol-info answer is refused for a `null` `data` as well.

| Codes | Class | Why |
|---|---|---|
| `40006`, `40037`, `40041`, `40012`, `40036`, `40009`, `40038`, `40018` | `ExchangeAuthError` | the key, the passphrase, the signature or the IP allowlist: the owner fixes the key |
| `40014`, `40025`, `40040` | `ExchangeInsufficientScopeError` | the key lacks read permission |
| `40008`, `40005` | `ExchangeUnavailableError` | **not auth**: a replayed request or a skewed host clock is not a bad key |
| `429` | `ExchangeRateLimitedError` | the in-band spelling of the throttle |
| `40704` | `ExchangeRetentionWindowError` | "only the last three months": #15 clamps further |
| `00001`, `40705`, `40707`, `40017`, `40019`, `40020`, `40034`, `40102` | `ExchangeInvalidRequestError` | a request built wrongly, mapped so it is not a schema error on a 200 |
| `45001`, `40725`, `40808`, `40015` | `ExchangeUnavailableError` | the FAQ says these occur during releases, and to retry |

The header-missing codes (`40001`, `40002`, `40003`, `40011`) are deliberately **not** mapped:
the provider always sends every header, so one of them is a bug here, and the status fallback
says "needs a person" without marking a working key `auth_failed`.

**The transport replays a signed request, and that is accepted.** `RetryingTransport` resends
a GET that got a 429, a 5xx or no answer unchanged -- same timestamp, same signature. Without a
`Retry-After` the backoff is well inside the 30-second window; with one near the transport's
30-second cap the replay can arrive expired, is refused with `40008`, and that is
`ExchangeUnavailableError`, so the next run signs a fresh request. Re-signing per attempt would
need a transport that knows about signing or retries owned by the provider, for a 429 the FAQ
says takes five minutes to clear anyway.

**Credentials.** `PORTFOLIO_BITGET_API_KEY`, `PORTFOLIO_BITGET_API_SECRET` and
`PORTFOLIO_BITGET_API_PASSPHRASE`, all `SecretStr`, **all or none**. The application refuses to
start with a partial set (naming the missing variables), a blank value, a value that does not
encode as UTF-8 (since #14; see the BingX section), or a key or passphrase
holding a character no HTTP header can carry -- whitespace at either end, a control character,
anything outside printable ASCII -- because h11 refuses such a header with a transport error
whose message is the whole value. No refusal names a value. With none set, `exchange_providers`
holds no Bitget provider at all: absent, not built. `BitgetProvider` refuses `Credentials`
without a passphrase, or with an unsendable key or passphrase, and reads the key and the
passphrase out of their `SecretStr` only while the headers are built; the secret is unwrapped
only inside `signing.py`.

**Logging.** The provider has no log call. The transport logs
`https://api.bitget.com/exchange_fills`, `.../exchange_balances` or `.../exchange_symbol`,
never a path, a query or a
header, and every exception the provider raises is built by the taxonomy, which carries no
body, no URL and no header. **No exception it raises has a cause or a context that could hold
one**: an `httpx.LocalProtocolError` -- whose message is the illegal header value, which here
would be the key -- becomes an `ExchangeInvalidRequestError` raised after the `except` block
has closed, since `from None` still leaves the suppressed `__context__` for a debugger or an
error tracker to walk. A transport failure is `ExchangeUnavailableError` chained from the
`httpx.TransportError`, whose message carries no query.

#### The interpreter's limits, and the three `http.py` escapes they found

Spec 012's lesson, applied to every boundary that takes text the venue controls, and probed
against the implementation rather than assumed:

| Input the venue chooses | Where it is stopped |
|---|---|
| an amount of 5000 digits, as a string or a JSON number | `require_fill_amount`'s 100-digit bound, or `decode_json` for a JSON integer |
| a `tradeId` of 5000 digits | the trade-id pattern, before `int()` |
| a `cTime` of 4000 digits, or past `datetime`'s range | `datetime_from_epoch_ms` |
| `1e1000000000000000000`, as a string or a number | `require_fill_amount`, or `decode_json` |
| a fill object nested 1500 levels deep | `encode_raw_payload`'s depth of 32 |
| `"\ud800"` in `symbol`, `tradeId`, `side`, `orderId`, `feeCoin` or `baseCoin` | the ASCII patterns, or an explicit UTF-8 check naming the field |
| a `side`, `feeCoin` or `deduction` that is a list or an object | a type check before any lookup that would hash it |

Every one is an `ExchangeSchemaError` naming the field. The same probes found three defects in
the shared transport that no provider could have caught, because each escaped `client.get`
itself -- and `parse_rate_limit` runs on **every** response:

- a `Retry-After` or `ratelimit-*` value of 5000 ASCII digits passed the grammar check and
  reached `int()`, which raises a bare `ValueError` past 4300 digits. Now a run longer than
  `MAX_HEADER_DIGITS` (10) is unusable, like any other unparseable value;
- a `Retry-After` HTTP-date with a twenty-digit year, hour or zone offset raised
  `OverflowError` from the date parser, which is not a `ValueError`. Now caught and read as
  absent;
- a credential with an illegal header character, above.

#### The balance read (#104)

`BitgetProvider.fetch_balances()` reads the Classic spot account's balances with the same
key, and `bitget.parse_balances(data)` is the pure function behind it.

**Confirmed against Bitget's documentation on 2026-10-01. Nobody has called this endpoint
with a real key**: not this project, not the owner with a probe. Every fact below is the
page's, and the parser refuses what the page does not describe.

| Fact | Documented as | Source |
|---|---|---|
| endpoint | `GET /api/v2/spot/account/assets` on `https://api.bitget.com` | Get Account Assets |
| rate limit | "Frequency limit: 10 times/1s (User ID)" | Get Account Assets |
| key permission | **not stated on the page** | -- |
| parameters | `coin` and `assetType`, both optional. `coin` narrows the answer to one coin. `assetType` is `hold_only` (the coins held), which is the default, or `all`. With both, `coin` wins | Get Account Assets |
| envelope | the usual one, `code` `"00000"`; `data` is an **array**, with no cursor and no paging parameter | Get Account Assets |
| entry fields | `coin`, `available`, `frozen`, `locked`, `limitAvailable`, `uTime`, **all strings** | Get Account Assets |
| `frozen` | frozen assets, usually by a limit order or a Launchpad subscription | Get Account Assets |
| `locked` | locked assets, required of a fiat merchant | Get Account Assets |
| `limitAvailable` | restricted availability, for spot copy trading | Get Account Assets |
| the sample | one entry, its `coin` spelled **lower-case**, `"usdt"`, and every amount `"0"` | Get Account Assets |
| signing | the fills request's headers and pre-hash; nothing endpoint-specific | REST intro, read on 2026-09-25 |

Source, read on 2026-10-01:

- Get Account Assets, static copy:
  https://www.bitget.com/legacy-docs/classic/spot/account/Get-Account-Assets

**Not documented, and designed around:**

| Not documented | What the provider does |
|---|---|
| whether `limitAvailable` is part of `available` or beside it | **does not add it.** Adding it could count the same units twice; leaving it out can only read the balance too low, which is the safe side |
| what the endpoint answers for a **Unified Trading Account** | the owner's account is Classic (#76). A `data` of `null`, or anything that is not an array, is refused; an empty array is "the spot account holds nothing" |
| what `hold_only` answers for an account that holds nothing | read as an empty array, above. The page shows no such sample |
| how a coin's name is spelled: the sample is lower-case, and the names on fills come from symbol info for symbols that are `[A-Z0-9]` | **upper-cases `coin`**, which is right under either spelling. Two entries equal once upper-cased are refused as a duplicate |
| which key permission the endpoint needs | a refusal arrives as one of the mapped codes and is `ExchangeInsufficientScopeError`; nothing is assumed |
| `uTime`'s unit: described as milliseconds, and the sample's value has ten digits | not read |

**Decisions.**

- **The request** is `GET /api/v2/spot/account/assets?assetType=hold_only`. `hold_only` is
  the default and is written out anyway, so that the query is never empty and the request is
  signed exactly as a fills request is: the pre-hash always has its `?`. `coin` is not sent.
  It is labelled `exchange_balances`.
- **`coin` is held to `base.is_asset_name`, the rule BingX's balance `asset` is held to, before it is upper-cased**: 1 to 40 characters, no whitespace anywhere, no Unicode `C*` character. A padded or otherwise unrecognised name is refused, never stripped: kept, `" USDT "` would join nothing and report the whole balance as held with no history.
- **The total is `available + frozen + locked`**, added with `domain.money.add`, which is
  exact. All three are required, and a missing one is not read as zero. Each goes through
  `require_fill_amount`, so a JSON number is accepted as a string is, and a `bool`, a
  `null` or anything else is refused.
- **A negative part is refused**, each on its own, before the sum.
- **No decoding and no rounding.** A total with a nineteenth decimal place is refused by
  `AssetBalance`.
- **Classification is the fills call's**: `unwrap_envelope` and `BITGET_ERROR_MAP`, so a
  revoked key, a missing permission, a throttle and an outage are the classes they are for
  fills. The three `httpx` arms are `_get`'s, shared by all three calls.
- **Logging.** `https://api.bitget.com/exchange_balances`, and nothing else. No message
  names a coin or an amount.

### BingX, the second venue (#14)

`providers/exchanges/bingx.py` holds `BingXProvider`, which reads the owner's spot fills
through BingX's **spot v1 API** with a signed, read-only key, and the registry builds it when
both of its variables are set. The module docstring is the reasoning, and it separates each
fact by source; this section is the record. Since #104 it reads the spot account's balances
as well; "The balance read", at the end of this section, is that record.

**BingX is the venue where the documentation was not enough.** Read in full, it contradicts
itself on whether `symbol` is required and on the largest page. A live probe then contradicted
it on retention and on what an unbounded query returns. So every fact below names its source,
and where the probe and the documentation differ, the probe wins.

#### BingX at a glance

The endpoints are read from `providers/exchanges/bingx.py`. "The probe" is the owner's, below:
a measurement of the live service, on 2026-09-26 and 2026-09-27.

| | What the code uses or assumes | Status |
|---|---|---|
| fills | `GET https://open-api.bingx.com/openApi/spot/v1/trade/myTrades`, signed, no `symbol`, labelled `exchange_fills` | **confirmed** 2026-09-27, V3 and V1; **measured** by the probe |
| balances | `GET /openApi/spot/v1/account/balance`, signed, labelled `exchange_balances` | **confirmed** 2026-10-01, V3. Never called with a real key |
| rate limit | fills: 5 a second per UID. Balances: 5 a second per UID and 3 a second per IP | **confirmed** 2026-09-27 and 2026-10-01; the `X-RateLimit-Requests-*` headers **measured** present by the probe. The transport's one request a second per host is what paces |
| retention | 365 days declared, because the documented 7 days is wrong | the 7 days **disproved** by the probe: older fills came back, and spans to 365 days answered. The year is **unverified**: BingX states one only for its web export, and nothing short of a fill older than a year can settle it |
| query window | 30 days declared; spans to 365 days were accepted | **measured** by the probe |
| page size | 500, the smaller of the two maxima V3 states | the two maxima **confirmed** 2026-09-27; `limit` of 1 and 5 **measured** as honoured |
| bounds | `startTime` and `endTime` both inclusive; the cursor is a time | **measured** by the probe |
| still open | a page past the first, a second symbol in one answer, and history older than the probe's two weeks | **unverified**. The owner's first sync past each is the check; `docs/operations.md` section 14 says how for the second symbol |

#### Three sources, and how each was read

- **V3**, https://bingx-api.github.io/docs-v3/, the current documentation, last deployed
  **2026-09-19**. The site is a single-page application whose content ships in the
  `BingX-API/docs-v3` repository's JavaScript bundle. That bundle is what was read, on
  2026-09-27, for the endpoint, its parameter table, the signing recipe, the error-code
  tables and the changelog.
- **V1**, https://bingx-api.github.io/docs/, the older site, deployed from the `BingX-API/docs`
  repository's `gh-pages` branch on **2026-01-22**. Its `main` branch holds a 2023 build, so
  a reader has to follow the deployed `index.html` to the current bundle. The first reading
  of the error codes for this section made exactly that mistake.
- **The probe**: three read-only scripts the **owner** ran on **2026-09-26** and 2026-09-27
  against the live API with their own read-only key. The third asked exactly as the
  provider does, with both bounds and no symbol, to settle which fills a capped page
  keeps. Nothing in this repository, and no agent, saw the key. The scripts print response
  codes, counts, JSON types, the symbols traded and time relations ("the fill at `T` is
  returned for `[T, T]`"). They never print an amount, an id, a key or a signature. The account held **a few dozen fills in one symbol,
  the oldest under two weeks old**. The scripts and their output stayed on the owner's
  machine. They printed the number of spot symbols and three examples, not the list.

#### What the documentation says, what the probe found, and what is used

| Fact | Documentation | Probe | Used here |
|---|---|---|---|
| endpoint | `GET /openApi/spot/v1/trade/myTrades` on `https://open-api.bingx.com`, signed, key permission "Read" (V3, V1) | answered as documented | yes |
| `symbol` required? | V3: "No". V1: yes | V1 is **wrong**: without `symbol` the answer is code 0, with every fill and a `symbol` on each | never sent |
| retention | "Can only check data within the past 7 days range" (V3, V1) | **wrong**, disproved: a time-bounded query returned fills more than a week old, and spans of 14, 30, 90 and 365 days ending now all answered code 0 with every fill | 365 days, below |
| no time bounds | "the data of the past 24 hours is returned by default" (V3, V1) | **wrong**: it returns from the oldest fill, ascending, and `limit=5` gave the oldest five | bounds are always sent |
| `startTime`, `endTime` | milliseconds (V3); whether inclusive is not stated | **both inclusive**, with a symbol and without one: `[T, T]` returns the fill at `T`; `[T+1, ...]` and `[..., T-1]` do not | `[since, until - 1 ms]` |
| order within a page | "sorted by time field, from smallest to largest" (V3, V1) | ascending by time and by id | the cursor takes the newest millisecond by value, so the order within a page does not matter |
| which fills a capped page keeps | not stated; `fromId`'s "by default, the latest trade will be retrieved" (V3) hints at the newest | **the oldest in range**: `limit` 5 and 2 over a span reaching back past the oldest fill, and 5 over a one-hour burst, returned the oldest fills every time, with both bounds, with `startTime` alone and with `endTime` alone, and without a symbol (the third probe, 2026-09-27) | pages forward from the newest millisecond. **This does matter**: a venue keeping the newest would lose the rest of a full window silently |
| fills sharing a millisecond | not stated | **they occur**: the probe saw fills sharing one millisecond | the next page starts **at** that millisecond, not after it |
| `fromId` | "Starting trade ID" (V3) | inclusive and ascending: `id >= fromId` | not used, below |
| trade id | `int64` (V3); the sample is `36767057` | a JSON integer of about 26 bits, like the sample | namespaced by symbol |
| `limit` | "Default 500, maximum 1000" in the parameter table, and "limit = 500" in the notes, on the same page (V3) | `1` and `5` honoured exactly; `1001` accepted without an error | 500 |
| envelope | `{code, msg, debugMsg, data: {fills: [...]}}`, code 0 on success (V3, V1) | also `retryable`, and `timestamp` on an error; **every error arrived on HTTP 200** with a non-zero integer `code` | yes |
| `commission` | `float64` (V3); the sample is a bare number, `-0.000046483255` | a bare JSON number, negative, in the base asset on buys | rounded, below |
| `quoteQty` | a string (V3); the docs' own sample is `"17.997667582000002"`, float noise | equal to `price x qty` exactly for one fill in three | rounded, below |
| signing | HMAC-SHA256 as "a 64-character lowercase hexadecimal string"; the key in `X-BX-APIKEY`; `signature` appended. V3: "Sort all parameters (business parameters + timestamp) by key in ASCII ascending order". V1: "without sorting" | the V3 recipe was accepted | sorted, and sent exactly as signed |
| wrong signature | `100001` (V3, V1) | `100001` on HTTP 200 | auth |
| timestamp 60 s old | `100421` (V3, V1); the window is "default 5000ms", adjustable with `recvWindow` (V3) | `100421` on HTTP 200 | unavailable |
| no key header | `100413` (V3, V1) | `100413` on HTTP 200 | auth |
| malformed symbol | `100400` (V3, V1) | `100400` on HTTP 200 | invalid request |
| empty window | not stated | code 0 with `data.fills: []`, never `100204` | an empty page |
| server time | "all timestamps in milliseconds" | `/openApi/spot/v1/server/time` answers in **seconds** | not used |
| rate limit | 5 a second for this endpoint, per UID, "each API having its own independent rate limit"; headers `X-RateLimit-Requests-Remain` and `X-RateLimit-Requests-Expire`; throttled requests "restore after 5 minutes" (V3) | the two headers are present | `RateLimit(5, 1000)` |
| key permission | "Newly created API Keys have read-only permission by default" (V3) | -- | keep the default |

Sources, each read on 2026-09-27 unless stated:

- V3, "Query transaction details" (spot, trades): https://bingx-api.github.io/docs-v3/ ,
  bundle `static/js/app.8bc50bc0c8a6a308c3e4.js` in https://github.com/BingX-API/docs-v3
- V3, "Signature Authentication" and "Error Code Reference" (Common and Spot tables): the
  same bundle. https://github.com/BingX-API/api-ai-skills `skills/references/error-codes.md`
  carries the same tables word for word.
- V1: https://bingx-api.github.io/docs/ , bundle `static/js/app.4dcba784df17c52faa47.js` on
  the `gh-pages` branch of https://github.com/BingX-API/docs , including its "Common Error
  Codes" list for spot and its changelog of 2025-10-11.
- BingX support, "How to Generate Statements for Transaction History and Trade History"
  (2022-09-20):
  https://bingx.com/en/support/articles/10681140250393-howtogeneratestatementsfortransactionhistoryandtradehistory

#### Not established, and designed around

| Not established | What the provider does |
|---|---|
| whether trade ids are unique across symbols -- the account traded one symbol | namespaces every id `"{symbol}:{id}"`, and pages by time, which is right under either scheme |
| whether "no `symbol` returns every symbol" holds for an account with two symbols | V3 documents it, and a `symbol` on each fill implies it, but no probe has seen two. The owner's first sync after trading a second symbol is the check -- `docs/operations.md` section 14 says how |
| whether the venue keeps fills older than a year | declares a year, which the owner's history sits well inside |
| what the venue answers for a window older than it keeps | nothing maps to `ExchangeRetentionWindowError`. An unmapped error code is loud; an empty success is not, which is why the declared retention matters |
| which of the two page maxima the venue enforces | asks for 500 and calls exactly 500 full |
| a silent cap below 500 | assumed not to exist: `limit=5` was honoured exactly, and nothing suggests one. A capped page would look complete and lose the rest of its window |

**An empty success is a real risk at this venue, not only a theoretical one.** V3's changelog
of 2026-09-05, for the sibling endpoint `/openApi/swap/v2/user/positions`: when "the position
query service is temporarily unavailable", the API now returns error code `109500` "instead of
code=0 with data=[]". So BingX has, on at least one endpoint, answered a backend failure with
an empty success. Nothing says `myTrades` does or did. If it did, a window would read as
empty, #15 would advance past it, and the fills would be missed until an overlap or a moved
history start re-read them. The provider refuses every *malformed* empty answer -- a missing
`data`, a `null`, a missing `fills` -- but it cannot tell `fills: []` from a quiet window. That
is the residual, recorded here rather than assumed away.

**A renamed pair can duplicate a fill.** The namespaced id embeds the symbol, and BingX
renames a pair when its token migrates (`STRK-USDT` becoming `STRK-OLD-USDT`, say). A fill
read once under the old name and again under the new one gets a second
`external_trade_id`, so it is inserted again instead of meeting #15's collision check,
and the cost basis counts it twice. That needs the rename to fall between two reads of
the same fill, which in practice means the sync's five-minute overlap, or a window
re-read after an interruption. It is recorded, not designed around: the alternative, an
id without the symbol, is the silent loss the namespacing exists to prevent.
`docs/operations.md` has a troubleshooting row for it.

The support article above adds a second caveat to the year: "Some regions and risk-controlled
users are only able to export 30 days of data." That is about the web export, not the API, and
the probe read more than a week back. If the owner's account is ever limited this way and the API
follows, the oldest windows will come back empty, and nothing will say so.

#### Decisions

**Capabilities.** `retention` 365 days, `max_query_window` 30 days, `page_size` 500,
`cursor_kind` `time`, `rate_limit` 5 per 1000 ms, `requires_symbol` false and
`candidate_symbols()` empty.

- **Why 365 days.** It is a declared bound, not a measured one. The documented 7 days is
  disproved, and at 7 the owner's own fills older than a week would never be read. The only longer statement BingX makes is the support article's "records are only
  available for up to one year", about the web export. Declaring more would claim history
  the venue may not return; declaring `None` would promise history back to 2009. With a
  year, `history_truncated` tells the owner that nothing before a year ago is promised,
  which is true.
- **Why 30 days a window.** Spans up to 365 days were accepted, so it is headroom, not a
  limit. A first backfill is 13 windows of one or two requests each.
- The rate limit is declarative; the shared transport's floor of one request a second per
  host is stricter. The transport reads neither `X-RateLimit-Requests-*` header:
  `parse_rate_limit` knows only the `ratelimit-*` and `x-ratelimit-*` trios. At one request a
  second against a budget of five, nothing needs to.

**The request.** The query keys are in ASCII order -- `endTime`, `limit`, `startTime`,
`timestamp` -- every value ASCII digits, then `&signature=<hex>`, last. The string sent is the
string signed, so V3's "sort every key" and V1's "without sorting" are both satisfied. The
signature is `hmac_sha256_hex(secret, query)`, the key goes in `X-BX-APIKEY`, and there is no
other credential header. `timestamp` is the clock in epoch milliseconds; no `recvWindow` is
sent, so the venue's 5000 ms applies. No `symbol`, no `fromId`. The URL is passed whole, never
through `params=`. The base URL is a constant; V3's fallback domain `open-api.bingx.io` ("only
when the primary domain is unavailable", 60 a minute) is not used, because an outage is
`unavailable` and the next run asks again.

**The bounds are `[since, until - 1 ms]`, sent as they stand.** The probe proved both are
inclusive, so `startTime = epoch_ms(since)` and `endTime = epoch_ms(until) - 1` ask for exactly
`[since, until)`. There is no widening and no edge filter, which Bitget needs only because its
inclusivity is undocumented.

**The cursor is a time, because the ids are probably per symbol.** A 26-bit id cannot number
every spot trade on a venue this size, and the docs' own BTC-USDT sample is the same size. If
ids are a sequence per symbol, `fromId` means something different in every symbol's sequence,
and a page from a symbol with low ids would be skipped for good, silently. A time is right
whatever the id scheme, because the venue orders every symbol's fills by time. So:

1. The first request asks `startTime = since`. A later one asks `startTime = cursor`.
2. `m` is the newest fill's millisecond on the page, by value, not the last fill as served,
   so the order *within* a page does not matter. **Which fills a capped page holds does
   matter**, and it is established: the venue keeps the **oldest** in range (the third
   probe, 2026-09-27), so everything after `m` is still unread and paging forward from
   it misses nothing.
3. No fills, or fewer than 500: `next_cursor = None`.
4. 500 fills and `m` after `startTime`: `next_cursor = str(m)`. The next request starts
   **at** `m`, not `m + 1`, because more fills may share that millisecond -- and fills
   sharing a millisecond occur, as the probe saw. The fills at `m` are read twice, and
   #15's unique constraint makes the second read insert nothing.
5. 500 fills all at `startTime`: `ExchangeSchemaError`. More than a page of fills in one
   millisecond cannot be paged past with a time cursor, and a loud failure beats a silent
   loss.
6. A fill before `startTime` is an `ExchangeSchemaError`: the venue ignored the bound.
   `assemble_fill_page` refuses one outside `[since, until)`.

Each cursor is strictly after the one before and never past `until - 1 ms`, so **pagination
terminates by construction**, a repeat and a cycle alike. A cursor from the caller must be
canonical digits, `\A(0|[1-9][0-9]{0,14})\Z`, inside `[since, until - 1 ms]`, or it is a
`ValueError` before any request. #15 drops a `time` cursor when a window's `since` moves, which
costs a re-read that the unique constraint makes free.

**Trade ids are namespaced by symbol:** `external_trade_id = "{symbol}:{id}"`, for example
`KAS-USDT:36767057`. It costs nothing if ids are global, and if they are per symbol it is the
difference between two fills and one silently dropped. The id must be a JSON **integer** from
1 to `2**63 - 1`; `decode_json` returns it as an `int`, never through a float, and it is
compared with the bound rather than parsed. `orderId` follows the same rule, or is `null` or
absent.

**Parsing a fill.** `symbol` is kept as the venue spells it and split on its **last**
hyphen, so no symbol endpoint is asked. The quote after it must be `[A-Z0-9]{1,20}`; the
base before it is 1 to 40 characters with no whitespace, no Unicode `C*` character (control,
format, surrogate, private use, unassigned) and nothing that does not encode as UTF-8, and
anything else is accepted. **The base is wide because BingX's symbols are not all
`BASE-QUOTE` in letters and digits.** The public symbol list, read on 2026-09-27 by the
reviewer and again for this section, holds 38 of 2273 that are not: pairs renamed when a
token migrated (`STRK-OLD-USDT`, `ZK-OLD-USDT`, `H_OLD-USDT`, `PUMP_OLD-USDT`), and `$U-USDT`,
`$1-USDT`, `D.O.G.E.-USDT`, `ATOM(ARC20)-USDT` and `MØTH-USDT`. Every quote in it matches,
and so does every quote `myTrades`' own validation message names (`USDT`, `USD1`, `USDT2`,
`USDC`, `ETH`, `BTC`). The longest base is 17 characters. A rename can turn a pair the owner
holds into one of these, and a narrower rule would fail every page carrying it, forever.
An earlier version of this section said the probe had checked every symbol; it printed
the count and three examples. `isBuyer` must be exactly `true` or `false`. `qty` and `price` go
through `require_fill_amount` and are stored as reported. `time` must be a JSON integer of
epoch milliseconds. `isMaker` is kept in `raw_payload` only. A missing field, or one of the
wrong JSON type, is an `ExchangeSchemaError` naming the field, never the value.

**`commission` and `quoteQty` are decoded from binary floats, by a stated rule.** BingX
produces both from IEEE-754 doubles: `commission` is typed `float64`, the docs' own `quoteQty`
sample is `"17.997667582000002"`, and a real-looking response quoted in the source comments of
CCXT, a third-party wrapper, shows `"quoteQty": "4.9988562000000005"` beside
`"commission": -0.00005820000000000001` -- an illustration, not a source. A double carries 15
significant decimal digits faithfully (`DBL_DIG`); the digits after them are artefacts of the
representation, not information the venue holds. `from_binary_float` rounds to 15 significant
digits, half to even, in an explicit `Decimal` context, and drops the trailing zeros the
rounding leaves:

| Input | Result |
|---|---|
| `"17.997667582000002"` | `17.997667582` |
| `-0.00005820000000000001` | `-0.0000582`, negated to a fee of `0.0000582` |
| `-1.2493e-7` | unchanged: five significant digits |
| `1.000000000000005` | `1` -- the tie goes to the even digit |

Without it a fee like `-0.00005820000000000001` has 20 fractional digits, `NormalizedFill`
refuses it (`FILL_SCALE` is 18), and every page holding one fails on every run. **On a fill
it is applied to those two fields and never to `price` or `qty`**, which are strings the
venue formats exactly: a 19-digit KAS quantity must survive intact. Since #104 it is also
applied to a balance's `free` and `locked`, for the same reason; "The balance read", below,
is that record. It does not contradict "refuse, never
round": that rule is about a column rounding silently, and this is a documented decoding of a
venue's float encoding. A value still finer than `FILL_SCALE` after it is refused as before.
The risk is stated where it lives: a `commission` or `quoteQty` that genuinely needed 16 or
more significant digits would be rounded, by at most half a unit in the 15th.

**The fee is negated, and a positive one refused.** V3's sample and the probe both report a
fee paid as a negative `commission`; `NormalizedFill` counts a fee paid as positive. A
**positive** `commission` is refused, as Bitget's positive `totalFee` is, until a real rebate
shows what one looks like. `commissionAsset` is required unless the fee is zero. A missing,
`null` or `""` `quoteQty` is derived with `derive_quote_quantity` and flagged.

**Classification.** A success is HTTP 200 **and** a JSON object whose `code` is the integer
`0` -- not `false`, not `"0"` -- **and** whose `data` is an object holding a `fills` array.
Any other status is `exchange_error(status, code)`, with the code read from the body only when
it is a JSON object, so a 502 carrying HTML is unavailable. A 200 with any other code goes
through the map, and an unmapped one is a schema error. A 200 with code 0 but no `data`, a
`null` `data`, no `fills` or a `fills` that is not an array is a schema error: **a missing list
is never read as "no fills"**, for the reason spec 014 learned with Bitget's `null`, and which
the 2026-09-05 changelog above makes concrete here. `raise_for_status()` is never called; its
message carries the URL, and here the URL carries the signature.

| Codes | Class | Source, and why |
|---|---|---|
| `100001` signature verification failed, `100412` null signature, `100413` incorrect or missing key, `100419` IP not on the key's whitelist | `ExchangeAuthError` | V3 Common, V1; the owner fixes the key. A signature mismatch is a wrong secret once the golden vectors prove the recipe |
| `100414` "The account is abnormal" (V1, spot), `100441` "Account is abnormal or advanced identity verification is required" (V3, Spot) | `ExchangeAuthError` | the owner has to act on the account. Whether one replaced the other is not stated, so both |
| `100401` `AUTHENTICATION_FAIL` | `ExchangeAuthError` | V1's legacy status list. It can only mean the owner must act, and unmapped on a 200 it would never mark the account `auth_failed` |
| `100004` permission denied | `ExchangeInsufficientScopeError` | V3 Common, V1; the key lacks Read |
| `100421` timestamp mismatch | `ExchangeUnavailableError` | V3 Common, V1, the probe. **Never auth**: the transport replays signed requests, and a skewed clock is not a bad key. V3's Spot table also uses `100421` for "Request rejected", about order placement; unavailable fits every reading |
| `100410` rate limit, `109429` APIRateLimit, `(418, None)` IP banned after a 429 | `ExchangeRateLimitedError` | `100410` V3, V1. V1's changelog of 2025-10-11: "Old error code 100410 has been updated to new error code 109429, meaning: APIRateLimit", from 2025-10-16 -- in a list of futures codes, and V3 lists `109429` under Futures only, so it is mapped defensively. `418` is V3's HTTP table |
| `100500` system busy, `100503` server busy, `109500` system busy | `ExchangeUnavailableError` | V3 Common, V1; `100503` is V1 only. `109500` is V3's code for a sibling endpoint whose backend is down -- its changelog of 2026-09-05, above -- and is mapped defensively, like `109429` |
| `100400` parameter error, `100204` data not found or span too wide, `100404` path not found, `100490` pair offline | `ExchangeInvalidRequestError` | V3, V1; a request this code built. The probe shows an empty window is code 0, so `100204` is never an empty answer |

**`100403` is deliberately unmapped.** V1 calls it `AUTHORIZATION_FAIL`, and V3 uses it for "not
the main account". Two meanings is no meaning, so it takes the fallback: a schema error on a
200, which is loud. **Nothing maps to `ExchangeRetentionWindowError`**, as above.

**The transport replays a signed request, and that is accepted.** A replay older than the
venue's 5000 ms is refused with `100421`, which is `unavailable`, and the next run signs a
fresh request. BingX's window is six times tighter than Bitget's, so this matters more here;
but every error the probe saw arrived on a 200, which the transport does not retry, so the
replay is the rarer path.

**Credentials.** `PORTFOLIO_BINGX_API_KEY` and `PORTFOLIO_BINGX_API_SECRET`, both `SecretStr`,
**both or neither**. The application refuses to start with one of them set and not the other
(naming the missing one), a blank value, a value that does not encode as UTF-8, or a key
holding a character no HTTP header can carry. The secret is not checked for the header
rule; it only ever enters an HMAC. **It is checked for UTF-8**, as every venue's
credentials are: on Linux, environment bytes that are not UTF-8 arrive as lone
surrogates, and the signing helper would then fail with a `UnicodeEncodeError` whose
`args` hold the whole secret. Found by review on #14. BingX keys have no
passphrase, and `BingXProvider` refuses `Credentials` carrying one: a set one means the caller
is confused. With neither set, `exchange_providers` holds no BingX provider at all.

**Logging.** The provider has no log call. The transport logs
`https://open-api.bingx.com/exchange_fills` or `.../exchange_balances`, never a path, a query
or a header -- which is the
control that keeps the signature, carried in the query string, out of the log. Every
exception is built by the taxonomy; the `httpx.LocalProtocolError`, `httpx.DecodingError` and
`httpx.TransportError` arms are Bitget's, for Bitget's reasons.

**Golden vectors.** BingX publishes no vector that reproduces. The tests use two, each computed
outside this code with `openssl dgst -sha256 -hmac 'SECRET_KEY' -hex`: V3's own recipe run
verbatim, and the exact request this provider sends for a fixed clock and window. The fake venue
in the tests also re-verifies every request's signature over the bytes it received.

#### The balance read (#104)

`BingXProvider.fetch_balances()` reads the spot account's balances with the same key, and
`bingx.parse_balances(balances)` is the pure function behind it.

**Confirmed against BingX's documentation on 2026-10-01, and against nothing else. Nobody
has called this endpoint with a real key**: the owner's probes of 2026-09-26 and 2026-09-27
asked about fills only. So this subsection has one source column where the fills table has
two, and none of it has the standing of a fact the probe saw.

| Fact | Documentation | Used here |
|---|---|---|
| endpoint | `GET /openApi/spot/v1/account/balance` on `https://open-api.bingx.com`, "Query Assets", signed, for master and sub accounts (V3) | yes |
| key permission | "API Key Permission: Read" (V3) | the key's default |
| rate limit | `5/second` per UID, and an IP limit of `3` (V3: the bundle's `rate-limitation` and `ip-rate-limitation`). The vendor's reference: "5/s per UID; 3/s per IP" | the transport's one request a second per host is under both |
| parameters | `timestamp` required, `recvWindow` optional (V3). The page's two descriptions are swapped, and its sample sends a `recvWindow` of `60000` where the vendor's reference caps it at 5000 | `timestamp` alone; no `recvWindow`, as for fills |
| envelope | `{code, msg, debugMsg, data: {balances: [...]}}`, code 0 on success (V3) | the fills envelope, with `balances` for `fills` |
| entry fields | the response table lists only `balances`, an array; the entries are the sample's, `{asset, free, locked}`, **all three strings** (V3). The vendor's reference types all three `string`: `free` the available balance, `locked` the locked one | `free + locked` |
| the amounts | the sample's `locked` is `"244.18616265388994"`: **seventeen significant digits, float noise**, as `quoteQty`'s sample is. Its integers reach fifteen digits | decoded, below |
| zero balances | the sample lists an asset whose `free` and `locked` are both `"0"` | dropped |
| the fund account | "Query Fund Account Assets", `GET /openApi/fund/v1/account/balance`, 2 a second, an optional `asset`, and **the same sample response, digit for digit** (V3) | **not read**, below |

Sources, each read on 2026-10-01:

- V3, "Query Assets" (Spot, Account Endpoints):
  https://bingx-api.github.io/docs-v3/#/en/Spot/Account%20Endpoints/Query%20Assets , in the
  bundle `static/js/app.8bc50bc0c8a6a308c3e4.js` of https://github.com/BingX-API/docs-v3 --
  the bundle the fills section was read from, last committed on 2026-09-19. The bundle holds
  the page twice, alike except for which error-code table each links.
- V3, "Query Fund Account Assets": the same bundle.
- The vendor's reference, https://github.com/BingX-API/api-ai-skills ,
  `skills/spot-account/api-reference.md` and `skills/fund-account/api-reference.md`. It is
  BingX's own repository, and it is cited for the two things the V3 page leaves out: the
  entry fields' types, and the IP limit's unit.

**Not established, and designed around:**

| Not established | What the provider does |
|---|---|
| whether the **spot account and the fund account are one account or two** | **reads the spot endpoint only.** The two pages show the same sample, and the vendor's reference calls the spot endpoint's account "the fund (spot) account", which suggests one. Nothing states it. Reading both could count the same units twice; reading one can only read too low, which is the safe side |
| that **the venue holds no balance finer than 18 decimal places** | decodes the float noise away, below, and refuses what is still finer. A balance held to 18 places or coarser is always recovered: below `0.0001` it has at most fourteen significant digits, and the decode keeps fifteen. One the venue really holds finer -- below `0.0001` with fifteen real significant digits, which is nineteen places or more -- **fails the whole balance read with `schema`**, on every run, until a rule is written from a real answer. The provider does not round to the column |
| whether a backend failure can arrive as **an empty success** | a missing, `null` or non-array `balances` is refused. `balances: []` cannot be told from an empty account and reads as one. The 2026-09-05 changelog entry above is why this is listed; it under-reads, which is the safe side |
| whether a **demo or virtual token** can be listed with a balance | the sample lists `VST`, at zero. Nothing is excluded by name. If a real account reports one above zero, it shows in the holdings check as held with no history, and the first real case gets a rule |
| that a balance's `asset` is spelled as a fill's base asset is | takes it as reported, no case changed, and holds it to the base-asset rule. A different spelling would split one asset in two in the comparison, visibly |
| which error codes are specific to this endpoint | none is added. One copy of the page links the Spot error table, which the map was built from; the other links the Account / Wallet / Agent table, which was not read for this section. An unmapped code on a 200 is a `schema` error, which is loud |

**Decisions.**

- **The request** is `GET /openApi/spot/v1/account/balance?timestamp=<ms>&signature=<hex>`:
  `timestamp` is the only key, so it is trivially in ASCII order, the signature is
  `hmac_sha256_hex(secret, "timestamp=<ms>")` appended last, and the key goes in
  `X-BX-APIKEY`. It is the fills recipe with nothing changed, built by the same method. It
  is labelled `exchange_balances`.
- **The asset is the venue's spelling**, held to the rule a fill's base asset is: 1 to 40
  characters, no whitespace, no Unicode `C*` character. A fill's `base_asset` is what
  precedes the last hyphen of its symbol, as reported, so the two meet under one name.
- **`free` and `locked` are decoded from binary floats**, each on its own, with
  `from_binary_float`: the rule and the reason are `quoteQty`'s, above. Without it one
  entry such as `"0.000012340000000000001"` -- twenty-one places -- is finer than
  `FILL_SCALE` and fails the whole read, every run. With it that entry is `0.00001234`. The
  cost is the stated one: a part that truly needed sixteen significant digits is moved by at
  most half a unit in the fifteenth, which is twelve orders of magnitude inside the one
  percent the comparison tolerates. Bitget's amounts are exact strings and are not decoded.
- **The total is `free + locked`**, added with `domain.money.add`, which is exact. Both are
  required. Each goes through `require_fill_amount`, so a JSON number is accepted as a
  string is.
- **A negative part is refused**, each on its own, after the decode and before the sum.
- **Classification is the fills call's.** `unwrap_envelope` takes a `member` -- `"fills"` by
  default, `"balances"` here -- and is otherwise the same function, with the same map: a
  success is HTTP 200, code `0`, and a `data` object holding that array.
- **Logging.** `https://open-api.bingx.com/exchange_balances`, and nothing else: the
  signature is in the query string here too. No message names an asset or an amount.

### Not confirmed, and who confirms it

Nothing vendor-specific is in #12, by design. Each of these was belief, and the issue named
beside it replaced the belief with the venue's documentation. #13 did so for Bitget **from
documentation alone**: no request has been made to its signed endpoint, because that needs a
key this repository must not contain. #14 did so for BingX from its documentation **and** from
the owner's read-only probe of the live API, because the documentation contradicts itself;
the probe ran on the owner's machine, and no key came near this repository. For both, the
owner's first sync with #15 is the first run of the provider itself against the venue.

| Belief | Bitget, after #13 | BingX, after #14 | Still open for |
|---|---|---|---|
| venues report errors as numeric codes (`venue_code_of` drops anything else, and classification falls back to the status) | **confirmed**: five-digit strings, `"00000"` on success, plus `429` | **confirmed**: JSON integers, `0` on success, six digits for an error | -- |
| a venue may report failures, auth included, on a 200, where an unmapped code is a schema error | Bitget's map is keyed by code under any status, so a mapped code on a 200 is classified too | **confirmed by the probe**: every error it saw arrived on a 200. The map is keyed by code, auth codes included | -- |
| timestamps are epoch milliseconds | **confirmed** for `ACCESS-TIMESTAMP`, `startTime` and `endTime`; `cTime` is documented both ways and read as milliseconds (a seconds value fails the page) | **confirmed** for `timestamp`, `startTime`, `endTime` and `time`; the server-time endpoint answers in seconds, and is not used | -- |
| each venue pages in one of the four `CursorKind` shapes | **confirmed**: `trade_id_before`, over `idLessThan` | `time`, over `startTime`: `fromId` exists, but would be wrong silently if ids are per symbol | -- |
| each venue's retention, maximum query window, page size and rate limit | **confirmed**: 90 days, 90 days (30 declared, on purpose), 100, 10/s per UID | the documented 7 days **disproved**; 365 declared as a bound. Spans to 365 days accepted (30 declared). 500 of two stated maxima. 5/s per UID | the owner's first sync past one page |
| trade ids are unique per account across symbols | **not documented**. One cursor pages every symbol, which only works if they are; a collision within a page is refused, and across windows #15 refuses a same-id fill whose accounting fields differ, as a `conflict` that stops the account | **not established**, and probably not: ids are namespaced `"{symbol}:{id}"`, and the cursor is a time | the first account with two symbols |
| which string each venue signs, and in which encoding | **confirmed**: `timestamp + "GET" + path + "?" + query`, HMAC-SHA256, Base64. No published vector; the tests compute theirs outside the code | **confirmed and accepted live**: the sorted query with `timestamp`, HMAC-SHA256, lower-case hex. V3's recipe is one of the two golden vectors | -- |
| `RETENTION_MARGIN` of five minutes is enough | **not measurable without a key.** "The last three months" in `40704` may be 89 days; if so the oldest window is refused as `ExchangeRetentionWindowError` and #15 steps it a day later (`RETENTION_STEP`, itself a guess) | not measured: what BingX answers past its retention is unknown, and nothing maps to the retention error | the owner's first sync |
| whether `startTime` and `endTime` are inclusive, and the order within a page | **not documented**, and made not to matter: the window is widened and filtered, the cursor is the smallest id | **both inclusive**, by the probe, with and without a symbol; ascending by time. The cursor is the newest fill by value, so the order within a page does not matter. Which fills a capped page keeps does matter, and it is the oldest in range (the third probe, 2026-09-27) | -- |
| the sign of a fee, and the fields of a fee paid in BGB | **not documented**. A positive fee and a BGB deduction are refused loudly | negative for a fee paid, by the sample and the probe; a positive `commission` is refused loudly | the first real fill that shows either |
| `FILL_SCALE` of 18 covers every fee a venue reports; a 19th place fails its page loudly | unmeasured | float noise past it is expected, and `from_binary_float` removes it from a fill's `commission` and `quoteQty`, and from a balance's `free` and `locked` | whichever venue meets it |
| a zero `quote_quantity` for a dust trade never happens; if it does, the page fails loudly | unmeasured | unmeasured | whichever venue meets it |

### The exchange sync (#15)

`services/exchange_sync.py` drives every configured venue; `services/exchange_sync_plan.py`
holds its arithmetic as pure functions; `repositories/exchanges.py` and
`repositories/exchange_sync_runs.py` hold its queries. Nothing in this section is
vendor-specific, and nothing in it was measured against a venue: it is written against #12's
protocol and tested with a fake provider. The owner's first sync is the first measurement.

**A run.** Sweep interrupted runs, open the run (committed), find the single owner, ensure an
`exchange_accounts` row per configured venue (committed), then sync each account in turn,
sorted by `exchange_key`, over one session. No owner is a `success` with no accounts; more
than one is a `failed` run that calls no venue.

**Planning.** The requested start is `PORTFOLIO_EXCHANGE_HISTORY_START` at 00:00 UTC, or
2009-01-03; `clamp_to_retention` moves it inside the venue's retention. The first sync plans
`[effective_since, now)`. A later one plans a **top** range reaching `OVERLAP` (five minutes)
back into planned history, and a **bottom** range only when the owner moved the history start
earlier -- judged against the recorded `requested_since`, because a floor that a retention
step lifted above the declared edge must not be re-planned every run. Ranges are split into
windows no longer than `max_query_window`, newest first, and **persisted before any fetch**.

**A clock behind the plan pulls the plan back.** If `now < planned_until` -- a clock that once
ran ahead, then was corrected -- nothing is planned, `planned_until` is pulled back to `now`
(never below the floor), and `exchange_sync_clock_behind_plan` is logged. Keeping the future
ceiling would plan no top until real time caught up, and every fill in between would fall in a
range the plan claims to have read. A signed venue refuses every request made while the clock
is ahead, so the pull-back loses nothing; an unsigned one that answered pays a re-read.

**No network call inside an open write transaction.** SQLite has one write lock, held from a
transaction's first write to its commit. Planning therefore computes the re-clamp and the plan
first (both pure), then asks a `requires_symbol` venue for its symbols, and only then makes
every planning write and commits once; a page is fetched before it is written. A write held
open across a venue call and its rate-limit sleeps made a concurrent login or balance sync fail
with "database is locked".

**The pending queue is the checkpoint.** `exchange_sync_windows` holds only unfinished work:
a window, an optional symbol, and the cursor of its next page. Each page is one transaction --
insert the fills, then advance the cursor or delete the window -- so a crash loses at most the
page in flight, and the re-read is deduplicated by the constraint. Windows are read newest
first across the whole queue, sorted in Python. Before reading, the queue is re-clamped to the
current retention floor: a window wholly below it is dropped, one partly below has its
`since` moved up in place, and one longer than the venue's limit is re-split. A re-split
window restarts from its first page. **A window whose `since` only moved keeps its cursor
when the cursor is a trade id** (`trade_id_before`, `trade_id_after`): a bound on ids means
the same thing over a narrower range, and the rolling floor passes the oldest backfill window's
`since` on every run, so restarting it would make an interrupted backfill re-read that window
in full. A `time` cursor, or none, restarts.

| Cursor kind | What the sync does |
|---|---|
| `trade_id_before`, `trade_id_after`, `time` | pass the cursor back and follow `next_cursor` to `None`. A `next_cursor` already sent for this window in this run is a cursor cycle, refused as a schema error |
| `none` | a page holding `page_size` fills is not inserted: the window is replaced by its two halves, the newer read first. A full page for a window under 2 ms is a schema error |

**Idempotent, append-only writes.** `INSERT ... ON CONFLICT (exchange_account_id,
external_trade_id) DO NOTHING RETURNING external_trade_id` reports `seen` and `inserted`. Every
id the constraint skipped is compared with the stored row on the accounting fields -- order
id, symbol, both assets, side, quantity, price, quote quantity and its derived flag, fee and
fee asset, execution time -- as `Decimal`s and aware datetimes. A difference raises
`FillConflictError`, the page rolls back, and the account fails with `conflict`. **`raw_payload`
is not compared**, narrowing spec 014's hand-on: a venue adding a field to its response would
otherwise make every overlap re-read a false conflict. The triggers from `0007_exchange_sync`
refuse any `UPDATE` or `DELETE` of a fill.

**Failures, per account.** Subclasses are matched before their parents:

| Raised | Account status | `error_kind` | Behaviour |
|---|---|---|---|
| `ExchangeInsufficientScopeError` | `auth_failed` | `insufficient_scope` | stop, no retry |
| `ExchangeAuthError` | `auth_failed` | `auth` | stop, no retry |
| `ExchangeRateLimitedError` | `error` if exhausted | `rate_limited` | retry the same request up to `RATE_LIMIT_RETRIES` (3) times, waiting `Retry-After` or 2, 4, 8 s, rounded up to whole seconds; a wait over `MAX_RATE_LIMIT_WAIT_SECONDS` (60) is not waited |
| `ExchangeRetentionWindowError` | `error` if the steps run out | `retention_window` | first, once per window per run and spending no step, move the window's `since` to the retention edge computed from a fresh clock read, if that is later; otherwise, or when refused again, move it a `RETENTION_STEP` (a day) later, at most `MAX_RETENTION_STEPS` (3) per window per run. Either move drops pending windows wholly below the new `since`, keeps a trade-id cursor, and raises `effective_since` to where the held history now begins -- the new `since`, or the refused window's `until` when the move emptied it -- never past `planned_until`; the moved start is kept |
| `ExchangeUnavailableError` | `error` | `unavailable` | stop; the transport already retried |
| `ExchangeInvalidRequestError` | `error` | `invalid_request` | stop |
| `ExchangeSchemaError`, a cursor cycle included | `error` | `schema` | stop |
| `FillConflictError` | `error` | `conflict` | stop, page rolled back |
| anything else | `error` | `internal` | stop; traceback logged, `detail` is the type name only |

Only `Exception` is caught: a cancellation or the process being stopped leaves the run row
`running` for the sweep and writes no outcome. **A scheduled or startup run skips an
`auth_failed` account** without calling the venue, and records `skipped`; a manual sync
retries it (`docs/operations.md` section 13). An account whose queue is empty at the end of
the run becomes `ok`, with `last_synced_at`.

**What reaches a log, a column or a response**: counts, `exchange_key`, `error_kind` and the
exception's type name. Never a trade id, a cursor, a symbol or an amount. A stored `detail` is
an exchange error's `str()` -- a fixed class summary, the status and a digits-only venue code
-- a conflict's count, or a type name.

**Why a fresh edge comes before a step.** Newest first means a long first backfill reaches its
oldest window last, after the edge it was planned at has aged past what the venue keeps --
five minutes of `RETENTION_MARGIN` is less than a large backfill takes. That refusal is elapsed
time, not a short retention, and a whole day's step would discard history the venue still has.

**Guesses, recorded as guesses.** `RETENTION_STEP` of a day and `MAX_RETENTION_STEPS` of
three: the venue's real retention differing from its declared one is expected to be a matter
of a day or so ("the last three months" against 90 days). `OVERLAP` of five minutes: long
enough for a fill recorded a moment late, and free, because the constraint makes the re-read
insert nothing. The rate-limit numbers: three retries and a one-minute cap keep one account's
throttle from holding the coordinator, and a manual sync joined to it, for as long as a venue
likes.

## Who calls a provider, and when

**Landed in #10.** The wiring three earlier issues each deferred now exists.

`portfolio.main.lifespan` builds the shared `httpx.AsyncClient` with `build_http_client()`,
publishes it on `app.state.http_client`, and closes it on the way down. One client per
application, which is what the rate limiter requires rather than a tidiness preference: its
state lives on the transport and the transport lives on the client, so a second one would
keep its own idea of the interval and the effective floor would silently become half of what
`DEFAULT_MIN_HOST_INTERVAL_MS` says.

`services/balance_sync.py` is the only module in `services/` that reaches a chain provider,
and it does not import `httpx` or the registry: it takes a `provider_for` callable, and the
lifespan passes `lambda key: get_chain_provider(key, client)`. `main.py` imports
`portfolio.providers.chains` for its registration side effect, which is the one line that
makes the registry able to answer at all.

Two things reach a **chain** provider, and both go through `SyncCoordinator`:

- **the balance timer**, every `PORTFOLIO_BALANCE_SYNC_INTERVAL_MINUTES` minutes, plus once
  at startup when the newest run **of any status** started more than one interval ago --
  attempts rather than successes, so a crash loop that never finishes a sync still cannot
  start one per restart;
- **`POST /api/balances/sync`**, which is a request path calling a vendor *on purpose* --
  the owner asked for the read and is waiting for it. That is the deliberate asymmetry with
  prices, where `prices-are-never-fetched-in-a-request` forbids the same thing.

A second caller does not start a second run. It attaches to the one in flight and gets that
run's summary with `joined: true`, so a double-clicked refresh button costs no extra requests
at a public index.

Inside the application, one thing reaches a **price** source: the price timer, every
`PORTFOLIO_PRICE_REFRESH_INTERVAL_MINUTES` minutes -- sixty by default, matching
`STALE_AFTER` -- plus once at startup when the newest `prices.fetched_at` is older than one
interval. That one counts successes, because there is no record of a price attempt. So while
every source fails, a crash loop costs one refresh per restart: one call to Kraken, up to two
to Coinbase, one to the Kaspa server, and one to CoinGecko when it is keyed. There is no
coordinator and no join, because there is no endpoint that can ask for one: nothing in a
request path may reach a price vendor, which is the contract. `portfolio refresh-prices` is the
same work on demand, in a process of its own with a client of its own.

**No timer sleeps a whole interval after a restart that found nothing due.** It sleeps what
is left, rounded up to a whole second, so a deploy resumes the schedule rather than pushing
it back -- the first version pushed it back, and every deploy left prices stale for most of
an hour.

**Landed in #15: what reaches an exchange provider.** The lifespan builds the provider
mapping once, with `exchange_providers(client, settings=settings)`, and hands it to a closure
the exchange `SyncCoordinator` runs; `app.state` learns only `configured_exchanges`, the set
of its keys. Two things reach an exchange provider, both through that coordinator: the
exchange timer, which exists only when `PORTFOLIO_EXCHANGE_SYNC_ENABLED` is true **and** a
venue is configured, and `POST /api/exchanges/sync`. The
`api-never-reaches-an-exchange-provider` import contract forbids any module under
`portfolio.api` from importing `portfolio.providers.exchanges` at all, directly or not, so the
coordinator is the only path from a request to a credential.

**Three timers reach a provider, each its own task with its own switch, and they share no
state.** A fourth, the backup timer (#22), reaches none. `services/scheduler.py` is generic
over what it ticks -- it takes "when did this last happen" and "do it" -- so the timers are
instances rather than loops, and none can stop another. They are separate because they
answer to different vendors:

- chain indexes that ban you for asking too often;
- market-data APIs where the primary answers every configured pair in one call;
- venues that sign every request with the owner's key.

**Nothing in production calls a chain provider's `health()`.** `GET /api/health/detail`
(#23) reports what each source's last recorded attempt says and asks no vendor, because the
page refetches every minute and a check that called an index would spend the budget the
limiter exists to protect (`services/health.py`).

### The per-address cache is the snapshot table

An earlier revision of this document said a per-address cache was outstanding and that #10
owned it. It is answered rather than built: `balance_snapshots` **is** the previous reading,
with the instant it was taken, durable across restarts and visible to an operator. A second
in-memory cache in front of it would be a copy of that table with a different lifetime and
no way to look at it.

What the deferral was really protecting against -- a refresh button that hammers a public
index -- is answered by the coordinator's join, not by a cache. A cache would have answered
it by returning a stale number that looks exactly like a fresh one, which is the failure
`ProviderUnavailableError` exists to prevent.

## Not done yet, and who owns it

- **BingX has been probed, and the provider has not yet run against it.** #14 wrote it from
  the documentation and the owner's read-only probe, whose account held a few dozen fills
  in one symbol. Three things only real use shows: a page past the first (no account had
  500 fills), a second symbol in one answer (the premise of never sending `symbol`), and
  history older than the probe's two weeks. The owner's first sync after trading a second symbol is
  the check, and `docs/operations.md` section 14 says how to make it. BingX reuses the
  `exchange_fills` label; it has no public endpoint, so it needs no second one.
- **Neither balance endpoint has been called with a real key.** #104 wrote both balance
  reads from documentation alone: Bitget's `GET /api/v2/spot/account/assets` and BingX's
  `GET /openApi/spot/v1/account/balance`. The owner's first sync after #104 is the first
  measurement. A surprise is a `schema` failure recorded on the balance read, which leaves
  the fills and the account's status as they were; it is the evidence to act on. What is
  most likely to need a rule: the float noise in BingX's amounts, whether Bitget's
  `limitAvailable` belongs in the total, and whether BingX's fund account holds units its
  spot answer does not. Each has a row in its venue's section.
- **BingX's empty success.** The provider refuses every malformed empty answer, but cannot
  tell `fills: []` from a quiet window, and BingX's own changelog shows a sibling endpoint
  answering a backend failure that way until 2026-09-05. If it ever happens on `myTrades`,
  the missed fills come back only through an overlap or a moved history start. See the
  BingX section; nothing here closes it.
- **Bitget on a Unified Trading Account.** The provider speaks the Classic v2 API only, which
  is right for the owner's Classic account. `GET /api/v3/trade/fills` differs in its cursor,
  its window, its rate and its field names, so supporting a UTA account is a second provider
  or a second mode, written from its own documentation. That is #76. Until then the operator keeps the account Classic (`docs/operations.md`
  section 12).
- **The Bitget provider was written from documentation.** Measuring a signed endpoint needs
  a key this repository must not contain. Every guess in the Bitget section is written to
  fail loudly, as a typed error naming a field, rather than to guess. The owner's production
  syncs since #15 have imported fills through it, so the fill read works against the venue
  for a Classic account; the guesses listed next have not been checked one by one, and a
  refusal on a later sync is the evidence to act on.
- **What #15 guessed, and the owner's first sync measures.** `RETENTION_STEP` (a day),
  `MAX_RETENTION_STEPS` (three), `OVERLAP` (five minutes) and the rate-limit numbers are
  choices, not documentation; "The exchange sync" above says what each does. A retention
  refusal on the first sync is the evidence for moving `RETENTION_MARGIN` or the step.
- **A venue that `requires_symbol` is planned with the symbols known at plan time.** No wired
  venue requires one. A symbol first traded later is read only from windows planned after it
  appears. #14 found that BingX needs none: one query without a symbol answers every symbol.
- **A corrected fill is refused, not recorded.** The sync stops the account with `conflict`
  rather than overwriting or silently keeping the first version. M4's manual adjustments did
  not change that: an adjustment is an inflow only (`docs/accounting.md`, "What an adjustment
  is"), so it cannot stand for a fill a venue changed. No issue owns recording a correction.
- **A venue's declared `rate_limit` is read by nothing.** Bitget declares 10 requests a
  second and BingX 5; the sync does not pace by either, and the transport's one request a
  second per host is what actually spaces them. `RateLimit` still refuses a budget below one
  when it is built, so the declaration is ready for the day a sync needs it.
- **A throttle answered on a 200 loses its `Retry-After`.** BingX's `_get` reads the header
  only when the status is not 200, and BingX answers most errors on a 200, so the sync waits
  its own backoff instead of the venue's. That is #115. Bitget's `_get` has the same
  condition, so its in-band `429` would drop the header too, if Bitget sends one with it; that
  is not documented either way.
- **A signed request replayed by the transport can arrive expired.** `RetryingTransport`
  retries a `GET` that got a 429, a 5xx or no answer by sending **the same request
  again** -- same timestamp, same signature -- for up to `RetryPolicy.max_attempts` (3)
  attempts, with backoff up to `max_backoff_ms` (30 seconds). A venue that checks a
  request's timestamp against a receive window can refuse the replay as expired. **Decided
  for Bitget in #13**: its window is 30 seconds, the replay is accepted, and `40008` and
  `40005` are `ExchangeUnavailableError`, never auth, so the next run signs afresh. **Decided
  for BingX in #14** the same way: its window is 5000 ms by default, so a replay after any
  backoff is likely refused, as `100421`, which is `ExchangeUnavailableError`. The probe saw
  every error on a 200, which the transport does not retry, so it is the rarer path. Neither
  venue's clock error marks the account `auth_failed`: the key is not what failed.
- **Tuning settings.** Every number in the defaults table under "Vendor facts" -- the host
  interval, the retry policy and the four timeouts -- is still a module constant. Promoting one
  to a `PORTFOLIO_PROVIDER_*` setting is a change an operator's measurement should drive, not
  a guess.
- **No bound on a whole read.** Each attempt has its timeouts, but a read is up to three
  attempts and the backoff between them, and nobody chose a ceiling for the sum. A server
  that sends a byte just inside the read timeout holds one attempt open for as long as it
  likes. That is #50.
- **A partial multi-address read returns nothing.** A Bitcoin read that fails at the twelfth
  of twenty addresses yields none of the eleven it read. That falls out of raising on the
  first failure; whether it is the right answer is #54.
- **A corrupt compressed body escapes as `httpx.DecodingError`.** It is not an
  `httpx.TransportError`, which is all the chain and price providers and `EndpointSet` catch,
  so it breaks `health()`'s promise never to raise and can end a price refresh instead of
  failing over. Both exchange providers already catch it. That is #75.
- **A JSON object that names a key twice keeps the last value.** `decode_json` gives
  `json.loads` no `object_pairs_hook`, so a repeated `free` or `price` is read silently
  rather than refused. No
  vendor is known to send one. That is #114.
- **A failed price refresh cannot tell a bad key from an outage.** Each source's
  `ProviderError` is swallowed the same way, so a wrong `PORTFOLIO_COINGECKO_API_KEY` on a day
  the key-free sources are down reads as "every source failed". That is #59.
- **The source-walk test, and the traceback it does not reach.**
  `backend/tests/security/test_address_logging.py` walks the modules that handle an address,
  `services/balance_sync.py` and `providers/chains/bitcoin.py` among them, and fails on a log
  call that could bind one. No chain, price or exchange provider has a log call at all. The
  transport's is the only one, deliberately, since its contract is the only one enforced
  rather than remembered.

  **What the walk cannot see is a traceback.** `services/balance_sync.py` catches any
  non-`ProviderError` exception from a chain and calls `_logger.exception`, and a `KeyError`
  raised while correlating a balance renders its key, which would be an address. The database
  column and the response body carry only the exception's *type name*, never its message.
  `api.errors.handle_unexpected_error` has logged the same way since #5.

  Since #23 the traceback is rendered to a string before the `ValueRedactor` runs, so an
  address in it is replaced by pattern. That is redaction by recognition, not by
  construction. Dropping the traceback for a correlation id is the other remedy, and #62,
  which asked for one or the other, is still open.
- **Network-aware address registration.** A wrong-network address is refused by the
  *provider*, at read time, not when the wallet is registered. Making registration
  network-aware changes #5's contract and needs a story for rows that already exist. That is
  #55.
- **The Kaspa batch ceiling.** 64 is a guess; see above. The first evidence will be a
  refused batch in production, and the refusal is written to carry the size so that the
  evidence is actionable when it arrives.
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
