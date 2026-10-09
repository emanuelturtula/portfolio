"""Two scripted Kaspa REST instances that record **who was asked, and how many times**.

The sibling of `tests/providers/chains/harness.py`, and it exists for the same reason: a
provider that hammered a throttled primary and then succeeded on the fallback returns
exactly the same balances as one that moved on after the first refusal, so only the
per-host request log tells them apart. `KaspaFake` records each request against the host
that received it, and the failover tests assert on `fake.counts` rather than only on
balances.

**It is a separate module rather than a generalisation of the Esplora one, deliberately.**
The two vendors do not share a response shape: Esplora answers a balance as a derivation
over two stat objects, Kaspa answers it as one integer, and Kaspa's batch answer is an
*array* with a correlation failure the single-address shape cannot produce. A harness
covering both would be a parameterised builder whose every call site had to say which
vendor it meant, which is how a fixture stops being readable.

**Nothing here sleeps and nothing here reads a clock.** The limiter, the jitter and the
sleep are injected, so no assertion in this suite is a measurement of how fast the machine
running it happened to be.

## The response shapes, and where they came from

Read off the live OpenAPI document on 2026-09-22:

* `GET /addresses/{kaspaAddress}/balance` -> `{"address": ..., "balance": <sompi>}`
* `POST /addresses/balances` with `{"addresses": [...]}` -> an **array** of those objects
* `GET /info/health` -> `{"kaspadServers": [...], "database": {...}}`, and its own
  description says it answers 503 when the database lags by around ten minutes or no node
  is synced.

**The document's example values are real mainnet addresses and not one of them is here.**
Every address in every body comes from `tests/address_vectors.py` and is `kaspatest:`;
`tests/security/test_address_logging.py::test_fixtures_contain_no_mainnet_address` proves
it mechanically over this file too, which is what makes that a control rather than a
promise.

`KASPAD_HOST` is a fictional internal node name carried in every health body on purpose:
`ProviderHealth.detail` is rendered in an operations view and reaches a log, and a test
that never put a node name in the body could not tell a provider that drops it from one
that never saw it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final

import httpx

from portfolio.config import Settings
from portfolio.providers.chains.kaspa import KaspaProvider
from portfolio.providers.http import HostRateLimiter, RetryPolicy, build_http_client

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# --------------------------------------------------------------------------------------
# The two fictional instances
# --------------------------------------------------------------------------------------
#
# `example` is reserved by RFC 2606 and resolves nowhere, so a test that somehow escaped
# its mock transport fails to connect rather than reaching somebody's real index. Rule 3
# forbids a real hostname in the repository regardless, and the shipped defaults -- which
# *are* a real vendor -- are pinned once, in `test_kaspa.py`, by reading `Settings()`
# rather than by being copied here.

PRIMARY_HOST: Final = "kaspa-primary.example"
FALLBACK_HOST: Final = "kaspa-fallback.example"
PRIMARY_URL: Final = f"https://{PRIMARY_HOST}"
FALLBACK_URL: Final = f"https://{FALLBACK_HOST}"

#: The vendor's own internal node identity, which must never leave the provider. Fictional,
#: and shaped like the thing it stands in for: `kaspadHost` names the operator's node
#: topology, and `detail` says how many nodes were synced and indexed, never which.
KASPAD_HOST: Final = "kaspad-node-7.internal.example"

#: A plausible blue score. Not a round number: a round one would make a parser that
#: returned a constant look correct.
BLUE_SCORE: Final = 93_417_622

#: A whole coin in sompi, and a smaller odd number. Kaspa has eight decimals, the same as
#: Bitcoin, and neither of these is a round power of ten in the other's units -- so a
#: dropped exponent or a swapped field is visible in the assertion rather than plausible.
ONE_COIN: Final = 100_000_000
DUST: Final = 54_321


# --------------------------------------------------------------------------------------
# Bodies, built by hand rather than by a model
# --------------------------------------------------------------------------------------


def balance_body(address: str, balance: int = 0) -> str:
    """The single-address body, exactly the shape the OpenAPI document declares."""
    return json.dumps({"address": address, "balance": balance})


def batch_body(entries: Sequence[tuple[str, int]]) -> str:
    """The batch body: an **array**, which is where the correlation failures live.

    Takes a sequence of pairs rather than a mapping, so a test can script two entries for
    the same address -- the case a `dict` cannot express and the one that would otherwise
    resolve silently to whichever the vendor happened to send second.
    """
    return json.dumps([{"address": address, "balance": balance} for address, balance in entries])


def health_body(
    *,
    nodes: Sequence[tuple[bool, bool]] = ((True, True),),
    database_synced: bool = True,
) -> str:
    """The `/info/health` body, with one entry per `(isSynced, isUtxoIndexed)` pair.

    `isUtxoIndexed` is the field a reader would drop as redundant. It is not: a node
    without the UTXO index is synced and simply cannot answer a balance query, which is
    precisely the state where a ping-shaped health check says yes and every read fails.

    Every node carries `kaspadHost`, `serverVersion` and `p2pId`, because the point of this
    fixture is that none of them reaches `ProviderHealth.detail`.
    """
    servers = [
        {
            "kaspadHost": f"{KASPAD_HOST}:{16110 + index}",
            "serverVersion": "0.15.2",
            "isUtxoIndexed": indexed,
            "isSynced": synced,
            "p2pId": f"p2p-{index}-a9f3c1",
            "blueScore": BLUE_SCORE - index,
        }
        for index, (synced, indexed) in enumerate(nodes)
    ]
    return json.dumps(
        {
            "kaspadServers": servers,
            "database": {
                "isSynced": database_synced,
                "blueScore": BLUE_SCORE,
                "blueScoreDiff": 0 if database_synced else 4_211,
                "acceptedTxBlockTime": 1_790_000_000_000,
                "acceptedTxBlockTimeDiff": 0 if database_synced else 612,
            },
        }
    )


# --------------------------------------------------------------------------------------
# Accepted transactions, for `address_history` (spec 038)
# --------------------------------------------------------------------------------------
#
# Shaped after the tn10 example measured on 2026-10-08: `block_time` in epoch
# **milliseconds**, `mass` and `previous_outpoint_index` as strings, and the two resolved
# input fields that `resolve_previous_outpoints=light` adds.

#: 2026-10-08T00:00:00Z in epoch milliseconds.
BLOCK_TIME_MS: Final = 1_791_417_600_000

#: The response header that carries the next page's cursor.
NEXT_PAGE_HEADER: Final = "X-Next-Page-Before"


def transaction_id_of(seed: int) -> str:
    """A transaction id built at run time -- the SHA-256 of the seed -- never a literal."""
    return hashlib.sha256(f"kaspa-fixture-{seed}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class KaspaTx:
    """One transaction as `full-transactions-page` renders it, described by what it does.

    `outputs` are `(address or None, sompi)`. `inputs` are `(address, sompi)` resolved, or
    `None` for an input whose `previous_outpoint_address` and `previous_outpoint_amount` are
    both `null` -- the unresolved case. `coinbase` renders `inputs` as `null`, which the
    schema allows and a coinbase transaction, spending nothing, is the natural owner of.
    """

    seed: int
    block_time: int = BLOCK_TIME_MS
    outputs: tuple[tuple[str | None, int], ...] = ()
    inputs: tuple[tuple[str, int] | None, ...] = ()
    coinbase: bool = False
    accepted: bool = True

    @property
    def transaction_id(self) -> str:
        return transaction_id_of(self.seed)

    def funded(self, address: str) -> int:
        return sum(amount for owner, amount in self.outputs if owner == address)

    def spent(self, address: str) -> int:
        return sum(spend[1] for spend in self.inputs if spend is not None and spend[0] == address)

    def document(self) -> dict[str, Any]:
        """The row, as a plain mutable document a test can break one field of."""
        inputs: list[dict[str, Any]] | None = None
        if not self.coinbase:
            inputs = [
                {
                    "transaction_id": self.transaction_id,
                    "index": index,
                    "previous_outpoint_hash": transaction_id_of(self.seed * 1_000 + index + 1),
                    "previous_outpoint_index": "0",
                    "previous_outpoint_address": spend[0] if spend is not None else None,
                    "previous_outpoint_amount": spend[1] if spend is not None else None,
                    "signature_script": "41a1b2c3",
                    "sig_op_count": "1",
                }
                for index, spend in enumerate(self.inputs)
            ]
        return {
            "subnetwork_id": "0" * 40,
            "transaction_id": self.transaction_id,
            "hash": transaction_id_of(self.seed + 5_555_555),
            "mass": "2036",
            "payload": None,
            "block_hash": [transaction_id_of(self.seed + 7_777_777)],
            "block_time": self.block_time,
            "version": 0,
            "is_accepted": self.accepted,
            "accepting_block_hash": transaction_id_of(self.seed + 9_999_999),
            "accepting_block_blue_score": BLUE_SCORE + self.seed,
            "accepting_block_time": self.block_time + 115,
            "inputs": inputs,
            "outputs": [
                {
                    "transaction_id": self.transaction_id,
                    "index": index,
                    "amount": amount,
                    "script_public_key": "20a1b2c3ac",
                    "script_public_key_address": owner,
                    "script_public_key_type": "pubkey",
                }
                for index, (owner, amount) in enumerate(self.outputs)
            ],
        }


def history_reply(address: str, transactions: Sequence[KaspaTx], **overrides: Any) -> Reply:
    """An instance serving `transactions` (newest first) with a count and balance that agree.

    The count is every distinct id, **accepted or not** -- the vendor counts every row of its
    address index, measured on 2026-10-09 -- and the balance is the net of the accepted ones.
    A test that changes nothing therefore gets a history that proves itself, and each
    incomplete arm is one override away: `tx_total=`, `balance=`, or a different reply after
    the paging.
    """
    accepted = {
        transaction.transaction_id: transaction
        for transaction in transactions
        if transaction.accepted
    }
    reply = Reply(
        balance=sum(
            transaction.funded(address) - transaction.spent(address)
            for transaction in accepted.values()
        ),
        tx_total=len({transaction.transaction_id for transaction in transactions}),
        rows=tuple(transaction.document() for transaction in transactions),
    )
    return replace(reply, **overrides)


def is_history_request(request: httpx.Request) -> bool:
    """Whether a request read a page of transactions."""
    return request.url.path.endswith("/full-transactions-page")


# --------------------------------------------------------------------------------------
# The script
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reply:
    """One scripted answer from one instance.

    The defaults describe a healthy instance holding nothing, so a test that only cares
    about *which* instance was asked writes `Reply()` and says nothing else. Everything a
    test does spell out is therefore the thing under test, which is what keeps a hundred
    lines of scaffolding from hiding the one value that matters.
    """

    status: int = 200
    balance: int = 0
    """What every address this reply is asked about holds, unless `balances` says otherwise."""
    balances: Mapping[str, int] = field(default_factory=dict)
    """Per-address balances. An address absent from it falls back to `balance`."""
    omit: frozenset[str] = frozenset()
    """Addresses to leave out of a batch answer entirely, which must read as zero."""
    duplicate: str | None = None
    """An address to answer **twice** in a batch, which must be refused rather than resolved."""
    duplicate_balance: int = 0
    body: str | None = None
    """A raw body, replacing the generated one. This is how a malformed answer is scripted."""
    headers: Mapping[str, str] = field(default_factory=dict)
    error: BaseException | None = None
    """Raised instead of answering, for the transport-failure arms."""
    nodes: Sequence[tuple[bool, bool]] = ((True, True),)
    """`(isSynced, isUtxoIndexed)` per backing node, for the health body."""
    database_synced: bool = True
    tx_total: int = 0
    """What `/transactions-count` answers as `total`."""
    rows: Sequence[Mapping[str, Any]] = ()
    """What `full-transactions-page` serves, newest first, as documents."""
    page_size: int | None = None
    """Rows per page before the boundary is completed; `None` takes the request's `limit`."""
    ignore_cursor: bool = False
    """Serve page one whatever `before` says: a vendor that never ends the history."""

    def render(self, request: httpx.Request) -> str:
        """The body this reply sends for `request`.

        The addresses are read back out of the request -- from the path for a single read
        and from the posted body for a batch -- rather than carried on the reply, so a
        scripted instance answers about whatever it was actually asked. That is what makes
        `test_an_answer_about_a_different_address_is_refused` a real test: the mismatch has
        to be overridden deliberately, with `body=`, rather than being the accident that a
        fixed fixture would make it.
        """
        if self.body is not None:
            return self.body
        if request.url.path.endswith("/balance"):
            address = requested_address(request)
            return balance_body(address, self._balance_of(address))
        if request.url.path.endswith("/transactions-count"):
            return json.dumps({"total": self.tx_total})
        if is_history_request(request):
            page, _cursor = self.history_page(request)
            return json.dumps(list(page))
        if request.method == "POST":
            return batch_body(self._batch_entries(posted_addresses(request)))
        return health_body(nodes=self.nodes, database_synced=self.database_synced)

    def response_headers(self, request: httpx.Request) -> dict[str, str]:
        """The headers this reply sends: the paging cursor when there is more, then `headers`.

        `headers` is applied last, so a test can replace or add a cursor the paging would
        not have produced -- a malformed one, or one that never runs out.
        """
        computed: dict[str, str] = {}
        if self.body is None and is_history_request(request):
            _page, cursor = self.history_page(request)
            if cursor is not None:
                computed[NEXT_PAGE_HEADER] = str(cursor)
        return {**computed, **self.headers}

    def history_page(
        self, request: httpx.Request
    ) -> tuple[Sequence[Mapping[str, Any]], int | None]:
        """One page and the cursor to the next, as the vendor was measured to page.

        Rows strictly before `before` (all of them on page one), `limit` of them -- or
        `page_size` -- and then **the boundary millisecond completed**: rows sharing the
        last row's `block_time` join the page, which is how a page of 500 came back with
        501. The cursor is the smallest `block_time` on the page, present only when rows
        remain after it.
        """
        before = request.url.params.get("before")
        remaining = [
            row
            for row in self.rows
            if before is None or self.ignore_cursor or int(row["block_time"]) < int(before)
        ]
        size = self.page_size if self.page_size is not None else int(request.url.params["limit"])
        page = remaining[:size]
        while page and len(page) < len(remaining):
            if remaining[len(page)]["block_time"] != page[-1]["block_time"]:
                break
            page.append(remaining[len(page)])
        if len(page) < len(remaining):
            return page, min(int(row["block_time"]) for row in page)
        return page, None

    def _balance_of(self, address: str) -> int:
        return self.balances.get(address, self.balance)

    def _batch_entries(self, addresses: Sequence[str]) -> list[tuple[str, int]]:
        entries = [
            (address, self._balance_of(address))
            for address in addresses
            if address not in self.omit
        ]
        if self.duplicate is not None:
            entries.append((self.duplicate, self.duplicate_balance))
        return entries


