# 008 — Kaspa balance provider on the public REST API

Issue: #8
Status: done

## Problem

The registry answers for one chain. A Kaspa wallet can be registered — #5's codec validates
`kaspa:` and `kaspatest:` addresses at full checksum strength — and then nothing can read its
balance, so a portfolio holding both assets reports one of them and silently omits the other.

This is also the issue that tests whether #6's seam was designed or merely described. It is
the first provider that **batches**, the first whose read is a `POST`, and the first to meet a
vendor that documents its failures. Where the seam does not fit, this change is where the
seam moves.

## Scope

- `providers/chains/kaspa.py`: a provider registered for `ChainKey.KASPA`, reading balances
  singly and in batches, with the same failover shape as Bitcoin.
- **Extraction of the endpoint-failover loop out of `bitcoin.py` into `providers/endpoints.py`**,
  used by both providers. See Design: this is the moment the seam promised.
- A per-request retry opt-in in `providers/http.py`, so that a read expressed as a `POST` can
  be retried without making every future `POST` retryable.
- `ratelimit-*` response headers honoured when present, clamped, in `HostRateLimiter`.
- `kaspa_network_of` in `domain/addresses.py`, and the configured-network refusal.
- Three settings: the base URL, the fallback base URL, the network.
- `docs/providers.md`: what was confirmed against the live OpenAPI document, what was not, and
  the mainnet-examples trap.

## Non-goals

- **No transaction history, no UTXO listing, no pagination.** Balances only. Pagination and
  retention are unchecked for this vendor and belong with whatever first needs them.
- **No lifespan wiring.** Still #10.
- **No fallback from a refused batch to single reads.** A batch the server refuses is a
  configured batch size that is too large, which is a value to correct rather than a path to
  code around. The refusal names the size.
- **No endpoint, no table, no migration, no frontend.**

## Design

### Layout

| Path | Change |
|---|---|
| `providers/chains/kaspa.py` | new: `KaspaProvider`, its two parsers, its endpoint constants |
| `providers/chains/__init__.py` | the wiring import |
| `providers/endpoints.py` | new: `Endpoint`, `EndpointSet`, the failover loop, `_Failure` |
| `providers/chains/bitcoin.py` | loses its private copy of the loop, gains the shared one |
| `providers/http.py` | `IDEMPOTENT_EXTENSION`; `ratelimit-*` parsing; two more labels |
| `domain/addresses.py` | `KaspaNetwork`, `kaspa_network_of` |
| `config.py` | three settings |
| `docs/providers.md` | the vendor facts, the trap, the batch-size guess |

### The failover loop is extracted, and this is the right moment rather than the earliest one

`bitcoin.py` owns `_Instance`, `_Failure`, `_configured_instances` and the loop in `_read`.
Copying them into `kaspa.py` would be the second invention of one thing, which `CLAUDE.md`
names as how two copies drift apart — and `docs/providers.md` promises that the *third* chain
is a file rather than a refactor, which is only true if the second chain does the extracting.

It is also the right moment on the evidence: with one provider the shared part was a guess,
and with two it is observable. The parts that are genuinely common — ordered endpoints,
sticky-within-a-call failover, the `_Failure` record, classification by the last failure —
are exactly the parts review already corrected once in #7, so extracting them means the
correction cannot be un-made by a provider that copied the old shape.

What stays per-provider: the base URL settings it reads, its labels, its parsers, and its
capabilities. `EndpointSet.read(path, label, start)` returns the body and the index, and the
provider decides what the body means.

This makes the diff larger than a size:M issue implies. The alternative is a copy, and the
copy is how the next reviewer finds two failover rules that disagree.

### Batching, and the number nobody documents

`GET /addresses/{kaspaAddress}/balance` for one address; `POST /addresses/balances` with
`{"addresses": [...]}` for more than one. That split is criterion 2 read literally, and it is
also the honest one: the single read is a `GET`, which is retryable and cacheable by every
intermediary, and paying for a `POST` to ask about one address buys nothing.

