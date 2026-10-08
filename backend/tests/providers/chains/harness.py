"""Two scripted Esplora instances that record **who was asked, and how many times**.

The counting is the whole point of this module and it is not incidental bookkeeping.

A provider that hammered a throttled primary twenty times and then succeeded on the
fallback returns exactly the same balances as one that moved on after the first refusal.
Every assertion about the *result* passes for both. Only the per-host request log tells
them apart -- and the difference between them is the difference between a sync that is
slow and an application banned from a free public index, which is a failure that outlives
the sync that caused it. So `EsploraFake` records each request against the host that
received it, and the failover tests assert on `fake.counts` rather than only on balances.

**Nothing here sleeps and nothing here reads a clock.** The limiter, the jitter and the
sleep are injected through `tests/providers/harness.py`'s conventions, so no assertion in
this suite is a measurement of how fast the machine running it happened to be.

Every address in every body comes from `tests/address_vectors.py`: testnet, signet or
regtest, never mainnet. Rule 3, and
`tests/security/test_address_logging.py::test_fixtures_contain_no_mainnet_address` proves
it mechanically over this file too.

**Nothing here asserts conformance with `isinstance`, and nothing here should.**
`ChainProvider` is deliberately not `@runtime_checkable`: an `isinstance` check compares
four attribute names and says nothing about whether `fetch_balances` takes a sequence or
whether `health` is a coroutine function. The conformance assertion for `EsploraProvider`
is the module-level annotated assignment at the bottom of `test_bitcoin.py`, which
`mypy --strict` decides in the gate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final

import httpx

from portfolio.config import Settings
from portfolio.providers.chains.bitcoin import EsploraProvider
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
# *are* real vendors -- are pinned once, in `test_bitcoin.py`, by reading `Settings()`
# rather than by being copied here.

PRIMARY_HOST: Final = "primary.example"
FALLBACK_HOST: Final = "fallback.example"
PRIMARY_URL: Final = f"https://{PRIMARY_HOST}/api"
FALLBACK_URL: Final = f"https://{FALLBACK_HOST}/api"

#: A plausible block height. Any non-negative integer will do; a round number would make a
#: parser that returned a constant look correct.
TIP_HEIGHT: Final = 2_873_119


# --------------------------------------------------------------------------------------
# Bodies, built by hand rather than by a model
# --------------------------------------------------------------------------------------


def balance_body(
    address: str,
    *,
    funded: int = 0,
    spent: int = 0,
    mempool_funded: int | None = 0,
    mempool_spent: int | None = 0,
    tx_count: int = 2,
) -> str:
    """The Esplora address body, exactly the shape Blockstream's `API.md` documents.

    `mempool_funded=None` omits `mempool_stats` altogether, which is the case criterion 10
    turns on: an instance that does not report a mempool is one that *cannot answer*, not
    one answering zero.

    `tx_count` is `chain_stats.tx_count`. Two by default, as before spec 038; a history test
    sets it to the number of transactions it serves, which is what the history is checked
    against.
    """
    body: dict[str, object] = {
        "address": address,
        "chain_stats": {
            "funded_txo_count": 1,
            "funded_txo_sum": funded,
            "spent_txo_count": 1,
            "spent_txo_sum": spent,
            "tx_count": tx_count,
        },
    }
    if mempool_funded is not None:
        body["mempool_stats"] = {
            "funded_txo_count": 1,
            "funded_txo_sum": mempool_funded,
            "spent_txo_count": 1,
            "spent_txo_sum": mempool_spent if mempool_spent is not None else 0,
            "tx_count": 1,
        }
    return json.dumps(body)


# --------------------------------------------------------------------------------------
# Confirmed transactions, for `address_history` (spec 038)
# --------------------------------------------------------------------------------------

#: A plausible block time, in Unix seconds: 2026-10-08T00:00:00Z. Not a round number in any
#: unit a parser might confuse it with, and inside a UTC day so offsets from it stay there.
BLOCK_TIME: Final = 1_791_417_600

#: The txid a coinbase input names: 64 zeros. Never a cursor, never a history entry.
COINBASE_TXID: Final = "0" * 64


def txid_of(seed: int) -> str:
    """A txid, built at run time from a number so no hex literal sits in the source.

    Lower-case, 64 characters, as Esplora renders one: the SHA-256 of the seed, so it has
    letters as well as digits -- an upper-cased copy is a different string, which the txid
    check has to notice. Built rather than written because a 64-character hex literal is the
    shape a secret scanner's entropy rule looks at twice.
    """
    return hashlib.sha256(f"esplora-fixture-{seed}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class EsploraTx:
    """One confirmed transaction, described by what it does rather than by its bytes.

    `outputs` are `(address or None, value)`: `None` is an output with no address -- an
    `OP_RETURN` -- which can never pay the address being read. `inputs` are
    `(address, value)` spent from, or `None` for a non-coinbase input whose `prevout` is
    missing, which is the unresolved case. `coinbase` adds the coinbase input, which has no
    `prevout` at all.
    """

    seed: int
    block_time: int = BLOCK_TIME
    outputs: tuple[tuple[str | None, int], ...] = ()
    inputs: tuple[tuple[str, int] | None, ...] = ()
    coinbase: bool = False

    @property
    def txid(self) -> str:
        return txid_of(self.seed)

    def funded(self, address: str) -> int:
        """What this transaction paid `address`: its part of `funded_txo_sum`."""
        return sum(value for owner, value in self.outputs if owner == address)

    def spent(self, address: str) -> int:
        """What this transaction spent from `address`: its part of `spent_txo_sum`."""
        return sum(spend[1] for spend in self.inputs if spend is not None and spend[0] == address)

    def document(self) -> dict[str, Any]:
        """The transaction as `/txs/chain` renders it, per API.md and the measured example.

        `Any` because a test mangles one field of it to script a refusal, and that is the
        whole point of returning a plain mutable document.
        """
        vin: list[dict[str, Any]] = []
        if self.coinbase:
            vin.append(
                {
                    "txid": COINBASE_TXID,
                    "vout": 4_294_967_295,
                    "scriptsig": "03a1b2c3",
                    "scriptsig_asm": "OP_PUSHBYTES_3 a1b2c3",
                    "is_coinbase": True,
                    "sequence": 4_294_967_295,
                }
            )
        for index, spend in enumerate(self.inputs):
            entry: dict[str, Any] = {
                "txid": txid_of(self.seed * 1_000 + index + 1),
                "vout": index,
                "scriptsig": "",
                "scriptsig_asm": "",
                "witness": ["30440220"],
                "is_coinbase": False,
                "sequence": 4_294_967_293,
            }
            if spend is not None:
                entry["prevout"] = _output_document(*spend)
            vin.append(entry)
        return {
            "txid": self.txid,
            "version": 2,
            "locktime": 0,
            "vin": vin,
            "vout": [_output_document(owner, value) for owner, value in self.outputs],
            "size": 223,
            "weight": 562,
            "fee": 564,
            "status": {
                "confirmed": True,
                "block_height": 2_873_000 + self.seed,
                "block_hash": txid_of(self.seed + 7_777_777),
                "block_time": self.block_time,
            },
        }


def _output_document(owner: str | None, value: int) -> dict[str, Any]:
    """One `vout` entry, or a `prevout`, which API.md says has the same format."""
    output: dict[str, Any] = {
        "scriptpubkey": "0014abcdef",
        "scriptpubkey_asm": "OP_0 OP_PUSHBYTES_20 abcdef",
        "scriptpubkey_type": "v0_p2wpkh" if owner is not None else "op_return",
        "value": value,
    }
    if owner is not None:
        output["scriptpubkey_address"] = owner
    return output


def history_reply(
    address: str,
    transactions: Sequence[EsploraTx],
    **overrides: Any,
) -> Reply:
    """An instance that serves `transactions` (newest first) and stats that agree with them.

    `chain_stats` is computed from the transactions -- funded, spent, and the count of
    distinct txids -- so a history test that changes nothing gets a history that proves
    itself, and every incomplete arm is one override away: `tx_count=`, `funded=`, or a
    different reply for the read after the paging.
    """
    distinct = {transaction.txid: transaction for transaction in transactions}
    reply = Reply(
        funded=sum(transaction.funded(address) for transaction in distinct.values()),
        spent=sum(transaction.spent(address) for transaction in distinct.values()),
        tx_count=len(distinct),
        history=tuple(transaction.document() for transaction in transactions),
    )
    return replace(reply, **overrides)


def history_address(request: httpx.Request) -> str:
    """The address a `/address/{a}/txs/chain[/{txid}]` request is about."""
    segments = request.url.path.split("/")
    return segments[segments.index("address") + 1]


def history_cursor(request: httpx.Request) -> str | None:
    """The `last_seen_txid` a history request carried in its path, or `None` on page one."""
    segments = request.url.path.split("/")
    after_chain = segments[segments.index("chain") + 1 :]
    return after_chain[0] if after_chain else None


def is_history_request(request: httpx.Request) -> bool:
    """Whether a request read a page of confirmed history rather than stats or the tip."""
    return "/txs/chain" in request.url.path


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
    funded: int = 0
    spent: int = 0
    mempool_funded: int | None = 0
    mempool_spent: int | None = 0
    body: str | None = None
    """A raw body, replacing the generated one. This is how a malformed answer is scripted."""
    headers: Mapping[str, str] = field(default_factory=dict)
    error: BaseException | None = None
    """Raised instead of answering, for the transport-failure arms."""
    tip: int = TIP_HEIGHT
    tx_count: int = 2
    """`chain_stats.tx_count` in the address body."""
    history: Sequence[Mapping[str, Any]] = ()
    """The confirmed transactions `/txs/chain` serves, newest first, as documents."""
    page_size: int = 25
    """How many transactions one page holds: 25, as both vendors document."""
    ignore_cursor: bool = False
    """Serve page one whatever the cursor says: a vendor that never ends the history."""

    def render(self, request: httpx.Request) -> str:
        """The body this reply sends for `request`.

        The address is read back out of the request path rather than carried on the reply,
        so a scripted instance answers about whatever it was actually asked. That is what
        makes `test_an_answer_echoing_a_different_address_is_refused` a real test: the
        echo has to be overridden deliberately, with `body=`, rather than being the
        accident that a fixed fixture would make it.
        """
        if self.body is not None:
            return self.body
        if request.url.path.endswith("/blocks/tip/height"):
            return str(self.tip)
        if is_history_request(request):
            return json.dumps(list(self.history_page(request)))
        return balance_body(
            requested_address(request),
            funded=self.funded,
            spent=self.spent,
            mempool_funded=self.mempool_funded,
            mempool_spent=self.mempool_spent,
            tx_count=self.tx_count,
        )

    def history_page(self, request: httpx.Request) -> Sequence[Mapping[str, Any]]:
        """The page after the cursor, as Esplora pages: newest first, `page_size` long.

        **An unknown cursor answers an empty page**, exactly as both hosts were measured to
        on 2026-10-08 -- which is what makes an empty page useless as proof of the end. A
        cursor that appears more than once answers from after its first appearance, so a
        history scripted with a repeated transaction is served it twice.
        """
        cursor = None if self.ignore_cursor else history_cursor(request)
        txids = [str(document.get("txid")) for document in self.history]
        if cursor is None:
            begin = 0
        elif cursor in txids:
            begin = txids.index(cursor) + 1
        else:
            begin = len(txids)
        return self.history[begin : begin + self.page_size]


def requested_address(request: httpx.Request) -> str:
    """The address an Esplora request is about, from the last path segment."""
    return request.url.path.rsplit("/", 1)[-1]


class ScriptedInstance:
    """One Esplora instance: a queue of replies whose last entry repeats.

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
            headers=dict(reply.headers),
            content=reply.render(request),
        )


