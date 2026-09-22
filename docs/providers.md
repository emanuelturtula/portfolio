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
| a negative base-unit count | `ProviderResponseError` |
| the same address requested twice | `ValueError` |

The third row is the one worth internalising. A batch API answering about something we did
not ask about is a correlation bug, and dropping the entry silently would hide it behind a
total that still looks plausible.

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
    response = await self._client.get(url, extensions={"endpoint": "address_balance"})
    response.raise_for_status()
except httpx.TransportError as error:
    raise ProviderUnavailableError("the chain did not answer") from error
except httpx.HTTPStatusError as error:
    raise ProviderUnavailableError("the chain returned an error") from error
```

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

Set an endpoint label on every request:

```python
response = await self._client.get(url, extensions={"endpoint": "address_balance"})
```

The transport logs `"{scheme}://{host}/{label}"` and **never the path**. Both current
vendors put the address in the path, so a log line built from the URL would disclose
exactly what the wallet registry refuses to disclose. A request with no label logs
`"<unlabelled>"`: the default says nothing, and saying more about an endpoint is an opt-in.

`strip_query(url)` exists separately and removes the query string, the fragment and any
userinfo. It is the rule `CLAUDE.md` states, and it is what a future **exchange** provider
will use, because one exchange signs its requests in the query string. It is not sufficient
for a chain provider: reaching for it to log a chain request would meet the letter of the
rule and leak the address anyway.

Three further rules, none of them optional:

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
- **Whether mempool or unconfirmed value is even expressible.** `AddressBalance` has no
  `pending` field, and that is the reason: Esplora exposes `mempool_stats`, the Kaspa REST
  balance endpoint exposes nothing of the kind. A shared field would read zero on one chain
  for two different reasons -- "nothing is pending" and "this chain cannot tell you" -- and
  the second is not a balance. Adding the field means also adding a way to say "not
  answerable here".
- **What the vendor's rate limit actually is.** See below: for both current vendors, nobody
  knows.

## Vendor facts, and the line between confirmed and assumed

The next person cannot tell a verified endpoint from a plausible one unless the difference
is written down, and will trust both equally.

### Confirmed against the published documentation

| | Bitcoin (Esplora) | Kaspa (kaspa-rest-server) |
|---|---|---|
| single address | `GET /address/:address` | `GET /addresses/{address}/balance` |
| batch | none documented | `POST /addresses/balances`, body `{"addresses": [...]}` |
| response | `chain_stats` / `mempool_stats`, each with `funded_txo_sum` and `spent_txo_sum` | `[{"address": ..., "balance": ...}]` |
| units | satoshis | sompi, 1 KAS = 1e8 |

Two consequences the design already reflects. The address is in the path on both, which is
why `request_target` logs a label instead of a path -- necessary, not defensive. And Esplora
documents no batch endpoint while Kaspa documents one, which is why
`max_addresses_per_call` is an integer.

### Not confirmed, because neither vendor documents it

- **Any rate limit.** Esplora's documentation mentions none and points at self-hosting
  instead. That is a reason to run our own limiter -- there is no server-side contract to
  lean on -- rather than a reason to skip one.
- **Any `Retry-After` behaviour.** The transport honours the header if it arrives, in both
  RFC 9110 forms, and clamps it to `RetryPolicy.max_backoff_ms`. Whether either vendor ever
  sends one is unknown.
- **Any cap on the Kaspa batch size.** The endpoint takes a list; the documentation does not
  say how long a list.
- **Pagination and retention.** Neither matters for a balance read. Both will matter for
  transaction history, and neither has been checked.

The defaults below are conservative guesses, chosen so that being wrong costs seconds per
sync rather than getting us refused by a free public index. They are a policy object and a
capability integer rather than literals in the request path, so correcting them with a
measurement is a change to a value.

| Setting | Default | Basis |
|---|---|---|
| `DEFAULT_MIN_HOST_INTERVAL_MS` | 250 | guess: 4 requests/second to one host |
| `RetryPolicy.max_attempts` | 3 | guess |
| `RetryPolicy.base_backoff_ms` | 250 | guess |
| `RetryPolicy.max_backoff_ms` | 30_000 | guess; the ceiling exists for a server that asks for a day |
| `CONNECT_TIMEOUT_MS` | 5_000 | guess |
| `READ_TIMEOUT_MS` | 20_000 | guess; a batch read legitimately takes longer than a handshake |
| `WRITE_TIMEOUT_MS` | 10_000 | guess |
| `POOL_TIMEOUT_MS` | 5_000 | guess |

Do not invent an endpoint path because a third-party wrapper uses it. Verify against the
vendor's own documentation, and record here what you confirmed and what you assumed, in
those words.

## Not done yet, and who owns it

- **Lifespan wiring.** `build_http_client()` is process-wide by construction -- the rate
  limiter's state lives on the transport, which lives on the client, so two clients would
  each keep their own idea of the interval and it would silently become half of what it
  says. Nothing calls a provider yet, so nothing builds one at startup; creating and
  closing it in `main.py` today would be an unused connection pool held open for the life
  of the application. **#10 owns building it in the lifespan and closing it there.**
- **Settings.** Every number above is a module constant. Promoting one to a
  `PORTFOLIO_PROVIDER_*` setting is a change an operator's measurement should drive, not a
  guess made before anything has ever made a request.
- **The source-walk test over `providers/`.** `backend/tests/security/test_address_logging.py`
  walks the wallet modules and fails on a log call that could carry an address. Extending it
  to `providers/` belongs with the first provider that has a log call of its own (#7), not
  with a package that has none.