**`max_addresses_per_call` is a guess and the spec says so.** The OpenAPI document declares
`addresses` as an array of strings with no `maxItems`, and the operation description names no
ceiling — confirmed against the live document on 2026-09-22. The value is `64`: large enough
that any realistic portfolio is one request, small enough that a request body stays a few
kilobytes. `chunk_addresses` already sizes calls from the declaration, so correcting it is a
change to a constant.

A batch the server refuses raises `ProviderResponseError` naming **the size of the batch**,
never its contents. That is the number an operator can act on, and the contents are the
owner's holdings.

### A read expressed as a `POST`, retried without making every `POST` retryable

#6 anticipated this and proposed that #8 opt in by widening `RetryPolicy.retry_methods`. That
is wrong now that the consequence is visible: the policy lives on the transport, the transport
is process-wide by construction, and widening it would make **every** future `POST` retryable
— including an exchange request that places an order, where a retry after a transport error
can double a trade. One provider's convenience would silently become another's duplicate.

So the opt-in is per request, deny-by-default, and visible at the call site:

```python
IDEMPOTENT_EXTENSION: Final = "idempotent"
...
await client.post(url, json=payload,
                  extensions={ENDPOINT_EXTENSION: ADDRESS_BALANCES, IDEMPOTENT_EXTENSION: True})
```

`RetryingTransport` retries a request whose method is in `retry_methods` **or** which declares
itself idempotent. Same shape as the endpoint label: the default says nothing and saying more
is a deliberate edit at the one call site it applies to.

**The body must be bytes, never a stream.** `httpx` consumes a request stream on the first
attempt, so a retried streamed body replays as empty and the server answers about no addresses
— a correlation bug that `align_balances` would catch as "the response carried addresses that
were not requested" only by luck. The provider passes `json=`, and a test asserts the second
attempt carries the same body as the first.

### `ratelimit-*` headers, honoured and distrusted

Criterion 4. The headers are the IETF draft's lower-case trio — `ratelimit-limit`,
`ratelimit-remaining`, `ratelimit-reset` — plus the older `x-ratelimit-*` spelling. `httpx`
matches headers case-insensitively, so the two spellings are two lookups, not four.

A pure function `parse_rate_limit(headers) -> RateLimitHint | None`, and a limiter that takes
the hint: when `remaining` is zero, the next request to that host waits `reset` seconds rather
than the ordinary interval.

**Every value is clamped and every failure is ignored rather than raised**, for the reason
`parse_retry_after` already gives: a malformed header is not a reason to fail a request that
would otherwise succeed, and a server asking us to wait a day must not stall a sync for a day.
`reset` is clamped to `RetryPolicy.max_backoff_ms`. A negative or non-numeric value is ignored.

Nothing here reads the wall clock: `reset` is a delay in seconds by the draft, and the limiter
runs on `time.monotonic`, so an NTP step cannot turn a pause into an hour.

**Measured against the live service on 2026-09-23, and the measurement says this parser will
not run.** Neither `GET /info/health` nor the balance endpoint returns any `ratelimit-*` or
`x-ratelimit-*` header. What they return instead is `Server: cloudflare`, `cf-cache-status`
and `CF-RAY`: the API sits behind a CDN, so the throttle, when it arrives, is **Cloudflare's**
-- a 429 carrying `Retry-After`, which `parse_retry_after` has honoured since #6, or a 403 for
a block, which #7's failover now moves on from.

The criterion says "when present", so the parser is built and the criterion is met. It is
built knowing nothing in production exercises it, which is why it stays small and pure and says
so in its own docstring.

### The balance endpoint is cached at the edge for eight seconds

`Cache-Control: public, max-age=8`, on the balance response and on the error alike, in front of
a Cloudflare cache. A balance read can therefore be served from an edge cache rather than from
the index.

