# Adding a provider

What a new chain has to implement, what a new price source has to implement, what the
shared machinery already does for both, and --
kept separate on purpose -- which facts about each vendor were confirmed against its
published documentation, which were measured against the live service, and which are still
guesses.

Two kinds of provider live under `backend/src/portfolio/providers/`. A **chain provider**
reads balances from addresses (`providers/chains/`); a **price source** reads what an asset
costs (`providers/prices/`). Everything down to "Vendor facts" is about the first kind; the
"Price sources" section near the end is about the second, and says where it differs.

A third kind, exchange providers that read spot fills and balances from Bitget and BingX, was
removed together with the accounting built on it (spec 036). Their vendor facts are no longer
kept here; the specs that introduced them (012, 014, 015, 017) are the historical record.

Read `backend/src/portfolio/providers/base.py` alongside this. The docstrings there are the
reasoning; this is the checklist.

**Every vendor fact here carries one of three words**, in the summary table at the top of
each provider's section and in the detail beneath it:

- **confirmed**: read in the vendor's published documentation, on the date given;
- **measured**: seen in an answer from the live service, on the date given;
- **unverified**: neither. The row says what would settle it, and who is placed to.

A fact nobody has read or seen is unverified, however plausible. The summary tables are under
"Vendor facts" for Bitcoin and Kaspa, and under "Price sources" for the four price sources and
for Kraken's daily candles, which the price backfill reads.

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
construction, and widening it would make *every* future `POST` retryable -- including a
request that places an order or moves funds, where a retry after a transport error can do it
twice. One provider's convenience would silently become another's duplicate. The
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
and `EndpointSet` is exactly what any provider with a primary and a fallback will reach for,
at which point the failover loop double-submits a write after a transport error.

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