class EsploraFake:
    """Both instances behind one `httpx.MockTransport`, routed by host.

    Routing on the host rather than on a URL prefix is deliberate: the provider builds its
    own URLs out of a base URL and a path, and a fake that matched the whole URL would
    pass only for the exact string the test already believed the provider would produce.
    Matching the host means the test finds out what path the provider actually asked for,
    which is how `test_health_reports_healthy_when_an_instance_answers` can assert the
    documented endpoint rather than assume it.
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
        instance = self._instances.get(request.url.host)
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
        return [request.url.host for request in self.requests]

    def addresses_asked_of(self, host: str) -> list[str]:
        """Which addresses one instance was asked about, in order."""
        return [
            requested_address(request)
            for request in self._instances[host].requests
            if not request.url.path.endswith("/blocks/tip/height")
        ]

    def history_requests(self) -> list[httpx.Request]:
        """Every request that read a page of history, across both hosts, in order."""
        return [request for request in self.requests if is_history_request(request)]


# --------------------------------------------------------------------------------------
# Building the provider under test
# --------------------------------------------------------------------------------------


def esplora_settings(
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
        bitcoin_esplora_url=primary_url,
        bitcoin_esplora_fallback_url=fallback_url,
        # `network` is a plain `str` here and the field is a `Literal`. mypy does not
        # object because `BaseSettings.__init__` is typed `**data: Any`, which is worth
        # knowing: the Literal is enforced by pydantic at construction and by nothing
        # statically, so `test_a_network_that_does_not_exist_is_refused_at_construction`
        # is the only thing that checks it.
        bitcoin_network=network,
    )


def esplora_client(
    fake: EsploraFake,
    *,
    max_attempts: int = 3,
) -> httpx.AsyncClient:
    """The production client, over the fake, with every duration injected.

    `build_http_client` rather than an assembled transport, because that is what #10 will
    hand the provider and it is the wiring whose timeouts and retry loop the failover
    behaviour composes with. A zero backoff and a zero interval, because this file's
    assertions are about *how many* requests were made and to whom -- how long they waited
    is `tests/providers/test_http.py`'s subject and is already covered there.
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


def esplora_provider(
    fake: EsploraFake,
    *,
    network: str = "testnet",
    primary_url: str = PRIMARY_URL,
    fallback_url: str = FALLBACK_URL,
    max_attempts: int = 3,
) -> tuple[EsploraProvider, httpx.AsyncClient]:
    """The provider and the client it holds, so a test can close the client afterwards."""
    client = esplora_client(fake, max_attempts=max_attempts)
    settings = esplora_settings(network=network, primary_url=primary_url, fallback_url=fallback_url)
    return EsploraProvider(client, settings=settings), client