def requested_address(request: httpx.Request) -> str:
    """The address a single-address request is about: `/addresses/{address}/balance`.

    `httpx.URL.path` is URL-decoded, so the `:` in `kaspatest:q...` arrives as itself
    whether or not the client percent-encoded it on the wire.
    """
    return request.url.path.rsplit("/", 2)[-2]


def posted_addresses(request: httpx.Request) -> list[str]:
    """The addresses a batch request asked about, out of the body it actually sent.

    **Read from `request.content`, which is the byte payload that went to the server.**
    That is the whole reason criterion 10 is testable: a streamed body replays as empty on
    a retry, and a fake that took the addresses from anywhere else would answer correctly
    about a request that carried nothing.
    """
    document: Any = json.loads(request.content) if request.content else {}
    addresses = document.get("addresses", []) if isinstance(document, dict) else []
    return [str(address) for address in addresses]


class ScriptedInstance:
    """One Kaspa REST instance: a queue of replies whose last entry repeats.

    `ScriptedInstance(Reply(status=429))` is "always throttled" and
    `ScriptedInstance(Reply(status=429), Reply())` is "throttled once, then fine". The
    repetition matters for the retry arms: the transport may attempt three times, and a
    script that ran out would answer `IndexError` and turn a retry test into a crash.
    """

    def __init__(self, *replies: Reply) -> None:
        self._replies: tuple[Reply, ...] = replies or (Reply(),)
        self.requests: list[httpx.Request] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self._replies[min(len(self.requests) - 1, len(self._replies) - 1)]
        if reply.error is not None:
            raise reply.error
        return httpx.Response(
            reply.status,
            headers=reply.response_headers(request),
            content=reply.render(request),
        )