Eight seconds is immaterial to a sync that runs on a schedule of minutes, so this changes no
code. It is recorded because the mechanism is invisible in the OpenAPI document, and the next
person debugging "why did two reads a second apart return the same number" deserves to find
the answer written down rather than rediscover it against a CDN.

### `pending` is `None`, not zero — a deliberate departure from the issue text

The issue says "there is no confirmed/pending split, so pending is always zero — document why
rather than leaving a reader to wonder". It was written before #7 existed, and #7 settled the
question the other way: `AddressBalance.pending` is `int | None`, and **`None` is what "this
chain cannot tell you" means**.

Zero would be the wrong answer here in the precise way #6 predicted. A Kaspa balance of zero
pending and a Bitcoin balance of zero pending would render identically while meaning different
things — one says "nothing is in the mempool", the other says "nobody asked the mempool". A
dashboard cannot honour a distinction it was never given.

So `KaspaProvider` never sets `pending`, `align_balances` is called without the mapping, and
every Kaspa balance carries `None`. The docstring says why, which is the part of the issue's
request that still applies.

### Health is not a ping, and this vendor says so

`GET /info/health` returns `{kaspadServers: [{kaspadHost, serverVersion, isUtxoIndexed,
isSynced, p2pId, blueScore}], database: {isSynced, blueScore, blueScoreDiff,
acceptedTxBlockTime, acceptedTxBlockTimeDiff}}`, and its own description says it returns
**503 if the database lags by around ten minutes or no nodes are synced**. Confirmed against
the live document on 2026-09-22.

That is a richer answer than Esplora's tip height and it changes what healthy means. A node
that is reachable but not synced returns balances that are *stale and well-formed*, which is
the failure this project keeps finding in other clothes: a wrong number is worse than an
error. So:

> Healthy requires a 200, `database.isSynced`, and at least one `kaspadServers` entry with
> both `isSynced` **and** `isUtxoIndexed` true.

`isUtxoIndexed` is the one a reader would drop as redundant. It is not: a node without the UTXO
index is synced and simply cannot answer a balance query, which is precisely the state where a
ping-shaped health check says yes and every read fails.

**`kaspadHost` must never leave the provider.** It names the vendor's internal node topology
and `ProviderHealth.detail` is rendered in an operations view and reaches a log. `detail` says
how many nodes were synced and indexed, never which or where.

### Network: the prefix is the network, and unlike Bitcoin it is unambiguous

`domain/addresses.py` already keeps the prefix in both stored forms, exactly so that "the same
payload on another network can be told apart" — so `kaspa_network_of` reads the prefix and is
three lines. `PORTFOLIO_KASPA_NETWORK` defaults to `mainnet`, and `validate_address` refuses
an address from another network before a request is built, as Bitcoin's does.

Worth recording as a contrast rather than a copy: **Bitcoin's check collapses testnet3,
testnet4 and signet into one answer and #7 had to record that residual. Kaspa has no such
collapse** — `kaspa`, `kaspatest` and `kaspadev` are three distinct prefixes, each covered by
the 40-bit checksum, so the same payload checksums differently on each. The check is exact
here and approximate there, and a reader comparing the two modules deserves to be told which
is which.

This vendor also documents a 422 for an address it will not accept, where Esplora documented
no error at all. The offline refusal is still preferable — it costs no request and it names the
configured network rather than echoing a validation error — but the argument for it is weaker
here, and the spec would rather say that than pretend the two cases are identical.

**Measured on 2026-09-23, and it turns the argument back the other way.** Sending one of our
own published `kaspatest:` vectors to `api.kaspa.org` returns 422 with the server's own rule
quoted in the body: the path must match `^kaspa:[a-z0-9]{61,63}$`.

Two things follow, and the second is the one that matters.

The vendor's path validation is **mainnet-only by construction** -- the prefix is a literal in
its regex -- which confirms that one instance serves one network, and that a configured-network
refusal describes something real rather than something imagined.

