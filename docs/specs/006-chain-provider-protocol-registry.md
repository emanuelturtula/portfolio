# 006 — Chain provider protocol and registry

Issue: #6
Status: draft

## Problem

Nothing in the backend can ask a chain a question. `providers/` is an empty package with a
docstring, and the two balance providers that M2 needs (#7 Bitcoin, #8 Kaspa) would each
have to invent their own HTTP client, their own retry rule, their own idea of what a
balance is and their own way of being found by a service. Two inventions of the same thing
is how they drift apart, and the third chain is then a refactor rather than a file.

This change lands the seam and nothing that travels through it.

## Scope

- `AddressBalance`, `ChainCapabilities`, `ProviderHealth` and the `ChainProvider` protocol.
- A typed error hierarchy for provider failures, including the registry's unknown-chain error.
- An explicit decorator registry keyed by `ChainKey`.
- A shared `httpx.AsyncClient` factory whose transport carries timeouts, bounded retry with
  jitter, a per-host rate limiter and `Retry-After` handling.
- Request logging that cannot carry a wallet address.
- `docs/providers.md`: what a new chain must implement.
- `httpx` moves from a dev dependency to a runtime dependency.

## Non-goals

- **No concrete provider.** Bitcoin is #7 and Kaspa is #8. `providers/chains/` lands empty,
  which has a consequence for criterion 6 that is dealt with explicitly below.
- **No lifespan wiring.** The client is process-wide by construction -- the rate limiter's
  state lives on the transport, which lives on the client -- but nothing calls a provider
  yet, so creating and closing one in `main.py` would be an unused resource held open for
  the life of the application. #10 wires it when it has a caller. `docs/providers.md`
  records that as work #10 owns.
- **No settings.** Timeouts and retry bounds are module constants. Promoting one to
  `PORTFOLIO_PROVIDER_*` is a change an operator's measurement should drive, not a guess
  made before anything has ever made a request.
- **No endpoint, no service, no table, no migration, no frontend.**
- **No `Decimal` at this boundary.** Balances are integer base units; see Design.

## Design

### Layout

| Path | Holds |
|---|---|
| `providers/base.py` | `AddressBalance`, `ChainCapabilities`, `ProviderHealth`, `ChainProvider`, `align_balances`, `chunk_addresses` |
| `providers/errors.py` | `ProviderError` and its subclasses |
| `providers/http.py` | `build_http_client`, `RetryingTransport`, `RetryPolicy`, `HostRateLimiter`, `parse_retry_after`, `strip_query`, `request_target` |
| `providers/registry.py` | `ChainProviderRegistry`, `CHAIN_PROVIDERS`, `register_chain_provider`, `get_chain_provider` |
| `providers/chains/__init__.py` | the explicit one-line import per provider. Empty today. |
| `docs/providers.md` | the contract a new chain implements |

`providers` sits above `db` and `config` and beside `repositories` in the existing
`import-linter` layers contract, so importing `portfolio.domain.money` and
`portfolio.domain.chains` is legal and importing `portfolio.services` is not. No contract
change is needed; the tester adds no new one.

### Balances are integers, and the shape is self-describing

```python
@dataclass(frozen=True, slots=True)
class AddressBalance:
    address: str      # the canonical form the provider was asked about
    confirmed: int    # base units: satoshis, sompi
    decimals: int
```

Both target APIs return integers. Converting to `Decimal` at the provider boundary would
add a rounding decision for no benefit and make provider tests approximate instead of
exact. `amount()` converts on demand through `domain.money.from_base_units`, so the one
conversion rule stays in the one module that owns it.

`decimals` is carried on the balance as well as on the capabilities, deliberately: a
snapshot that outlives the provider instance has to be interpretable without it.

**There is no `pending` or `unconfirmed` field.** Only one of the two target chains
distinguishes mempool from confirmed, and a field that one provider always sets to zero
makes zero ambiguous between "nothing pending" and "this chain cannot tell you". If #7
wants mempool visibility it adds the field *and* something that expresses "not answerable
here", which is a decision with a caller behind it rather than one made in advance.

### `fetch_balances` returns one result per requested address, in order

A provider is asked about a sequence and answers about the same sequence: same length,
same order, each entry carrying the address it is about. An address with no history is a
zero, not an omission -- that is what it means on chain.

The invariant is not left to discipline. `align_balances(requested, found, decimals=...)`
builds the result from a mapping the provider parsed out of its response, and it is where
the failure modes are decided:

| Case | Outcome |
|---|---|
| requested address missing from `found` | zero balance |
| `found` carries an address that was not requested | `ProviderResponseError` |
| a negative base-unit count | `ProviderResponseError` |
| the same address requested twice | `ValueError` |

The extra-address case is the one worth stating. A batch API answering about something we
did not ask about is a correlation bug, and silently dropping the entry would hide it
behind a plausible-looking total.

### Batching is a capability

```python
@dataclass(frozen=True, slots=True)
class ChainCapabilities:
    chain_key: ChainKey
    decimals: int
    max_addresses_per_call: int   # >= 1; 1 means "cannot batch"
```

An integer rather than a boolean, because the boolean is derivable from it
(`can_batch = max_addresses_per_call > 1`) and the integer is not derivable from the
boolean. Esplora takes one address per call; Kaspa's REST API takes a list with a cap.
`chunk_addresses(addresses, capabilities)` splits a request into calls the provider can
actually make, so a caller sizes its work from the declaration instead of assuming. A
capability nothing consumes is decoration.

`__post_init__` refuses `decimals < 0` and `max_addresses_per_call < 1`.

### The protocol is checked statically, never with `isinstance`

```python
class ChainProvider(Protocol):
    @property
    def capabilities(self) -> ChainCapabilities: ...
    def validate_address(self, raw: str) -> ValidatedAddress: ...
    async def fetch_balances(self, addresses: Sequence[str]) -> Sequence[AddressBalance]: ...
    async def health(self) -> ProviderHealth: ...
```

**`ChainProvider` is deliberately not `@runtime_checkable`.** `isinstance` against a
runtime-checkable protocol compares attribute *names* and nothing else: a class whose
`fetch_balances` takes the wrong arguments, or is not a coroutine function, passes. That
is a verifier that can only confirm its own account -- the failure this project has now
found six times. The real check is `mypy --strict` deciding assignability, which is why
criterion 7 is written the way it is.

`validate_address` is synchronous, delegates to `domain.chains.validate_address`, and is
the reason a caller can tell a mistyped address from an unreachable API without a round
trip.

### The registry is a class with a module-level default

`ChainProviderRegistry` holds `dict[ChainKey, Callable[[httpx.AsyncClient], ChainProvider]]`.
`CHAIN_PROVIDERS` is the process-wide instance and `@register_chain_provider(ChainKey.X)`
decorates a provider class, returning it unchanged -- a class whose `__init__` takes the
shared client already *is* the factory.

A class rather than module globals so that a test builds its own empty registry and
exercises every path without mutating shared state and without a restore fixture. A test
that has to put a global back is a test that shares state with the thing it verifies.

- An unknown key raises `UnknownChainError`, which carries the key and the sorted set of
  registered keys. Criterion 5.
- Registering the same key twice raises `DuplicateProviderError`. A copy-pasted decorator
  silently shadowing the provider above it is a bug that presents as wrong balances.
- No `pkgutil.walk_packages`. A missing import should be an obvious `UnknownChainError` at
  the call site, not a mysterious 404 later.

### Durations are integer milliseconds, because `float` is banned here

`backend/tests/security/test_no_float.py` bans float literals and the name `float` in
`domain/`, `services/` and `providers/`. `base_backoff_seconds: float = 0.25` fails the
gate. Every duration in `providers/http.py` is therefore an `int` of milliseconds, named
`*_ms`, and the single conversion to the seconds `anyio.sleep` and `httpx.Timeout` want is
`value_ms / MILLISECONDS_PER_SECOND` -- a name divided by a name, which the AST walk does
not report and which is not money.

This is not an evasion of rule 2 and the code says so in a comment. Rule 2 exists because a
portfolio that adds fills in binary floating point reports a total that is wrong and never
says so. A sleep duration cannot corrupt a balance. Integer milliseconds is also simply the
better representation for a value tests compare exactly.

Jitter uses `secrets.randbelow`, injected as `jitter: Callable[[int], int]` so a test can
make it deterministic. `random.uniform` would return a float and would trip ruff's S311;
`randbelow` returns an `int` and does neither.

### Retry, rate limiting and timeouts live in the transport

`RetryingTransport(httpx.AsyncBaseTransport)` wraps the real transport, so every request
made through the client is covered whether or not its caller remembered. A helper function
callers must remember to use is the failure rule 8 was written against.

- **Timeouts:** connect, read, write and pool, all explicit. `httpx`'s default is 5 s on
  everything and no total ceiling; a public API that accepts a connection and then stalls
  would hold a sync open indefinitely.
- **Retries:** `max_attempts` total attempts, not retries. Retried on transport errors and
  on 429 and 5xx. Never on any other 4xx -- a 400 retried three times is three identical
  wrong requests.
- **Only idempotent methods by default.** `retry_methods` defaults to `{"GET", "HEAD"}`.
  Kaspa's batch balance endpoint is a read expressed as a `POST`, so #8 opts in explicitly.
  A visible line in a diff, which is the house rule for making something less safe.
- **Full jitter**, `sleep_ms = randbelow(min(cap_ms, base_ms * 2 ** attempt))`. Not
  "backoff plus a little random": the additive form leaves every client's retries clustered
  where the exponential put them, and only full jitter actually decorrelates.
- **`Retry-After` is honoured and capped.** `parse_retry_after(value, now) -> int | None`
  is pure, takes the clock as an argument, and handles both RFC 9110 forms -- delay-seconds
  and HTTP-date. A hostile or broken server sending `Retry-After: 86400` must not hang the
  sync for a day, so the value is clamped to `max_backoff_ms`. A date that parses to the
  past yields zero, not a negative sleep. An unparseable value is ignored in favour of the
  computed backoff rather than raising: a malformed header is not a reason to fail a
  request that would otherwise succeed on the next attempt.
- **Per-host rate limiter:** `HostRateLimiter` enforces a minimum interval between requests
  to the same host, on `time.monotonic` rather than the wall clock, so an NTP step cannot
  make it sleep for hours. A leaky bucket of size one rather than a token bucket with a
  burst allowance: it is verifiable against an injected clock without a timing assertion,
  and this application polls a handful of addresses on a schedule rather than bursting. A
  burst allowance is a later change if a measurement asks for it.

Both the clock and the sleep are injected, so no test in this change asserts on elapsed
wall-clock time. A verdict that depends on host speed is the sixth costume from #5.

### A logged URL cannot carry an address, and the query string is not the whole story

Criterion 4 asks for the query string to be removed, and that is necessary -- an exchange
provider will later sign its requests there, so the signature and the key that produced it
would otherwise ride along in any URL that reaches a log.

**It is not sufficient for a chain provider, and the issue does not say so.** Esplora's
endpoint is `GET /address/:address` -- `/api/address/...` on a deployment that mounts the
API under a prefix -- and Kaspa's is `GET /addresses/{address}/balance`. Both put the
address in the *path*, confirmed against their published APIs. Stripping the query does
nothing for either, and an address in a log is the disclosure `SENSITIVE_KEY_FRAGMENTS`,
`hide_parameters=True` and the whole of #5 exist to prevent.

Two functions, because they answer two different questions:

- `strip_query(url) -> httpx.URL` removes the query, the fragment **and any userinfo**.
  This is criterion 4 verbatim, unit-tested, and what an exchange provider uses.
- `request_target(request) -> str` is what the transport actually logs:
  `"{scheme}://{host}/{label}"`, where the label comes from
  `request.extensions["endpoint"]` -- a constant the provider sets, such as
  `"address_balance"`. **The path is never logged.** A request with no label logs
  `"<unlabelled>"` in its place, so the default is to disclose nothing and labelling is an
  opt-in to saying more.

Deny-by-default, same shape as rule 8: adding an endpoint protects it, and saying more
about one is a deliberate edit. The alternative -- scanning each path segment for something
address-shaped -- is the approach #44 already argues against: it is slow, it breaks on a
truncated address, and it is a guess dressed as a control.

Success logs at debug, a retry at warning with the attempt number and the delay, a final
failure at error. None of them carries a response body.

### Rejected alternatives

| Rejected | Why |
|---|---|
| `httpx.AsyncHTTPTransport(retries=...)` | retries connection failures only, never a 429 or a 5xx, and cannot honour `Retry-After` |
| an ABC instead of a `Protocol` | forces every provider to import and inherit; a protocol lets a test fake satisfy it structurally, which is what criterion 7 checks |
| `pkgutil.walk_packages` auto-discovery | turns a missing import into a runtime 404; the issue rules it out and is right |
| `Decimal` at the provider boundary | both APIs return integers; converting early adds rounding and makes provider tests approximate |
| a retry helper function | a caller can forget it; a transport cannot be forgotten |
| `@runtime_checkable` + `isinstance` | compares names, not signatures; passes for a broken fake |

## API contract

None. This change adds no endpoint and no route, so rule 8's allowlist is untouched and
`backend/tests/test_openapi.py` should report no drift. A tester finding OpenAPI drift here
has found a bug, not a fixture to update.

## Data model

None. No table, no column, no migration.

## Acceptance criteria

Verbatim from #6, numbered, with interpretations marked.

1. `ChainProvider` protocol with `validate_address` (pure, offline), `fetch_balances`
   (always takes a sequence) and `health`.
2. `ChainCapabilities` declares whether the provider can batch, since one target API can
   and the other cannot -- batching is a capability, not an assumption.
3. A shared `httpx` client factory with timeouts, retry with jitter, a per-host rate
   limiter, and `Retry-After` handling.
4. **URLs are logged with the query string removed**, verified by a test -- an exchange
   provider will later sign requests in the query string.
   *Interpretation, and an extension:* satisfied verbatim by `strip_query`. Because the
   address is in the path for both target chains, the transport additionally never logs a
   path at all. Criterion 4 is met and then exceeded; the extension is tested as its own
   criterion, 4b.
5. The registry raises a typed error for an unknown chain key.
6. A test asserts every module in `providers/chains/` is actually registered, so adding a
   file without wiring it fails CI.
   *Interpretation:* `providers/chains/` is empty in this change, so this assertion is
   **satisfiable by emptiness** -- the exact failure #5 catalogued. It is therefore built
   as a pure scanner driven against planted fixtures as well as the real directory, plus a
   count pinned against a literal that #7 must raise. See the test plan.
7. A `FakeChainProvider` satisfies the protocol under `mypy --strict`.
   *Interpretation:* the fake lives in `backend/tests/`, not in `providers/chains/`, so
   that criterion 6's scan does not demand it be registered and no test-only class ships in
   the image. `mypy` already covers `tests`.
8. `docs/providers.md` documents what a new chain must implement.

## Test plan

| # | Criterion | Test |
|---|---|---|
| 1 | protocol members | `tests/providers/test_protocol.py::test_the_protocol_members_are_the_pinned_set` |
| 1 | `validate_address` is offline | `tests/providers/test_protocol.py::test_validate_address_delegates_to_the_domain_registry` |
| 1 | order and length preserved | `tests/providers/test_base.py::test_every_requested_address_gets_exactly_one_result_in_order` |
| 1 | missing address is a zero | `tests/providers/test_base.py::test_an_address_with_no_history_is_zero_not_an_omission` |
| 1 | unrequested address refused | `tests/providers/test_base.py::test_an_answer_about_an_address_we_did_not_ask_about_is_refused` |
| 1 | negative balance refused | `tests/providers/test_base.py::test_a_negative_base_unit_count_is_refused` |
| 1 | duplicate request refused | `tests/providers/test_base.py::test_the_same_address_requested_twice_is_refused` |
| 1 | `amount()` conversion | `tests/providers/test_base.py::test_amount_converts_through_the_domain_rule` |
| 2 | `can_batch` derived, not stored | `tests/providers/test_base.py::test_capabilities_declare_batching_as_a_size` |
| 2 | invalid capabilities refused | `tests/providers/test_base.py::test_capabilities_refuse_a_call_size_below_one` |
| 2 | chunking honours the size | `tests/providers/test_base.py::test_chunk_addresses_never_exceeds_the_declared_call_size` |
| 3 | timeouts are all explicit | `tests/providers/test_http.py::test_every_timeout_is_set_not_only_the_default` |
| 3 | retries a 5xx and a 429 | `tests/providers/test_http.py::test_a_server_error_and_a_throttle_are_retried` |
| 3 | never retries a 400 | `tests/providers/test_http.py::test_a_client_error_is_returned_not_retried` |
| 3 | non-idempotent not retried | `tests/providers/test_http.py::test_a_post_is_not_retried_unless_the_policy_opts_in` |
| 3 | attempts are bounded | `tests/providers/test_http.py::test_the_attempt_count_is_a_ceiling` |
| 3 | jitter is full, not additive | `tests/providers/test_http.py::test_the_backoff_is_drawn_from_zero_to_the_exponential_bound` |
| 3 | `Retry-After` seconds | `tests/providers/test_retry_after.py::test_a_delay_in_seconds_is_honoured` |
| 3 | `Retry-After` HTTP-date | `tests/providers/test_retry_after.py::test_an_http_date_is_honoured` |
| 3 | `Retry-After` capped | `tests/providers/test_retry_after.py::test_an_absurd_delay_is_clamped_to_the_ceiling` |
| 3 | past date is zero | `tests/providers/test_retry_after.py::test_a_date_in_the_past_never_sleeps_backwards` |
| 3 | junk header ignored | `tests/providers/test_retry_after.py::test_an_unparseable_header_falls_back_to_the_computed_backoff` |
| 3 | limiter spaces one host | `tests/providers/test_rate_limiter.py::test_two_requests_to_one_host_are_spaced_by_the_interval` |
| 3 | limiter is per host | `tests/providers/test_rate_limiter.py::test_a_second_host_is_not_made_to_wait` |
| 3 | limiter uses monotonic | `tests/providers/test_rate_limiter.py::test_a_wall_clock_step_backwards_does_not_stall_the_limiter` |
| 4 | query string removed | `tests/providers/test_url_scrubbing.py::test_the_query_string_is_removed` |
| 4 | fragment and userinfo removed | `tests/providers/test_url_scrubbing.py::test_the_fragment_and_any_credentials_are_removed_too` |
| 4b | **no address reaches stdout** | `tests/security/test_provider_url_logging.py::test_no_log_line_from_a_request_contains_the_address_in_its_path` |
| 4b | unlabelled logs no path | `tests/security/test_provider_url_logging.py::test_an_unlabelled_request_logs_no_path_at_all` |
| 4b | retry and failure lines too | `tests/security/test_provider_url_logging.py::test_a_retried_and_a_failed_request_log_no_address_either` |
| 5 | typed unknown-chain error | `tests/providers/test_registry.py::test_an_unknown_chain_key_raises_the_typed_error` |
| 5 | error names the known keys | `tests/providers/test_registry.py::test_the_unknown_chain_error_lists_what_is_registered` |
| 5 | duplicate registration refused | `tests/providers/test_registry.py::test_registering_one_key_twice_is_refused` |
| 6 | scan finds an unwired module | `tests/providers/test_chain_modules.py::test_a_module_that_is_not_registered_is_reported` |
| 6 | scan is not vacuous | `tests/providers/test_chain_modules.py::test_the_expected_provider_modules_match_the_pinned_literal` |
| 6 | real directory is clean | `tests/providers/test_chain_modules.py::test_every_module_in_the_chains_package_is_registered` |
| 7 | fake satisfies the protocol | static, `tests/providers/fakes.py`, asserted by the gate's `mypy --strict` |
| 7 | the static check can fail | `tests/providers/test_protocol.py::test_mypy_rejects_a_provider_with_the_wrong_signature` |
| 8 | doc covers every member | `tests/providers/test_documentation.py::test_the_provider_document_names_every_protocol_member` |

### The three tests that carry the weight

**Criterion 6 must not pass by being empty.** The scan is a pure function
`unregistered_chain_modules(package_dir, registered) -> set[str]`. It is driven three ways:
against a `tmp_path` holding a module that is not registered, asserting it is reported;
against a `tmp_path` holding one that is, asserting it is not; and against the real
`providers/chains/` directory. The third is paired with `EXPECTED_PROVIDER_MODULES`, a
pinned literal that is `frozenset()` today and which #7 has to change -- so the empty state
is asserted rather than assumed, and the test starts failing the moment a file lands
without wiring.

**Criterion 4b reads stdout, not `capture_logs`.** `structlog.testing.capture_logs`
replaces the processor chain and then reports on the pipeline; in #5 it hid a real
production leak through a green gate. This test reuses the `production_logging` and
`capsys` fixtures already established in `tests/security/test_address_logging.py`, drives a
request through the real transport against an `httpx.MockTransport`, and asserts both that
the testnet address is absent from what stdout carried *and* that stdout carried a request
log at all. An absence assertion without that companion is satisfied by silence.

Addresses come from `tests/address_vectors.py`. Testnet only.

**Criterion 7's static check must be provable.** `tests/providers/fakes.py` carries
`_CONFORMS: ChainProvider = FakeChainProvider()` at module level, so the gate's
`mypy --strict` over `tests` decides assignability at no runtime cost. That alone would be
a guard nobody has seen fail, so `test_mypy_rejects_a_provider_with_the_wrong_signature`
writes a fake with a deliberately wrong `fetch_balances` to `tmp_path` and runs
`uv run mypy --strict --no-incremental` on it, asserting a non-zero exit and an error that
names the incompatible member. Watch for the mypy cache and for `follow_imports`: the
planted file has to be checked with `MYPYPATH` pointing at `src` and with the cache off, or
it passes for reasons that have nothing to do with the protocol.

## File ownership

Disjoint, and it covers configuration -- a file owned by nobody stalls the team, which is
what happened to `tsconfig.app.json` on #4.

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/providers/**`, `backend/pyproject.toml` (the `dependencies` and `dev` lists **only**), `backend/uv.lock`, `docs/providers.md` |
| tester | `backend/tests/**` |
| tech-lead | `docs/specs/006-*.md`, `docs/architecture.md`, `backend/.importlinter` if a contract turns out to be needed, and the `fail_under` line in `backend/pyproject.toml` |

`backend/pyproject.toml` is touched by two owners and that is a real hazard, so it is
sequenced rather than shared: backend-dev moves `httpx` during implementation, and the
coverage ratchet is applied by the tech lead after the tester reports the measured figure,
when backend-dev has stopped. Nobody else opens the file.

## Coverage

The floor is 99 and ratchets only upward. This change adds a package with a high branch
count -- retry arms, header parsing, limiter paths -- so the floor moves only if the
measured figure supports it. It is not lowered. If the measurement comes in under 99 the
answer is missing tests, not a smaller number.

## Risks

- **`httpx` becomes a runtime dependency.** It is already installed in every environment as
  a dev dependency and is a Starlette-adjacent library the image will carry either way, but
  `uv.lock` changes and the arm64 image build is the place that would notice. The #3 lesson
  applies: the suite runs at `environment="dev"` and the image builds at `prod`, so a green
  local gate is not evidence about the container.
- **The target APIs' shapes are confirmed; their limits are not.** Checked against primary
  sources while this was being implemented, so the table below is fact rather than
  recollection:

  | | Bitcoin (Esplora) | Kaspa (kaspa-rest-server) |
  |---|---|---|
  | single address | `GET /address/:address` | `GET /addresses/{address}/balance` |
  | batch | **none documented** | `POST /addresses/balances`, body `{"addresses": [...]}` |
  | response | `chain_stats` and `mempool_stats`, each with `funded_txo_sum` and `spent_txo_sum` | `[{"address": ..., "balance": ...}]` |
  | units | satoshis | sompi, 1 KAS = 1e8 |

  Three design decisions in this spec are load-bearing on that table and all three hold.
  The address is in the *path* on both, so criterion 4b is necessary rather than
  defensive. One batches and one does not, so `max_addresses_per_call` is an integer and
  not a boolean. Kaspa's batch is a read expressed as a `POST`, so the `retry_methods`
  opt-in has a real caller rather than a hypothetical one. The batch response being a list
  of `{address, balance}` is also why `align_balances` refuses an unrequested address: a
  list correlated by content is exactly the shape that can come back short or reordered.

  **Still unconfirmed:** neither API documents a rate limit, a `Retry-After` behaviour or a
  cap on the batch size. Esplora's documentation mentions none at all and points at
  self-hosting instead, which is a reason to have a client-side limiter rather than a
  reason not to -- there is no server contract to lean on. #7 and #8 correct these by
  changing a policy value or a capability integer, not this seam.

- **Esplora reports a confirmed balance as a derivation, not a number.**
  `funded_txo_sum - spent_txo_sum` from `chain_stats`. That belongs in #7, but it is worth
  recording here because it is also the evidence for this spec's "no `pending` field"
  decision: Esplora *does* expose `mempool_stats` and Kaspa exposes nothing of the kind, so
  the asymmetry the decision assumed is real and a shared field would be zero on one chain
  for two different reasons.
- **The `Retry-After` HTTP-date path needs a clock.** `parse_retry_after` takes `now` as an
  argument rather than reading one, which keeps it pure, but `email.utils.parsedate_to_datetime`
  can return a naive datetime for some inputs and ruff's DTZ rules will not catch a
  comparison against an aware one -- it raises at runtime instead. The naive case needs an
  explicit test, not a code review.
- **The transport is the only thing enforcing the log contract.** A provider that logs its
  own URL bypasses everything here. The source-walk test in
  `tests/security/test_address_logging.py` covers the wallet modules today; extending it to
  `providers/` is cheap and belongs with the first provider that has a log call of its own
  (#7), not with a package that has none.