class KaspaFake:
    """Both instances behind one `httpx.MockTransport`, routed by host.

    Routing on the host rather than on a URL prefix is deliberate: the provider builds its
    own URLs out of a base URL and a path, and a fake that matched the whole URL would pass
    only for the exact string the test already believed the provider would produce.
    Matching the host means the test finds out what path the provider actually asked for.
    """

    def __init__(
        self,
        primary: ScriptedInstance | None = None,
        fallback: ScriptedInstance | None = None,
    ) -> None:
        self.primary = primary if primary is not None else ScriptedInstance()
        self.fallback = fallback if fallback is not None else ScriptedInstance()
        self._instances: dict[str, ScriptedInstance] = {
            PRIMARY_HOST: self.primary,
            FALLBACK_HOST: self.fallback,
        }
        #: Every request in the order it was made, across both hosts. Order matters for
        #: stickiness: "the primary was asked again" and "the primary was asked first" are
        #: different claims and the per-host counts alone cannot tell them apart.
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        instance = self._instances.get(str(request.url.host))
        if instance is None:  # pragma: no cover - a test asking an unscripted host is a bug
            message = f"the provider called an unscripted host: {request.url.host}"
            raise AssertionError(message)
        return instance.answer(request)

    @property
    def counts(self) -> dict[str, int]:
        """How many requests each host received, which is the ban risk in one number."""
        return {
            PRIMARY_HOST: len(self.primary.requests),
            FALLBACK_HOST: len(self.fallback.requests),
        }

    @property
    def hosts_in_order(self) -> list[str]:
        """The sequence of hosts asked, so stickiness is asserted rather than inferred."""
        return [str(request.url.host) for request in self.requests]

    def balance_requests(self) -> list[httpx.Request]:
        """Every request that read a balance, single or batch -- never the health probe."""
        return [request for request in self.requests if not request.url.path.endswith("/health")]

    def batches_asked_of(self, host: str) -> list[list[str]]:
        """The address lists each batch call carried, in order, for one instance.

        A list of lists rather than a flattened one: "sixty-five addresses in one call" and
        "sixty-five addresses in two calls" are different facts, and the flattened version
        cannot tell them apart.
        """
        return [
            posted_addresses(request)
            for request in self._instances[host].requests
            if request.method == "POST"
        ]

    def addresses_asked_of(self, host: str) -> list[str]:
        """Which addresses one instance was asked about, in order, single reads only."""
        return [
            requested_address(request)
            for request in self._instances[host].requests
            if request.url.path.endswith("/balance")
        ]

    def history_requests(self) -> list[httpx.Request]:
        """Every request that read a page of transactions, across both hosts, in order."""
        return [request for request in self.requests if is_history_request(request)]