And the vendor checks **prefix, charset and length, and not the checksum**. A mistyped mainnet
address that still matches that regex is accepted and answered with a balance, which for a
wallet that does not exist is `0`. That is precisely the failure #5's codec was built to
prevent: a typo that reports an empty wallet forever and looks no different from an empty one.
**Our offline validation is strictly stronger than the vendor's**, and that is now a
measurement rather than a preference.

### Parsing, by hand, and the duplicate-entry case that is new

Same rule as #7: a hand-written parser, no pydantic, because a `ValidationError` renders the
input and the input is a response body full of the owner's addresses.

The batch response is an **array**, which introduces a correlation failure the single-address
shape could not have:

| Case | Outcome |
|---|---|
| an entry whose `address` was not requested | `ProviderResponseError` (via `align_balances`) |
| **two entries for the same address** | `ProviderResponseError` — new here |
| a requested address with no entry | zero, by `align_balances` |
| `balance` absent, non-integer, boolean or negative | `ProviderResponseError` |
| the body is not an array | `ProviderResponseError` |

The duplicate case is the one worth stating. The parser builds a `dict` for `align_balances`,
and a `dict` keeps the last value silently — so two entries for one address, with different
balances, would resolve to whichever the vendor happened to send second and no assertion in
`align_balances` could ever see it. It is refused at the point where both values still exist.

### Two decisions taken during implementation, recorded here rather than in a commit message

**The JSON trust boundary was extracted too, not only the failover loop.** `bitcoin._decode`
and `_require_object` became `base.decode_json` and `base.require_json_object`, because Kaspa
needs the identical boundary and because that catch clause -- `(ValueError, RecursionError)`
rather than the narrower pair a reader would write -- is itself a #7 review correction. Two
copies of a correction is one correction away from being undone. Messages are byte-identical
and `test_bitcoin.py` did not move.

**The `GET`/`POST` split is decided per call, not per request.** A chunk holding one address
is read with the single-address `GET`, so sixty-five addresses at a call size of sixty-four is
one batch plus one single read rather than two batches. Criterion 2 is satisfied either way --
more than one address was requested and the batch endpoint was used -- and the spec's own
argument for the split applies to a trailing chunk exactly as it does to a request of one: a
`POST` for a single address gives up retry-by-default and every intermediary cache for nothing.

### Rejected alternatives

| Rejected | Why |
|---|---|
| copying the failover loop into `kaspa.py` | two copies of a rule review already corrected once |
| widening `RetryPolicy.retry_methods` to include `POST` | the transport is process-wide; it would make an exchange order retryable |
| `pending = 0` for Kaspa, as the issue's text asks | zero would be indistinguishable from "nothing is pending"; #6 and #7 both settled this |
| the batch endpoint for a single address | a `POST` where a `GET` exists, losing retry-by-default and every cache |
| falling back to single reads when a batch is refused | hides a batch size that is simply too large |
| a health check that only asks for a 200 | a synced node without a UTXO index passes it and answers nothing |
| `ratelimit-*` parsed in the provider | it is a property of a host, and the limiter is what owns pacing a host |

## API contract

None. No endpoint, no route, no schema; `backend/tests/test_openapi.py` must report no drift.

## Data model

None. No table, no column, no migration.

## Acceptance criteria

Verbatim from #8, numbered, with interpretations marked.

1. Sompi to KAS conversion is exact, via integer base units.
2. The batch endpoint is used when more than one address is requested.
3. An unfunded address returns zero, not an error.
4. `ratelimit-*` headers are respected when present; 429 backs off.
5. 5xx maps to a retryable "temporarily unavailable" error, not a permanent one.
6. Offline `kaspa:` address validation; if the checksum variant proves ambiguous, degrade to
   prefix, charset and length validation and say so explicitly in the docstring.
   *Interpretation:* **no degradation is needed.** #5 implemented the full 40-bit CashAddr
   checksum over the network prefix, verified against vectors from two unrelated publishers,
   with an exhaustive single-character corruption sweep. This issue's fallback clause is
   satisfied by not being reached, and the provider delegates.
