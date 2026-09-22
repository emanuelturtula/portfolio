# 007 — Bitcoin balance provider on the Esplora API

Issue: #7
Status: draft

## Problem

`providers/` has a protocol, a registry, a shared client and no provider. Nothing in this
application can read a Bitcoin balance, `providers/chains/` is empty, and the registry
answers `UnknownChainError` for every key it is asked about. #6 built the seam on the
explicit promise that the first real provider would travel through it; this is that
provider, and it is also the first chance to find out whether the seam actually fits.

It carries one debt from #6 as well. `request_target` guarantees that no *accidental* path
disclosure can reach a log, and says in its own docstring that a deliberate one is still
possible because the label is checked for shape rather than for membership. The completion
is an allowlist, and an allowlist was impossible while no provider had a label to put in
it.

## Scope

- `providers/chains/bitcoin.py`: an Esplora provider registered for `ChainKey.BITCOIN`,
  reading confirmed and pending balances, with failover between two instances.
- The endpoint-label allowlist in `providers/http.py`, and the first two labels in it.
- A signed `pending` field on `AddressBalance`, with `None` meaning "this chain cannot tell
  you", and the `align_balances` extension that fills it.
- A pure, offline network check in `domain/`: which Bitcoin network an address belongs to,
  so a provider configured for one network refuses an address from another before it builds
  a URL out of it.
- Three settings: the primary base URL, the fallback base URL, the network, and nothing
  else.
- The per-host rate floor raised from 250 ms to 1000 ms, and `docs/providers.md` updated
  with what was confirmed against the live documentation on 2026-09-22 and what was not.

## Non-goals

- **No per-address cache**, though the issue's prose asks for one. A provider instance is
  built per `registry.create()` call, so a cache on the instance would be dead on arrival;
  a module-level one would be shared mutable state whose staleness window nothing owns.
  More importantly, a cache that answers from memory turns "we did not read the chain" into
  a balance that looks read, which is the failure `ProviderUnavailableError` exists to
  prevent. **#10 owns it**: the scheduler is the layer that knows how often a read is
  allowed to repeat, and the snapshot table is where a previous reading already lives.
- **No lifespan wiring.** Still #10. Nothing builds a client at startup in this change.
- **No `pending` anywhere above the provider.** #10 stores it, #11 renders it. This change
  produces the number and stops.
- **No endpoint, no service, no table, no migration, no frontend.**
- **No xpub or zpub derivation.** #24.
- **No whole-request deadline.** #50, unchanged by this issue.
- **No change to what the wallets API accepts.** A wrong-network address is refused by the
  *provider*, at read time, not at registration; making registration network-aware changes
  #5's contract and needs a story for rows that already exist. Recorded as a follow-up.

## Design

### Layout

| Path | Change |
|---|---|
| `providers/chains/bitcoin.py` | new: `EsploraProvider`, its parser, its endpoint constants |
| `providers/chains/__init__.py` | the one wiring import |
| `providers/base.py` | `AddressBalance.pending`, `align_balances(..., pending=...)` |
| `providers/http.py` | `ENDPOINT_LABELS` allowlist; `DEFAULT_MIN_HOST_INTERVAL_MS` 250 to 1000 |
| `domain/addresses.py` | `BitcoinNetwork`, `bitcoin_network_of`, `AddressRejection.WRONG_NETWORK` |
| `config.py` | three settings |
| `docs/providers.md` | vendor facts confirmed on 2026-09-22; the label allowlist |
| `docs/operations.md` | the three settings, in the existing table style |

### Two instances, tried in order, and what counts as a reason to move on

`PORTFOLIO_BITCOIN_ESPLORA_URL` defaults to `https://mempool.space/api` and
`PORTFOLIO_BITCOIN_ESPLORA_FALLBACK_URL` to `https://blockstream.info/api`. Two scalar
settings rather than one list: pydantic-settings parses a `list[str]` out of the
environment as JSON, which is a thing nobody types correctly into a `.env` file at three in
the morning, and a self-hoster setting one URL and blanking the other should not have to
learn a syntax. An empty fallback means "one instance only".