# --------------------------------------------------------------------------------------
# Building the provider under test
# --------------------------------------------------------------------------------------


def kaspa_settings(
    *,
    network: str = "testnet",
    primary_url: str = PRIMARY_URL,
    fallback_url: str = FALLBACK_URL,
) -> Settings:
    """A real `Settings`, not a stub, so the `Literal` on the network is exercised too.

    Built directly rather than through the environment: `get_settings` is cached
    process-wide and a test that monkeypatched `PORTFOLIO_*` would be sharing state with
    every other test in the run. The provider takes the object, so nothing global moves.
    """
    return Settings(
        kaspa_api_url=primary_url,
        kaspa_api_fallback_url=fallback_url,
        # `network` is a plain `str` here and the field is a `Literal`. mypy does not
        # object because `BaseSettings.__init__` is typed `**data: Any`, which is worth
        # knowing: the Literal is enforced by pydantic at construction and by nothing
        # statically.
        kaspa_network=network,
    )


def kaspa_client(fake: KaspaFake, *, max_attempts: int = 3) -> httpx.AsyncClient:
    """The production client, over the fake, with every duration injected.

    `build_http_client` rather than an assembled transport, because that is what #10 will
    hand the provider and it is the wiring whose retry loop the failover behaviour composes
    with. A zero backoff and a zero interval, because this file's assertions are about
    *how many* requests were made and to whom -- how long they waited is
    `tests/providers/test_http.py`'s subject and is already covered there.
    """

    async def no_sleep(_milliseconds: int) -> None:
        return

    return build_http_client(
        transport=httpx.MockTransport(fake.handler),
        policy=RetryPolicy(max_attempts=max_attempts, base_backoff_ms=0, max_backoff_ms=0),
        limiter=HostRateLimiter(min_interval_ms=0, clock=lambda: 0, sleep=no_sleep),
        jitter=lambda bound: bound,
        sleep=no_sleep,
    )


def kaspa_provider(
    fake: KaspaFake,
    *,
    network: str = "testnet",
    primary_url: str = PRIMARY_URL,
    fallback_url: str = FALLBACK_URL,
    max_attempts: int = 3,
) -> tuple[KaspaProvider, httpx.AsyncClient]:
    """The provider and the client it holds, so a test can close the client afterwards."""
    client = kaspa_client(fake, max_attempts=max_attempts)
    settings = kaspa_settings(network=network, primary_url=primary_url, fallback_url=fallback_url)
    return KaspaProvider(client, settings=settings), client