The set this release ships is exactly seven: `address_balance` for a single balance read,
`address_balances` for Kaspa's batch read, `block_tip_height` for the tip-height call
Esplora's `health()` makes, `node_health` for the health document Kaspa's reads,
`asset_price` and `asset_prices` for the price reads (#9), and `asset_daily_closes` for the
daily candles the price backfill reads (spec 037). The three exchange labels left with the
exchange providers (spec 036).

So a new endpoint is two lines, not one: the constant, and its name in `ENDPOINT_LABELS`.
The same shape as `PUBLIC_API_PATHS` in rule 8 -- the default says nothing, and saying more
about an endpoint is a visible edit to a named constant. The label must still match
`ENDPOINT_LABEL`'s pattern, which a test asserts over the set's contents.

`strip_query(url)` exists separately and removes the query string, the fragment and any
userinfo. It is the rule `CLAUDE.md` states: a query string can carry a credential or a
signature, so a URL is never logged with one. **No provider calls it.** Every request is
logged through `request_target`, which carries neither the path nor the query. `strip_query`
is not sufficient for a chain provider either: reaching for it to log a chain request would
meet the letter of the rule and leak the address anyway.

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
| history read, for the rebuild | `GET /address/{address}/txs/chain/{last_seen_txid}`, 25 a page, labelled `address_history`, between two balance reads | **confirmed** 2026-10-08, both vendors' documentation; **measured** 2026-10-08 on both hosts. Detail in *Transaction history, for the balance rebuild* |
| an empty page | not proof of the end: a cursor not in the history answers `200 []` too. Completeness is proven by the count and the balance instead | **measured** 2026-10-08 |
| retention or history window | none: every history measured reached its first transaction | **measured** 2026-10-08 back to 2025-09 on mainnet; **unverified** further back |
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
| history read, for the rebuild | `GET /addresses/{address}/full-transactions-page?limit=500&resolve_previous_outpoints=light`, paged by `X-Next-Page-Before`, labelled `address_history`, between reads of `transactions-count` and `balance` | **confirmed** 2026-10-08, from the live OpenAPI document; **measured** 2026-10-08. Detail in *Transaction history, for the balance rebuild* |
| `block_time` | epoch **milliseconds** | **measured** 2026-10-08: the document gives no unit for the field, only for the cursors |
| retention or history window | none: a history from 2023-01-29 was served whole | **measured** 2026-10-08; **unverified** before 2023-01 |
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

### Transaction history, for the balance rebuild: confirmed and measured on 2026-10-08

The balance rebuild (spec 038) reads every confirmed transaction of an address. Everything
below was read on **2026-10-08** in each vendor's own documentation and measured against the
live services the same day, with addresses taken at run time from blocks the services served
and kept in memory only. None is written here.

#### Esplora: `/address/{address}/txs/chain`

**Confirmed against the documentation** (Blockstream's `API.md`, and mempool.space's REST
documentation, read from the page's own script bundle because the page is a single-page app):

- Amounts: "Amounts are always represented in satoshis."
- `GET /address/:address/txs/chain[/:last_seen_txid]`: "Get confirmed transaction history for
  the specified address/scripthash, sorted with newest first. Returns 25 transactions per page.
  More can be requested by specifying the last txid seen by the previous query." mempool.space
  carries the same text, but its URL template shows only `/address/:address/txs/chain`.
- `GET /address/:address` answers `chain_stats` and `mempool_stats`, each with `tx_count`,
  `funded_txo_count`, `funded_txo_sum`, `spent_txo_count` and `spent_txo_sum`.
- A transaction carries `vin[]` with `is_coinbase` and `prevout` ("previous output in the same
  format as in vout"), `vout[]` with `scriptpubkey_address` and `value`, and `status` with
  `confirmed`, `block_height`, `block_hash` and `block_time`.
- Rate limits: mempool.space enforces them, with a 429 and possibly a ban, and publishes no
  number. Blockstream's `API.md` says nothing about them.

**Measured on both hosts**, over five histories (two mainnet, one of them fully spent; one
testnet3; one testnet4 address of coinbase transactions only; and the first page of an
address with about 1.2 million transactions):

| | What came back |
|---|---|
| page size | 25, then the remainder, then `[]` |
| paging by the last txid of the previous page | reaches the oldest transaction, no duplicate, and the total equals `chain_stats.tx_count` in every case |
| **a cursor not in the history** | **`200 []`, the same answer as the end of the history**. An empty page does not prove the end |
| `?after_txid=` on `/txs/chain` | **ignored by both hosts**: the first page again. A pager relying on it loops on page 1 |
| `/txs` (not used) | up to 50 confirmed on mempool.space, 25 on blockstream.info, with pending ones mixed in |
| order | newest first by `block_height` |
| `funded_txo_sum` and `spent_txo_sum` | the outputs to the address and the `prevout` values of the inputs from it, summed over every transaction |
| the walk back from `funded_txo_sum − spent_txo_sum` | ends at exactly 0, never below, in all five histories |
| a coinbase input | `is_coinbase: true`, no `prevout` |
| types | every amount, height and time a JSON integer; `block_time` in Unix seconds, equal to the block header's time |
| a pending transaction's `status` | `{"confirmed": false}`: the other keys are **absent**, not `null` as `API.md` says |
| rate-limit headers | none on either host |

**Not documented, and not relied on:** the order of transactions within one block; how a
reorganisation affects a cursor (measured: an unknown one answers `[]`); any retention limit
(every history measured reached its first transaction, back to 2025-09 on mainnet; a
multi-year walk was not run). Header times are not guaranteed to rise with height, so two
transactions a block apart can fall on days in the other order; the rebuild dates each by its
own block time, and a walk that this would take below zero is refused rather than stored.

**What the reader does with that.** It pages with the path form only, the one both hosts
honour, at most `tx_count // 25 + 2` pages, checking each txid before it goes back into a
path. It does not take an empty page as proof: it reads `/address/{address}` before and
after the paging, and calls the history complete only when the two reads agree, the distinct
transactions collected number `chain_stats.tx_count`, and their effects add up to
`funded_txo_sum − spent_txo_sum`. Otherwise it answers `count_mismatch`, `balance_mismatch` or
`moved_during_read`, and the rebuild stores nothing.

#### Kaspa: `/addresses/{address}/full-transactions-page`

**Confirmed against the live OpenAPI document** (https://api.kaspa.org/openapi.json, version
`d0ea012`):

- `limit`: "The max number of records to get. For paging combine with using 'before/after'
  from oldest previous result. Use value of X-Next-Page-Before/-After as long as header is
  present to continue paging. The actual number of transactions returned for each page can be
  != limit." Minimum 1, **maximum 500**, default 50.
- `before` and `after`: "block time before / after this (epoch-millis)".
- `resolve_previous_outpoints`: `no`, `light` or `full`; "Light fetches only the adress and
  amount."
- A transaction carries `transaction_id`, `block_time`, `is_accepted`, `inputs[]` and
  `outputs[]`. An input's `previous_outpoint_address` and `previous_outpoint_amount` are
  **optional** in the schema; an output's `amount` is a required integer. No unit is given for
  `block_time`.
- `GET /addresses/{address}/transactions-count` answers `{"total": <int>}`, and
  `GET /addresses/{address}/balance` answers `{"address", "balance"}`.
- No rate limit is documented.
- `GET /addresses/{address}/balance/{day_or_month}` is titled "EXPERIMENTAL - EXPECT BREAKING
  CHANGES" and is "only available for larger addresses". **Not used**: it answered `[]` for
  every mainnet address measured and is disabled on the test network.

**Measured**, over five histories (four mainnet, of 66, 118, 702 and 2,247 transactions, the
largest from 2023-01-29 to 2024-01-25; one testnet-10):

| | What came back |
|---|---|
| page size | up to `limit`, and **once 501 for 500**: the server completes the millisecond at the boundary, so the exclusive `before` cursor loses nothing |
| `X-Next-Page-Before` | present on every page but the last, and equal to the smallest `block_time` on the page |
| following it | reaches the oldest transaction, with no duplicate and no gap; the count equals `transactions-count.total` in every case |
| the end | the last page has no `X-Next-Page-Before`; no trailing empty page |
| `limit=501` | 422 |
| order | newest first |
| `block_time` | **epoch milliseconds** |
| amounts | JSON integers in sompi; `mass` and `previous_outpoint_index` are strings |
| `light` resolution | every input of every history had its address and amount |
| reconciliation | outputs to the address minus resolved inputs from it equals `/balance`, before and after the paging, and the walk back ends at exactly 0 |
| `is_accepted` | `true` on all 3,154 rows read; `acceptance=rejected` answered `[]` |
| caching | Cloudflare, `max-age=8` |

**Not documented, and not relied on:** what an unaccepted transaction looks like, and whether
`transactions-count` counts it (none was ever seen); that the boundary millisecond is always
completed (seen once); any rate limit; any retention before 2023-01.

**What the reader does with that.** It pages with `limit=500` and
`resolve_previous_outpoints=light`, following `X-Next-Page-Before` while it is sent, never
computing the cursor itself, and de-duplicating by `transaction_id`. It counts only accepted
transactions, and an input without its address or amount is `unresolved_input`, never a zero.
The cursor is checked to be digits before it goes into the URL, and the paging stops at
`total // 500 + 2` pages. Cloudflare's eight-second cache can serve the "after" reads from the
same copy as the "before" ones on a short history, which weakens the before-and-after check;
the count and the sum still have to agree with each other.
It reads the count and the balance before and after the paging, and the history is complete
only when both agree, the transactions number the count, and their effects add up to the
balance. A transaction's day is the UTC date of its `block_time`, in milliseconds.

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
  mechanical consequence is that `services/prices.py` (valuation) and
  `services/portfolio_history.py` (the value over time) import no provider, and
  `services/price_refresh.py` and `services/price_backfill.py` are the only modules in
  `services/` that import from `providers.prices`. No router imports either of those two:
  only `main.py` and `cli.py` build them. That split is the guarantee; see the module
  docstrings.

### Each source at a glance

The endpoints are read from the four modules under `providers/prices/`. The refresh asks each
source for the current price only, so none of the four tables below has a retention window to
record. **History is asked of one vendor, Kraken, and only by the price backfill** (spec 037):
its endpoint, its window and what was confirmed about it are in the second Kraken table and in
*Kraken's daily candles, confirmed and measured on 2026-10-08*, below.

#### Kraken, the primary

| | What the code uses or assumes | Status |
|---|---|---|
| endpoint | `GET https://api.kraken.com/0/public/Ticker?pair=XXBTZUSD,XXBTZEUR,KASUSD,KASEUR`, labelled `asset_prices` | **measured** 2026-09-23 |
| what one call answers | all four pairs, with no key | **measured** 2026-09-23 |
| the price | a string, `c[0]`; a failure arrives in `error`, on a 200 | **measured** 2026-09-23 |
| the keys of `result` | the pair codes asked for. An entry under any other key is refused, never matched by position | **measured** 2026-09-23 for these four; the documentation does not promise it |
| rate limit | public endpoints are limited per IP address, and calling them once a second or less stays within the limit. No monthly quota is stated | **confirmed** 2026-10-04, Kraken's support article on API rate limits |

#### Kraken's daily candles, for the price backfill only

Read by `KrakenDailyCloses` in `providers/prices/kraken.py`, never by the hourly refresh. The
detail beneath each status is in *Kraken's daily candles, confirmed and measured on
2026-10-08*, below.

| | What the code uses or assumes | Status |
|---|---|---|
| endpoint | `GET https://api.kraken.com/0/public/OHLC?pair=<code>&interval=1440`, one pair per call, labelled `asset_daily_closes` | **confirmed** 2026-10-08, Kraken's API reference; **measured** 2026-10-08 |
| pairs | `XXBTZUSD` and `KASUSD`, the ticker's own codes. USD only: the chart values in USDT | **measured** 2026-10-08 |
| key | none; a public endpoint | **confirmed** 2026-10-08; **measured** 2026-10-08 |
| `interval` | `1440` minutes, one candle a day. Documented options 1, 5, 15, 30, 60, 240, 1440, 10080 and 21600; the default is 1 | **confirmed** 2026-10-08 |
| `since` | not sent. Documented for incremental updates, and it cannot reach further back than the window | **confirmed** 2026-10-08 |
| a candle | `[time, open, high, low, close, vwap, volume, count]`: `time` and `count` integers, the other six strings. The close is index 4 | **confirmed** 2026-10-08; **measured** 2026-10-08, every price a JSON string |
| a candle's day | the UTC date of `time`, the candle's open. Every `time` measured is 00:00:00 UTC; any other is refused | **measured** 2026-10-08; the refusal is spec 037, R1 |
| the last entry | the current day, still trading, always present. Never stored | **confirmed** 2026-10-08, quoted below |
| `result.last` | documented as the value to pass as `since` for new committed data. Read as the open time of the last committed candle, and every entry after it skipped | **confirmed** 2026-10-08 for the wording; **measured** 2026-10-08 as 2026-10-07 00:00 UTC, the day before. That it equals the last committed candle's time is the measurement, not the documentation |
| retention window | the 720 most recent entries, and nothing older, whatever `since` says | **confirmed** 2026-10-08, quoted below; **measured** 2026-10-08 |
| a close, once committed | assumed never to change | **unverified**: nothing documents it either way. The backfill rewrites every day it receives, every day, so a correction inside the window would be picked up |
| rate limit | per-API-key call counters are documented; a public, unauthenticated call is not addressed | **confirmed** 2026-10-08, Kraken's Spot REST rate-limit guide. **Not measured**. Two calls a day, at the shared one-request-a-second floor |

#### Coinbase Exchange's daily candles, for BTC before Kraken's window

Read by `CoinbaseDailyCloses` in `providers/prices/coinbase.py`, only by the price backfill and
only for days before the earliest stored close (spec 038, R8). Detail in *Coinbase Exchange's
daily candles, confirmed and measured on 2026-10-08*, below.

| | What the code uses or assumes | Status |
|---|---|---|
| endpoint | `GET https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400&start=<day>T00:00:00Z&end=<day>T00:00:00Z`, labelled `asset_daily_closes` | **confirmed** 2026-10-08, Coinbase's API reference; **measured** 2026-10-08 |
| key | none: `security: []` | **confirmed** 2026-10-08 |
| pairs | BTC/USD only. Coinbase Exchange lists no KAS product | **measured** 2026-10-08 |
| a window | at most 300 days, both ends sent; the next starts the day after | **confirmed** 2026-10-08 as "300 candles"; **measured** as 300 *intervals*, both ends inclusive |
| a candle | `[time, low, high, open, close, volume]`, prices as **JSON numbers**, parsed straight to `Decimal`. The close is index 4 | **measured** 2026-10-08: the documentation's "decimals as strings" rule does not hold here |
| a candle's day | the UTC date of `time`; any time not at 00:00 UTC is refused, any day outside the window dropped | **measured** 2026-10-08 |
| first day | 2015-07-20; nothing before it is asked | **measured** 2026-10-08 |
| today | never asked: the range ends yesterday at the latest | **measured** 2026-10-08 that a range reaching today returns today's moving candle |
| rate limit | 10 requests a second per IP, bursts to 15, a 429 beyond | **confirmed** 2026-10-08. Twelve requests on the first fill, none after, at the shared floor |

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

**The price backfill (spec 037) is a separate budget, to the same host.** One request per pair
per run, two pairs, one run a day: 2 a day and 2 × 30 = 60 a month, or 2 × 365 = 730 a year.
Together with the refresh that is 780 Kraken requests in a 30-day month. Like the refresh it
counts successes when deciding whether to run at startup (the newest `close` row's
`recorded_at`), so while both pairs fail a crash loop costs two calls per restart; one pair
answering is enough to stop that. Nothing falls back from Kraken for the backfill: a pair it
cannot read is reported and retried by the next day's run.

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

### Kraken's daily candles, confirmed and measured on 2026-10-08

The price backfill reads Kraken's OHLC endpoint at a one-day interval. Everything below was
read on **2026-10-08** in Kraken's own API reference,
https://docs.kraken.com/api-reference/market-data/get-ohlc-data — the older URL,
https://docs.kraken.com/api/docs/rest-api/get-ohlc-data, redirects there, and the Markdown
form at the same path with `.md` appended carries the OpenAPI schema — and measured against
the live API the same day.

**Confirmed against the documentation:**

- `GET https://api.kraken.com/0/public/OHLC?pair=<code>&interval=1440`, a public endpoint that
  takes no key.
- `interval` is in minutes. The documented options are 1, 5, 15, 30, 60, 240, 1440, 10080 and
  21600, and the default is 1, so an omitted `interval` would return one-minute candles.
  `DAILY_INTERVAL_MINUTES` is 1440.
- `since` is documented as "Return OHLC entries since the given timestamp (intended for
  incremental updates)". It is **not sent**: the backfill asks for the whole window every run.
- A tick is documented as `[int <time>, string <open>, string <high>, string <low>,
  string <close>, string <vwap>, string <volume>, int <count>]`. `CLOSE_INDEX` is 4.
- `result.last` is documented as "ID to be used as since when polling for new, committed OHLC
  data".
- Verbatim: "The last entry in the OHLC array is for the current, not-yet-committed timeframe,
  and will always be present, regardless of the value of `since`."
- Verbatim: "Returns up to 720 of the most recent entries (older data cannot be retrieved,
  regardless of the value of `since`)." **That is the retention window**: a rolling 720 days
  at this interval.

**Measured against the live API:**

| Pair code | Entries | First | Last entry | `result.last` | Committed closes parsed |
|---|---|---|---|---|---|
| `XXBTZUSD` | 721 | 2024-10-18 | 2026-10-08, the day of the measurement, uncommitted | 2026-10-07 00:00 UTC | 720 |
| `KASUSD` | 689 | 2024-11-19, KAS's first day on Kraken | 2026-10-08, uncommitted | 2026-10-07 00:00 UTC | 688 |

- Every entry's `time` is 00:00:00 UTC, the open of its day, read as Unix seconds — which the
  dates above bear out. `parse_daily_closes` refuses a candle at any other time (spec 037, R1).
- Every price is a JSON **string**, so no float boundary is crossed; `require_price` decides,
  as for every source.
- The envelope is the ticker's, `{"error": [], "result": {"<code>": [...], "last": <int>}}`,
  and the two parsers share the envelope check (`_result_of`): an error reported in `error`
  on a 200 is a refusal, not an empty series. A `result` carrying any key other than the pair
  asked for and `last` is refused as a correlation bug.

**How the parser tells the moving candle apart, and what it rests on.** An entry whose `time`
is after `result.last` is skipped. The documentation says the last entry is uncommitted and
calls `last` an ID for `since`; that `last` equals the open time of the last *committed*
candle is the measurement, not a promise. If it ever pointed at today's candle instead, today's
still-moving price would be stored as a `close` — for a day, until the next run rewrites it
with the committed one, since a `close` is written over anything.

**Rate limits.** Kraken's Spot REST rate-limit guide, read on 2026-10-08
(https://docs.kraken.com/exchange/guides/rest/ratelimits), documents a call counter **per API
key** — Starter 15, decaying by 0.33 a second; Intermediate 20, by 0.5; Pro 20, by 1 — and
does not address public, unauthenticated calls. The support article read on 2026-10-04 (see
the ticker table) says public endpoints are limited per IP address. **Nothing was measured.**
The backfill makes two calls a run, once a day, and the shared transport's floor of one
request per second per host applies to them as to the ticker.

**Assumed, and written down as an assumption:** that a day's close never changes once it is
committed. Nothing documents it either way. The backfill does not depend on it: it rewrites
every close it receives, every day, idempotently, so a correction Kraken made inside the
window would be picked up by the next run.

**What the window means for the history.** A day more than 720 days old can only be in
`price_history` because the backfill ran while that day was still inside the window. The
daily timer keeps the history complete from the first deploy onward; days that had already
left the window by then are a gap, except BTC's: Coinbase's candles fill those back to
2015-07-20 (spec 038, below). **KAS before 2024-11-19 has no price at all** -- Kraken has none
and Coinbase lists no KAS -- so those days show as a gap, never as zero.

### Coinbase Exchange's daily candles, confirmed and measured on 2026-10-08

Kraken keeps 720 daily candles, so BTC before 2024-10-18 needs a second source (spec 038,
R8). Read on **2026-10-08** in Coinbase's own documentation --
https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles,
the Exchange rate-limit and types pages, and the Exchange OpenAPI specification -- and measured
against the live API the same day.

**Confirmed against the documentation:**

- `GET /products/{product_id}/candles` at `https://api.exchange.coinbase.com/`, with
  `security: []`: no key.
- "Historic rates for a product. Rates are returned in grouped buckets." The response items
  are "`time` bucket start time, `low`, `high`, `open`, `close`, `volume`".
- `granularity` "must be one of the following "second" values: `{60, 300, 900, 3600, 21600,
  86400}`, or your request is rejected." 86400 is a day.
- "If the `start` or `end` fields are not provided, both fields are ignored." Both are always
  sent.
- "The maximum number of data points for a single request is `300` candles."
- "Historical rate data may be incomplete. No data is published for intervals where there are
  no ticks." and "Historical rates should *not* be polled frequently."
- Rate limits for public endpoints: "Requests per second per IP: 10", bursts "Up to 15", and a
  `429 Too Many Requests` beyond.
- The format of `start` and `end`, the order of the candles and the type of each element are
  **not documented**.

**Measured against the live API:**

| | What came back |
|---|---|
| `BTC-USD`, a day granularity, 2023-01-01 to 2023-10-01 | 274 candles: both ends inclusive, newest first, every `time` at 00:00 UTC in Unix seconds, no missing day |
| a candle's types | `time` an integer; the four prices JSON **numbers** -- some bare integers -- and never strings, against the documentation's general rule that "decimal numbers are returned as strings" |
| 300 intervals in one request | 301 candles; 301 intervals is a `400` naming the count. The limit counts intervals, not candles |
| `start` or `end` alone | both ignored: the 350 most recent candles, today's moving one included |
| `start` and `end` as ISO 8601 with `Z`, a date alone, or Unix seconds | all accepted alike |
| 2015-07-01 to 2015-08-01 | the first candle is **2015-07-20**; 2014 answers `[]` |
| 2015-07-20 to 2024-10-18, in twelve windows | 3,379 candles, no missing day, no duplicate |
| `KAS-USD` | 404; `GET /products` lists no KAS product |
| headers | Cloudflare, `max-age=300`; no rate-limit header |

**The float boundary.** The prices arrive as JSON numbers, so a plain `json.loads` would make
floats in `providers/` -- what rule 2 bans. `decode_json` builds every JSON number as a
`Decimal` from the digits the vendor sent, a bare integer arrives as an `int`, and
`require_price` decides each close, as for every source.

**Assumed, and written down as an assumption:** that a past day's candle does not change.
Nothing documents it either way, and a response can be five minutes stale through the cache.
Unlike Kraken's window, the range Coinbase fills is asked **once**: a correction made after the
first fill is not picked up. A day it has no candle for is a gap, never a zero, and is not
asked for again.

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

**A source of daily closes is a second, smaller protocol**, `DailyCloseSource` in the same
module, and only the price backfill uses it. `KrakenDailyCloses` is the one implementation.

| Member | Kind | What it must do |
|---|---|---|
| `name` | property | As above, written to `price_history.source`. |
| `pairs` | property | Every pair it can backfill. `BACKFILL_PAIRS` for Kraken: BTC and KAS in USD. |
| `daily_closes` | async method | Every **committed** daily close for one pair, oldest first, as `DailyClose(day, close)`. The day still trading is never returned; a failure is one of the three `ProviderError`s, never an empty series. |

A pair the backfill cannot read is reported by name and the class of its error, and the other
pair is still stored: each is asked, written and committed on its own.

Two more rules a new source inherits rather than decides:

- **Prices go through `require_price`**, which is the one boundary deciding what counts as a
  price: a JSON string or a `Decimal` from the shared decoder, positive, finite. Four vendors,
  one rule.
- **A new endpoint label goes in `ENDPOINT_LABELS`** in `providers/http.py`, in the same
  change as the call site that uses it. Membership is the gate; an unlisted label renders as
  `<unlabelled>` and the request becomes invisible in a log.

### Prices in the database

`prices` holds the price **now**: one row per `(asset_id, quote_currency)` — four today —
overwritten by every refresh. `amount` is `NumericText(12)` and **never `sqlalchemy.Numeric`**,
which round-trips through a C double on SQLite. Twelve decimal places serve a sub-cent asset
and a five-figure one in the same column: KAS was quoted near `0.042` and BTC near `86,000` on
the day this was measured.

`price_history` (spec 037, migration `0013_price_history`) holds the price **of a day**: one
row per asset, quote currency and UTC day, `UNIQUE (asset_id, quote_currency, day)`, with the
same `NumericText(12)` amount, the `source` that supplied it and a `basis`:

| `basis` | What it is | Who writes it |
|---|---|---|
| `close` | the day's closing price, from a committed daily candle. Final | the price backfill, over anything already there |
| `observed` | the latest price the hourly refresh saw that day | the refresh, for every pair it stores, over an earlier `observed` of the same day and **never over a `close`** |

So today's row is `observed` and moves each hour, and the next day's backfill replaces it with
the close. A day the backfill never reached keeps its `observed` row, which is the nearest
thing to a close there is.

**Money is never aggregated in SQL.** `SUM`, `ORDER BY` and `<` on a `TEXT` money column all
apply SQLite's numeric affinity, which is the double the column type exists to avoid — applied
to every row at once. `repositories/prices.py` and `repositories/price_history.py` have no
method that totals, sorts by price or compares one; `price_history` is ordered by `day`, a
`YYYY-MM-DD` text that orders as dates do, and the services load the rows and sum them in
Python.

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
  the column, not to prices — any amount a later column holds meets the same
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

Inside the application, two things reach a **price** source, and both are timers:

- **the price refresh**, every `PORTFOLIO_PRICE_REFRESH_INTERVAL_MINUTES` minutes -- sixty by
  default, matching `STALE_AFTER` -- plus once at startup when the newest `prices.fetched_at`
  is older than one interval. That one counts successes, because there is no record of a price
  attempt. So while every source fails, a crash loop costs one refresh per restart: one call
  to Kraken, up to two to Coinbase, one to the Kaspa server, and one to CoinGecko when it is
  keyed. Each refresh also records today's `observed` row in `price_history`;
- **the price backfill** (spec 037), every `PORTFOLIO_PRICE_BACKFILL_INTERVAL_MINUTES`
  minutes -- 1440, a day, by default -- plus once at startup when the newest `close` row's
  `recorded_at` is older than one interval, which also counts successes. It asks Kraken's
  OHLC endpoint for each of its two pairs and writes every committed close, then asks
  Coinbase Exchange for any BTC/USD days before the earliest stored close -- twelve calls
  the first time, none once filled. While both Kraken pairs fail, a crash loop costs two
  calls per restart, plus up to Coinbase's twelve while its range is unfilled.

A third thing reaches a **chain** provider, and it is a timer too: **the balance rebuild**
(spec 038), every `PORTFOLIO_BALANCE_REBUILD_INTERVAL_MINUTES` minutes -- 1440 by default --
plus once at startup when the newest `reconstructed_balances.rebuilt_at` is older than one
interval. It reads each active wallet's addresses' whole confirmed history, one address at a
time through the same shared client, so the per-host floor counts its requests with the
sync's. It has no coordinator either: no endpoint asks for a rebuild.
`portfolio rebuild-balances` is the same work on demand.

There is no coordinator and no join for either price timer, because there is no endpoint that
can ask for one: nothing in a request path may reach a price vendor, which is the contract.
`portfolio refresh-prices` and `portfolio backfill-prices` are the same work on demand, each in
a process of its own with a client of its own.

**No timer sleeps a whole interval after a restart that found nothing due.** It sleeps what
is left, rounded up to a whole second, so a deploy resumes the schedule rather than pushing
it back -- the first version pushed it back, and every deploy left prices stale for most of
an hour.

**Four timers reach a provider, each its own task with its own switch, and they share no
state.** A fifth, the backup timer (#22), reaches none. `services/scheduler.py` is generic
over what it ticks -- it takes "when did this last happen" and "do it" -- so the timers are
instances rather than loops, and none can stop another. They are separate because they
answer to different vendors, or to the same vendor on a different schedule:

- chain indexes that ban you for asking too often;
- market-data APIs where the primary answers every configured pair in one call;
- one market-data endpoint that answers 720 daily closes for one pair per call, and has a new
  one to give once a day. Folding it into the hourly refresh would be 24 times the calls for
  the same rows.

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
  failing over. That is #75.
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
  call that could bind one. No chain or price provider has a log call at all. The
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
- **Kraken is the only source of daily closes, and it keeps 720 days.** The backfill has no
  fallback, so a Kraken outage leaves the days it lasts as `observed` rows until a later run
  reaches them, which is fine for 720 days and a gap after that. Days that had already left the
  window when the backfill first ran are not recoverable from Kraken at all. PR 4 of the
  owner's plan (spec 037, *Scope*) adds Coinbase candles for older BTC days; KAS before
  2024-11-19, its first day on Kraken, has no source yet.
- **Whether a committed close can change is unverified.** Assumed not; the daily rewrite would
  pick up a correction inside the window either way.
- **`parse_rate_limit` has no production exerciser.** Measured on 2026-09-23: neither Kaspa
  endpoint sends a `ratelimit-*` header, and Esplora was never claimed to. It is tested
  against synthesised headers only, which is to say it is code that looks tested and is not
  known to work against any real server. It stays because the criterion is explicit and a
  self-hosted index without a CDN may send them -- recorded here because unexercised code
  that looks tested is how a green suite lies.