Failover is per address and **sticky within a single `fetch_balances` call**: once an
endpoint fails, the remaining addresses in that call start at the next one. Reading twenty
addresses against an instance that just refused the first one is how a soft throttle
becomes the ban mempool.space's documentation warns about. The stickiness resets between
calls, because an instance that was throttled five minutes ago is the one we would rather
be using now.

| Outcome on an endpoint | Next step |
|---|---|
| transport error, or 5xx that outlived the transport's retries | try the next endpoint |
| 429 that outlived the transport's retries | try the next endpoint |
| any other 4xx | **stop**, `ProviderResponseError` |
| 200 whose body does not parse | **stop**, `ProviderResponseError` |
| every endpoint exhausted | `ProviderRateLimitedError` if the last failure was a 429, else `ProviderUnavailableError` |

A 4xx and a malformed body do not fail over, deliberately. The second instance runs the
same software against the same chain, so it produces the same refusal — and if it does
*not*, then two instances disagree about a request, which is a fact worth surfacing rather
than papering over with whichever answer came second.

### An address is validated before it is ever interpolated into a URL

`fetch_balances` puts every address through `self.validate_address` before it builds
anything. Two reasons, and the second is the one that matters.

The address arrives from a database column. Interpolating a database value into a URL path
is the shape of a path-traversal bug, and the only thing standing between it and
`GET /address/../../blocks/tip/height` is that somebody validated it first. After
validation the string is bech32 or base58check, which is to say alphanumeric with no slash,
no dot and no percent-escape, by construction rather than by inspection.

The lesser reason is that a caller handing over a string this chain cannot parse has made
the caller's kind of mistake, and gets `AddressInvalidError` — the same class of answer as
`align_balances`'s `ValueError` for a duplicate address, and distinct from the two errors
that describe a vendor.

### Network: a pure check in `domain/`, because the vendor does not document the failure

`domain/addresses.py` accepts `bc`, `tb` and `bcrt` and both mainnet and testnet version
bytes, on purpose: it answers "is this a Bitcoin address", which is a question about the
string. Which *network* it is on is a separate question, and the provider is the first
thing that has ever needed it, because an Esplora instance serves exactly one network.

```python
class BitcoinNetwork(StrEnum):
    MAINNET = "mainnet"
    TESTNET = "testnet"   # testnet3, testnet4 and signet are indistinguishable here
    REGTEST = "regtest"

def bitcoin_network_of(canonical: str) -> BitcoinNetwork: ...
```

Pure, offline, derived from the HRP for bech32 and from the version byte for base58check,
and it lives in `domain/` because it reads the same prefix tables the codec already owns,
and two copies of a prefix table is how they drift.

`PORTFOLIO_BITCOIN_NETWORK` defaults to `mainnet`. `EsploraProvider.validate_address`
raises `AddressInvalidError(AddressRejection.WRONG_NETWORK)` for an address on another
network, before any request.

**Why this is worth a setting rather than left to the vendor.** Esplora's published API
documents no error response for an invalid address at all — checked on 2026-09-22 — so "the
instance will return 400" is an assumption about unspecified behaviour, and the failure it
would hide is the expensive one: a balance read from the wrong chain is a number, not an
error, and nothing downstream can tell it from a right one.

**The residual, stated plainly.** `tb1` is testnet3, testnet4 and signet alike, and testnet
base58 version bytes cover regtest too. An operator who points the base URL at signet while
holding testnet4 addresses gets confident, wrong answers, and no check built out of the
address can see it. The address does not carry the fact. Recorded in `docs/providers.md`;
it is a configuration mistake with a real consequence and no local detection.

### `pending` is signed, optional, and its `None` means something

#6 refused a `pending` field and named the condition on which it would be reasonable: the
field *plus* a way to say "not answerable here", so that zero is never ambiguous. This
change meets that condition rather than overriding it.

```python
pending: int | None = None   # None: this chain cannot answer. An int: it did.
```