7. `docs/providers.md` notes the undocumented limits and the mainnet-examples trap.
8. Fixtures use `kaspatest:` addresses only.
9. *Added here:* `pending` is `None`, not zero — the issue's text predates #7's decision and
   zero would mean two different things across two chains.
10. *Added here:* a retried `POST` replays the same body, and no other `POST` becomes
    retryable as a result.

## Test plan

Vectors from `backend/tests/address_vectors.py`. `kaspatest:` only.

| # | Criterion | Test |
|---|---|---|
| 1 | sompi converts exactly | `tests/providers/chains/test_kaspa.py::test_sompi_converts_to_kas_through_the_domain_rule` |
| 1 | no float anywhere in the path | `tests/security/test_no_float.py` (existing, now scanning `chains/kaspa.py`) |
| 2 | one address uses the single GET | `tests/providers/chains/test_kaspa.py::test_a_single_address_uses_the_single_address_endpoint` |
| 2 | more than one uses the batch POST | `tests/providers/chains/test_kaspa.py::test_more_than_one_address_uses_the_batch_endpoint` |
| 2 | a batch is split at the declared size | `tests/providers/chains/test_kaspa.py::test_a_batch_larger_than_the_call_size_is_split` |
| 2 | order survives several batches | `tests/providers/chains/test_kaspa.py::test_every_requested_address_comes_back_in_order_across_batches` |
| 3 | unfunded is a zero | `tests/providers/chains/test_kaspa.py::test_an_address_the_batch_omits_reads_zero_not_an_error` |
| 4 | headers parsed, both spellings | `tests/providers/test_rate_limit_headers.py::test_both_header_spellings_are_read` |
| 4 | exhausted budget paces the next call | `tests/providers/test_rate_limiter.py::test_a_zero_remaining_budget_waits_for_the_reset` |
| 4 | an absurd reset is clamped | `tests/providers/test_rate_limit_headers.py::test_an_absurd_reset_is_clamped_to_the_ceiling` |
| 4 | junk headers are ignored, not raised | `tests/providers/test_rate_limit_headers.py::test_an_unparseable_header_is_ignored` |
| 4 | 429 still backs off | `tests/providers/chains/test_kaspa.py::test_a_throttle_is_retried_and_then_moves_on` |
| 5 | 5xx is unavailable, not permanent | `tests/providers/chains/test_kaspa.py::test_a_server_error_is_a_retryable_unavailability` |
| 5 | 422 is a response error | `tests/providers/chains/test_kaspa.py::test_the_documented_422_is_a_response_error_not_an_outage` |
| 6 | every published vector accepted | `tests/providers/chains/test_kaspa.py::test_validate_address_accepts_every_published_vector` |
| 6 | every single-character corruption refused | `tests/providers/chains/test_kaspa.py::test_a_one_character_corruption_is_refused` |
| 6 | wrong network refused, zero requests | `tests/providers/chains/test_kaspa.py::test_an_address_from_another_network_is_refused_without_a_request` |
| 6 | the network function | `tests/domain/test_kaspa_network.py::test_every_prefix_maps_to_its_network` |
| 7 | the document records both | `tests/providers/test_documentation.py::test_the_document_records_the_kaspa_limits_and_the_example_trap` |
| 8 | no mainnet address anywhere | `tests/security/test_address_logging.py::test_fixtures_contain_no_mainnet_address` (existing) |
| 9 | pending is unknown, never zero | `tests/providers/chains/test_kaspa.py::test_pending_is_unknown_because_this_chain_cannot_answer_it` |
| 10 | a retried POST replays its body | `tests/providers/test_http.py::test_a_retried_idempotent_post_sends_the_same_body_again` |
| 10 | no other POST becomes retryable | `tests/providers/test_http.py::test_a_post_without_the_extension_is_still_not_retried` |
| extraction | both providers share one loop | `tests/providers/test_endpoints.py::*` — the loop's own tests move here |
| extraction | Bitcoin behaviour is unchanged | `tests/providers/chains/test_bitcoin.py` (existing, must pass untouched) |
| batch | a duplicate entry is refused | `tests/providers/chains/test_kaspa.py::test_two_entries_for_one_address_are_refused` |
| batch | an oversized batch names the size | `tests/providers/chains/test_kaspa.py::test_a_refused_batch_names_its_size_and_no_address` |
| health | synced and indexed is healthy | `tests/providers/chains/test_kaspa.py::test_health_requires_a_synced_database_and_an_indexed_node` |
| health | synced but unindexed is not | `tests/providers/chains/test_kaspa.py::test_a_node_without_a_utxo_index_is_not_healthy` |
| health | no host reaches the detail | `tests/providers/chains/test_kaspa.py::test_health_detail_never_names_a_backend_node` |
| wiring | the module is registered | `tests/providers/test_chain_modules.py` — `{"bitcoin", "kaspa"}`, keys `("bitcoin", "kaspa")` |