**The number is signed, and that is not a detail.** `mempool_stats.funded_txo_sum -
spent_txo_sum` is a net delta, not a balance: an outgoing payment sitting in the mempool
spends a confirmed output and funds nothing, so it reads negative, which is exactly right
and exactly what a naive "balances cannot be negative" guard would reject. So
`align_balances` applies its negative check to `confirmed` and **not** to `pending`, and
the docstring says why at the guard rather than in a commit message.

`align_balances(requested, found, *, decimals, pending=None)` gains one optional mapping.
When it is `None`, every result carries `pending=None`; when it is given, the same
unrequested-address and whole-number rules apply to it as to `found`, because a batch that
correlates wrongly correlates wrongly in both halves.

Spendable is `confirmed + pending`. Nothing in this change computes it; #11 does, and it
gets a sign that already works.

### Parsing is by hand, and that is a disclosure decision

The response shape, confirmed against Blockstream's published `API.md` on 2026-09-22:

```json
{"address": "...", "chain_stats": {"funded_txo_sum": 1, "spent_txo_sum": 0},
                   "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0}}
```

Parsed by a hand-written function rather than by a pydantic model. **A pydantic
`ValidationError` renders the input that failed, and the input here is a response body
containing the owner's address.** That exception would travel into a log through
`logger.exception`, which is #44 all over again — the same defect #5 found in
`services/wallets.py`, arriving by a different door. Every rejection raised here names the
field and the type and never the value.

The parser refuses, as `ProviderResponseError`: a body that is not JSON; a body that is not
an object; a missing or non-object `chain_stats`; a missing, non-integer or boolean
`funded_txo_sum` or `spent_txo_sum`; `spent > funded` in `chain_stats`, which would make a
confirmed balance negative and cannot happen on a chain that is telling the truth; and an
`address` field that is absent or not the address we asked about. The last one catches a
cache or a proxy answering about somebody else, which is the correlation failure
`align_balances` already refuses for batches and which a single-address API can produce just
as easily. `mempool_stats` absent is *not* an error — it yields `pending=None`, since an
instance that does not report a mempool is one that cannot answer rather than one answering
zero.

Order matters: status first, body second. A 502 carrying an HTML error page is an
unavailable upstream, not a schema error, and deciding that from the body would file it
under "needs a human" forever.

### The endpoint-label allowlist

`ENDPOINT_LABEL`'s pattern stays, as a shape check on the constants themselves, and
membership in a named allowlist becomes the gate:

```python
ADDRESS_BALANCE: Final = "address_balance"
BLOCK_TIP_HEIGHT: Final = "block_tip_height"
ENDPOINT_LABELS: Final[frozenset[str]] = frozenset({ADDRESS_BALANCE, BLOCK_TIP_HEIGHT})
```

Anything not in the set renders `UNLABELLED`, whatever its shape — so the truncated address
that passes the pattern (lower-case, alphanumeric, under 32 characters) no longer reaches a
log. Same shape as `PUBLIC_API_PATHS`: adding an endpoint protects it, and saying more about
one is a visible edit to a named constant.

A frozen constant rather than a `register_endpoint_label()` call, because a registration
function makes the set depend on which modules happened to be imported, and a label that
works in production and renders `<unlabelled>` in a test is worse than either outcome
consistently.

### Health, and what it is allowed to say

`GET /blocks/tip/height`, documented, cheap, and it names no address. It returns a plain
integer body; the provider requires it to parse as a non-negative integer, so an instance
returning an HTML holding page is unhealthy rather than healthy-and-wrong.

`health()` never raises. `detail` is short, and it carries a reason and at most which
endpoint position answered (`primary`, `fallback`) — never a URL, never a body, never an
address. It tries the endpoints in order and reports healthy if either answers.

### One request per second, not four

`DEFAULT_MIN_HOST_INTERVAL_MS` goes from 250 to 1000. mempool.space's documentation, read on
2026-09-22, states that exceeding its limits returns 429 and that repeatedly exceeding them
can get the caller banned, while publishing no numbers; being banned from a free public
index is a failure that outlives the sync that caused it, and one request per second is the
issue's own floor. The value is a shared default, so Kaspa (#8) inherits it — which is
acceptable because Kaspa batches, and a per-host override table would be a mechanism built
for a second vendor that has not complained yet.

`fetch_balances` is sequential. Esplora declares `max_addresses_per_call = 1`, so twenty
addresses is twenty calls spaced by the limiter; a `gather` would hand the limiter twenty
simultaneous acquisitions and turn a floor into a queue whose depth nobody bounded.

### Rejected alternatives

| Rejected | Why |
|---|---|
| a `pending` field that is always an `int` | zero would mean both "nothing pending" and "cannot tell"; #6 refused the field for exactly this |
| a pydantic model for the response | its `ValidationError` renders the offending input, and the input contains the address |
| failing over on a 4xx or a parse error | the second instance runs the same software; a differing answer is a fact to surface, not to absorb |
| one list setting for the two URLs | pydantic-settings wants JSON in the environment for a list; two scalars are typed correctly by a human |
| a per-host interval override table | a mechanism for a second vendor that has not asked for one; raise the shared floor instead |
| `register_endpoint_label()` at import | makes what may be logged depend on import order |
| deriving the network from the base URL | "testnet" appears in a self-hosted URL only by luck |
| a per-address cache in the provider | the instance is per call, so it would be dead code; and a cached balance looks exactly like a read one |

## API contract

None. No endpoint, no route, no schema. `backend/tests/test_openapi.py` must report no
drift; a tester who finds drift here has found a bug rather than a fixture to refresh.

## Data model

None. No table, no column, no migration.

## Acceptance criteria

Verbatim from #7, numbered, plus the two the #6 comment proposes and one interpretation.

1. Correct balances for P2PKH, P2SH, P2WPKH and P2TR fixtures.
2. An unfunded address returns zero, not an error.
3. Offline validation covers bech32, bech32m and base58check, and rejects wrong-checksum
   and wrong-network addresses.
   *Interpretation:* the codecs are #5's and are not rewritten; the provider delegates and
   is tested through its own `validate_address`. "Wrong network" is read in both senses —
   an unknown human-readable prefix, which `domain/` already refuses, and an address on a
   Bitcoin network this provider is not configured for, which is new here.
4. 429 triggers backoff and failover to the secondary instance.
   *Interpretation:* backoff is the transport's, already built and tested in #6; this change
   owns the failover and asserts that the two compose — the retries happen first, and only a
   429 that survives them moves to the fallback.
5. Malformed JSON raises a typed schema error rather than propagating a parse error.
6. `docs/providers.md` records the rate limit as an unpublished, verified-by-observation
   risk.
7. Fixtures use testnet or regtest addresses only.
8. Endpoint labels come from a named allowlist rather than from a pattern match, so a label
   that is not on it renders `<unlabelled>` regardless of its shape.
9. A test asserts that a truncated address used as a label does **not** reach the log.
10. *Added here:* pending is reported as a signed net mempool delta, and as `None` when the
    response carries no mempool figures — the condition #6 set for the field existing at
    all.

## Test plan

Addresses come from `backend/tests/address_vectors.py`. Testnet, signet and regtest only.