### The three that carry the weight

**The extraction must be proved by Bitcoin's existing tests passing untouched.** If
`test_bitcoin.py` needs edits to accommodate the shared loop, the extraction changed behaviour
and the change is unreviewed. That file is the control, and it is the reason the extraction
happens in an issue that already has a second provider to check the shape against.

**Criterion 10 is a test about a request that was already sent.** Asserting that the retry
happened is not enough; the assertion is on the **body of the second request**, captured by the
mock transport. A streamed body replays as empty and every other assertion in the suite still
passes — the balances come back wrong rather than missing.

**The duplicate-entry test must assert the refusal, not the resolution.** A parser that keeps
the first entry and one that keeps the last are both wrong and both plausible, and a test
asserting either one pins a coin flip. The only defensible outcome is a refusal while both
values are still visible.

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/providers/**`, `backend/src/portfolio/domain/addresses.py`, `backend/src/portfolio/config.py`, `docs/providers.md`, `docs/operations.md` |
| tester | `backend/tests/**` |
| tech-lead | `docs/specs/008-*.md`, the `fail_under` line in `backend/pyproject.toml`, `backend/.importlinter` |

## Coverage

The floor is 99.5, measured 99.85% at the end of #7 over 2533 units. The standard recorded in
`backend/pyproject.toml` is nine units of headroom; this change adds a parser, a header codec
and an extraction, so the floor moves only if the measurement supports it against that
standard.

## Risks

- **The batch ceiling is a guess.** No `maxItems`, no description, nothing in the operation.
  The first evidence will be a refused batch in production, which is why the refusal names the
  size.
- **`ratelimit-*` does not appear at all**, measured on 2026-09-23 against both endpoints.
  The parser is written against the IETF draft and tested against synthesised headers only,
  so nothing in production exercises it. Accepted deliberately, because the criterion is
  explicit and a self-hosted instance without a CDN may send them; recorded because
  unexercised code that looks tested is how a green suite lies.
- **The OpenAPI document uses real mainnet addresses as example values.** Nothing in this
  change copies one, and the existing mainnet scan over every test file is what keeps that
  true rather than a reviewer's attention.
- **Extracting the failover loop touches a file that shipped days ago**, and its correction in
  #7 came out of review rather than out of the plan. If the extraction quietly drops a case,
  Bitcoin loses a fix nobody re-derives.
- **The health check is stricter than the vendor's own.** The vendor returns 503 when its
  database lags; this provider additionally requires a UTXO-indexed synced node. If the field
  means something narrower than the schema suggests, this reports unhealthy where the vendor
  reports healthy — a false alarm rather than a false balance, which is the right direction to
  be wrong in, but it is a guess about a field's meaning and is recorded as one.

## What this plan got wrong

### The evidence for criterion 10 lived inside the one component that makes the failure impossible

The spec named the hazard precisely -- `httpx` consumes a request stream on the first
attempt, so a retried streamed body replays as empty -- and then named the test:
"a test asserts the second attempt carries the same body as the first".

**That test could not have failed.** `httpx.MockTransport.handle_async_request` begins with
`await request.aread()`, and `Request.aread` caches the bytes *and replaces a non-replayable
stream with a `ByteStream`*. Every mock-transport test in this suite therefore replays a body
that could never have been replayed in production. The implementer proposed the assertion,
the tester accepted it, and it took reading `inspect.getsource` on the pinned `httpx` to see
that the harness was answering the question instead of the code.

Closed by driving `RetryingTransport` over a hand-written transport that only iterates
`request.stream`, with a body that is an async iterable but **not** an async generator --
measured, because `httpx` raises `StreamConsumed` for a generator, which is loud, and
silently yields `b""` for anything else, which is the case that matters. Its falsification
control asserts `[payload, b""]`.

**Worth carrying past this issue: when a criterion is about a failure the harness itself
prevents, the harness needs its own falsification test.** Otherwise the green is the
harness's, not the code's.

### A test plan that names the parts can be complete and still not test the system

Criterion 4 was specified as a pure parser and a limiter method, and the test plan named
tests for both. Both were written, both passed, and the four lines joining them -- the
`observe(host, parse_rate_limit(...))` call in the transport -- could be deleted with 1144
tests still green. The parser was proven, the limiter was proven, and the feature was not.

This is #6's lesson arriving through the front door rather than the back: there, a shipped
default went unobserved because every test injected its own. Here, two halves went observed
and their join did not. The plan is where it was lost -- a test plan organised by *function*
produces tests organised by function, and nothing in it asks whether the system does the
thing.

### An expected value that the fixture could supply by accident

`test_a_refused_batch_names_its_size_and_no_address` asserted `str(len(THREE)) in message`,
which is `"3" in message`, against a fixture answering **413**. The status code contained the
answer, so the entire mitigation the Risks section leans on -- "the batch ceiling is a guess,
which is why the refusal names the size" -- was protected by an assertion that passed with the
naming removed.

The rule this earns: when asserting that a message contains a computed value, check that no
other part of the message can supply it, and look at the fixture's own constants first,
because they are the nearest source. The fix asserts the phrase and pins that the count's
digit cannot appear in the status.

### Extracting "the failover loop" was the right idea and the wrong boundary

The spec listed exactly what to share: `Endpoint`, `EndpointSet`, the loop, `_Failure`. The
JSON trust boundary was not on that list, and it needed the same treatment for the same
reason -- its catch clause, `(ValueError, RecursionError)` rather than the narrower pair
anyone would write, is itself a #7 review correction, and a correction that exists in two
copies is one edit away from existing in one.

The related miss is sharper. This spec argues at length that a retry opt-in belongs at the
call site, because a shared policy would make a future exchange order retryable. It then
introduced `EndpointSet.post`, the shared helper such a provider would reach for, with
`IDEMPOTENT_EXTENSION: True` hard-coded. **The rule was enforced at the transport and
contradicted one layer above it**, in a module this very change added. Review caught it;
`post` now takes a required `idempotent: bool` with no default.

### A note on what did not go wrong

The extraction control worked exactly as intended. `tests/providers/chains/test_bitcoin.py`
and its harness are absent from `git diff 5e16201..HEAD` up to `f6a17de`, and review diffed
the extracted module line by line against the merged Bitcoin provider to confirm every case
survived. Naming the control in the spec, and telling the tester in advance to refuse to
adjust that file, is what made a silent behaviour change impossible rather than unlikely.

`414` was deliberately left out of `BATCH_TOO_LARGE_STATUSES` although review named it: the
batch is a `POST` to a constant path with no query, so no batch size can lengthen the URI, and
a branch that cannot fire is a branch no test can check.