| # | Criterion | Test |
|---|---|---|
| 1 | four script types read correctly | `tests/providers/chains/test_bitcoin.py::test_each_script_type_reads_its_confirmed_balance` |
| 1 | confirmed is funded minus spent | `tests/providers/chains/test_bitcoin.py::test_confirmed_is_the_difference_of_the_chain_sums` |
| 1 | order and length preserved over several calls | `tests/providers/chains/test_bitcoin.py::test_every_requested_address_comes_back_in_order` |
| 2 | unfunded address is a zero | `tests/providers/chains/test_bitcoin.py::test_an_address_with_no_history_reads_zero_not_an_error` |
| 3 | bech32, bech32m, base58 accepted | `tests/providers/chains/test_bitcoin.py::test_validate_address_accepts_every_published_vector` |
| 3 | every single-character corruption refused | `tests/providers/chains/test_bitcoin.py::test_a_one_character_corruption_is_refused` |
| 3 | wrong network refused offline | `tests/providers/chains/test_bitcoin.py::test_an_address_from_another_network_is_refused_without_a_request` |
| 3 | and no request was made | the same test: the mock transport records zero requests |
| 3 | the network function itself | `tests/domain/test_bitcoin_network.py::test_every_prefix_and_version_byte_maps_to_its_network` |
| 4 | 429 retried, then failed over | `tests/providers/chains/test_bitcoin.py::test_a_throttled_primary_is_retried_and_then_falls_over_to_the_fallback` |
| 4 | failover is sticky within a call | `tests/providers/chains/test_bitcoin.py::test_a_failed_primary_is_not_asked_again_for_the_rest_of_the_call` |
| 4 | and not sticky across calls | `tests/providers/chains/test_bitcoin.py::test_the_next_call_starts_at_the_primary_again` |
| 4 | both exhausted raises rate-limited | `tests/providers/chains/test_bitcoin.py::test_both_instances_throttled_raises_the_rate_limited_error` |
| 4 | a 4xx does not fail over | `tests/providers/chains/test_bitcoin.py::test_a_client_error_is_not_retried_against_the_fallback` |
| 5 | a non-JSON body | `tests/providers/chains/test_bitcoin.py::test_a_body_that_is_not_json_raises_the_typed_error` |
| 5 | missing and mistyped fields | `tests/providers/chains/test_bitcoin.py::test_a_response_missing_its_sums_raises_the_typed_error` |
| 5 | a float sum is refused | `tests/providers/chains/test_bitcoin.py::test_a_sum_that_is_not_a_whole_number_is_refused` |
| 5 | spent exceeding funded | `tests/providers/chains/test_bitcoin.py::test_a_negative_confirmed_balance_is_refused` |
| 5 | an answer about another address | `tests/providers/chains/test_bitcoin.py::test_an_answer_echoing_a_different_address_is_refused` |
| 5 | no message carries the address | `tests/providers/chains/test_bitcoin.py::test_no_parser_rejection_names_the_address` |
| 5 | 5xx is unavailable, not a schema error | `tests/providers/chains/test_bitcoin.py::test_an_html_error_page_behind_a_5xx_is_unavailable_not_malformed` |
| 6 | the document records what was verified | `tests/providers/test_documentation.py::test_the_document_records_the_rate_limit_as_unpublished` |
| 7 | no mainnet address anywhere | `tests/security/test_address_logging.py::test_fixtures_contain_no_mainnet_address` (existing, now covering the new files) |
| 8 | a well-shaped label not on the list is dropped | `tests/providers/test_url_scrubbing.py::test_a_label_that_is_not_on_the_allowlist_renders_unlabelled` |
| 8 | every allowlisted label matches the shape | `tests/providers/test_url_scrubbing.py::test_every_allowlisted_label_is_also_well_shaped` |
| 8 | the allowlist is not empty | `tests/providers/test_url_scrubbing.py::test_the_allowlist_names_the_labels_this_release_uses` |
| 9 | a truncated address never reaches stdout | `tests/security/test_provider_url_logging.py::test_a_truncated_address_used_as_a_label_does_not_reach_the_log` |
| 9 | a real balance read logs no address | `tests/security/test_provider_url_logging.py::test_a_balance_read_logs_no_address_on_any_line` |
| 10 | pending is the signed mempool delta | `tests/providers/chains/test_bitcoin.py::test_pending_is_the_signed_net_mempool_delta` |
| 10 | an outgoing payment reads negative | `tests/providers/chains/test_bitcoin.py::test_a_spend_in_the_mempool_reads_as_a_negative_pending` |
| 10 | no mempool figures means None | `tests/providers/chains/test_bitcoin.py::test_a_response_without_mempool_figures_reports_pending_as_unknown` |
| 10 | `align_balances` defaults to None | `tests/providers/test_base.py::test_a_provider_that_says_nothing_about_pending_reports_none` |
| 10 | pending keeps its sign through alignment | `tests/providers/test_base.py::test_a_negative_pending_survives_alignment_and_a_negative_confirmed_does_not` |
| wiring | the module is registered | `tests/providers/test_chain_modules.py` — `EXPECTED_PROVIDER_MODULES` becomes `{"bitcoin"}` and `EXPECTED_REGISTERED_KEYS` becomes `("bitcoin",)` |
| wiring | capabilities declare no batching | `tests/providers/chains/test_bitcoin.py::test_the_provider_declares_that_it_cannot_batch` |
| health | the tip height answers | `tests/providers/chains/test_bitcoin.py::test_health_reports_healthy_when_an_instance_answers` |
| health | never raises, never names an address | `tests/providers/chains/test_bitcoin.py::test_health_reports_unhealthy_without_raising_and_without_an_address` |
| pacing | the floor is one second | `tests/providers/test_rate_limiter.py::test_the_default_interval_is_the_documented_floor` |

### The three that carry the weight

**Criterion 9 reads stdout, not `capture_logs`.** It extends the file #6 built for exactly
this, reusing the `production_logging` and `capsys` fixtures, and it pairs every absence
assertion with a positive companion proving a request line was actually emitted. An absence
check over an empty capture passes for the wrong reason, which #5 catalogued twice.

**The failover tests count requests per host, not just the final result.** A provider that
asked the primary twenty times and then succeeded on the fallback returns the same balances
as one that moved on after the first refusal. Only the request log tells them apart, and the
ban risk lives entirely in the difference.

**`test_no_parser_rejection_names_the_address` drives every rejection arm.** Each malformed
body is built around a real testnet address from the vectors, and the assertion is that the
address appears in neither `str(exc)` nor `exc.args` — a rejection whose message quotes the
body is the #44 shape, and it is easiest to introduce while writing a helpful error message.

## File ownership

Disjoint, and it covers the configuration files, because a file owned by nobody stalls the
team — what happened to `tsconfig.app.json` on #4.

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/providers/**`, `backend/src/portfolio/domain/addresses.py`, `backend/src/portfolio/domain/chains.py`, `backend/src/portfolio/config.py`, `docs/providers.md`, `docs/operations.md` |
| tester | `backend/tests/**` |
| tech-lead | `docs/specs/007-*.md`, the `fail_under` line in `backend/pyproject.toml`, `backend/.importlinter` if a contract turns out to be needed |

`backend/pyproject.toml` is opened by the tech lead only, and only after the tester has
reported a measured coverage figure and backend-dev has stopped editing. No new runtime
dependency is expected; if one becomes necessary, backend-dev says so and the tech lead
makes the edit, so the file still has one writer.

## Coverage

The floor is 99.5 and only ratchets. This change adds a parser with nine refusal arms,
failover, and two networks, so the floor moves only if the measured figure supports it. A
measurement below 99.5 is missing tests, not a smaller number.

## Risks

- **Neither vendor documents an error body for an invalid address**, confirmed on
  2026-09-22. Every mapping from a status to a `ProviderError` is therefore written against
  the status alone, which is the part both vendors do have to get right.
- **mempool.space's rate limit is unpublished and enforced by ban.** Verified on
  2026-09-22: the documentation states that exceeding the limits returns 429 and that
  repeatedly doing so may result in a ban, and it publishes no numbers. One request per
  second is a guess made from the shape of the warning rather than from a measurement, and
  the first real evidence will be a 429 in a production log.
- **No test in this change makes a real network request**, so every vendor fact rests on
  documentation read on one day. A response shape that changes silently produces a
  `ProviderResponseError` rather than a wrong number, which is the failure mode chosen
  deliberately — but it is still a failure that will first be seen in production.
- **testnet3, testnet4 and signet are one network to this code.** An operator can point at
  the wrong one and get confident wrong answers; nothing built from the address can detect
  it.
- **Raising the shared rate floor slows every provider**, including one that has not been
  written. If #8 finds it too slow for a batch endpoint, the answer is the per-host override
  table this spec rejected, not a lower shared floor.
