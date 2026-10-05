"""A fake Esplora that answers **by address**, and a spy on derivation (spec 031).

Shared by the provider's scan tests (`tests/providers/test_bitcoin_extended_key_scan.py`)
and the balance sync's (`tests/services/test_balance_sync_extended_keys.py`), so the two
suites drive the same vendor through the same transport.

`AddressBook` answers each address with the holding scripted for it, and every other address
as unused and empty. That is what lets a gap scan be tested as a gap scan: the used addresses
are named by their position in BIP-84's account key, as `vpub` (R11), and everything the scan
derives beyond them reads as the empty addresses they are.

The provider is the real `EsploraProvider` over the real `build_http_client` -- retry
transport, host limiter and all -- with a `MockTransport` underneath and every duration
injected. Only the vendor is fake.

Every key and address here is a test-network form, from `tests/extended_key_vectors.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import httpx
from sqlalchemy import text

from portfolio.domain import extended_keys
from portfolio.domain.extended_keys import (
    CHANGE_BRANCH,
    RECEIVE_BRANCH,
    DerivedKey,
    ExtendedPublicKey,
    parse_extended_public_key,
)
from portfolio.providers.chains import bitcoin
from portfolio.providers.chains.bitcoin import EsploraProvider
from portfolio.providers.http import HostRateLimiter, RetryPolicy, build_http_client
from tests.balance_harness import sqlite_timestamp
from tests.extended_key_vectors import (
    SCAN_CHANGE,
    SCAN_KEY,
    SCAN_RECEIVE,
    SCAN_USED_CHANGE,
    SCAN_USED_RECEIVE,
)
from tests.providers.chains.harness import esplora_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from datetime import datetime

    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class Holding:
    """What the fake reports for one address. The default is unused and empty."""

    funded: int = 0
    spent: int = 0
    chain_tx: int = 0
    mempool_funded: int | None = 0
    """`None` leaves `mempool_stats` out of the body, as an instance with no mempool does."""
    mempool_spent: int = 0
    mempool_tx: int = 0


#: A used address with nothing left on it: the case a balance alone cannot tell from an
#: address nobody ever paid, and the reason `used` comes from `tx_count`.
EMPTIED: Final = Holding(funded=50_000, spent=50_000, chain_tx=2)


def esplora_body(address: str, holding: Holding) -> str:
    """The documented Esplora address body, with `tx_count` in each stats object."""
    body: dict[str, object] = {
        "address": address,
        "chain_stats": {
            "funded_txo_count": 1,
            "funded_txo_sum": holding.funded,
            "spent_txo_count": 0,
            "spent_txo_sum": holding.spent,
            "tx_count": holding.chain_tx,
        },
    }
    if holding.mempool_funded is not None:
        body["mempool_stats"] = {
            "funded_txo_count": 0,
            "funded_txo_sum": holding.mempool_funded,
            "spent_txo_count": 0,
            "spent_txo_sum": holding.mempool_spent,
            "tx_count": holding.mempool_tx,
        }
    return json.dumps(body)


@dataclass
class AddressBook:
    """Both configured instances behind one `MockTransport`, answering by address.

    Records every request, retries included, in order: the address, the host, and -- when a
    clock is given -- the clock's reading at arrival.
    """

    holdings: Mapping[str, Holding] = field(default_factory=dict)
    everything_used: bool = False
    clock: Callable[[], int] | None = None
    down_hosts: frozenset[str] = frozenset()
    failures: dict[str, list[httpx.Response]] = field(default_factory=dict)
    """Responses an address gets before its holding, one per request, consumed in order."""
    asked: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    arrivals_ms: list[int] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        address = request.url.path.rsplit("/", 1)[-1]
        self.asked.append(address)
        self.hosts.append(str(request.url.host))
        if self.clock is not None:
            self.arrivals_ms.append(self.clock())
        if request.url.host in self.down_hosts:
            return httpx.Response(503)
        queued = self.failures.get(address)
        if queued:
            return queued.pop(0)
        default = Holding(chain_tx=1) if self.everything_used else Holding()
        return httpx.Response(
            200, content=esplora_body(address, self.holdings.get(address, default))
        )


async def no_sleep(_milliseconds: int) -> None:
    return


def provider_over(
    book: AddressBook,
    *,
    network: str = "testnet",
    max_attempts: int = 3,
    limiter: HostRateLimiter | None = None,
    transport_sleep: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[EsploraProvider, httpx.AsyncClient]:
    """The production provider and client over the fake, every duration injected."""
    client = build_http_client(
        transport=httpx.MockTransport(book.handler),
        policy=RetryPolicy(max_attempts=max_attempts, base_backoff_ms=0, max_backoff_ms=0),
        limiter=limiter
        if limiter is not None
        else HostRateLimiter(min_interval_ms=0, clock=lambda: 0, sleep=no_sleep),
        jitter=lambda bound: bound,
        sleep=transport_sleep if transport_sleep is not None else no_sleep,
    )
    return EsploraProvider(client, settings=esplora_settings(network=network)), client


#: What criterion 3's used addresses hold, by position. Each a different amount, so a sum
#: that dropped or doubled one is visible. Receive 0 is emptied; receive 24 has a pending
#: deposit; receive 46 holds the most and is never reached.
CRITERION_THREE_HOLDINGS: Final[dict[tuple[int, int], Holding]] = {
    (RECEIVE_BRANCH, 0): EMPTIED,
    (RECEIVE_BRANCH, 5): Holding(funded=120_000, chain_tx=1),
    (RECEIVE_BRANCH, 24): Holding(funded=7_000, chain_tx=1, mempool_funded=3_000, mempool_tx=1),
    (RECEIVE_BRANCH, 46): Holding(funded=999_999, chain_tx=1),
    (CHANGE_BRANCH, 0): Holding(funded=10_000, spent=4_000, chain_tx=2),
    (CHANGE_BRANCH, 3): Holding(funded=25_000, chain_tx=1),
}

#: The sum over what the scan reaches: 0 + 120,000 + 7,000 + 6,000 + 25,000. Receive 46's
#: 999,999 is not in it, because the gap limit never reaches it.
CRITERION_THREE_CONFIRMED: Final = 158_000

#: Every reached address reports a mempool, and only receive 24 has anything in it.
CRITERION_THREE_PENDING: Final = 3_000


def address_at(branch: int, index: int) -> str:
    return (SCAN_RECEIVE if branch == RECEIVE_BRANCH else SCAN_CHANGE)[index]


def criterion_three_book(**options: object) -> AddressBook:
    """Criterion 3's fixture: used receive 0, 5, 24 and 46; used change 0 and 3."""
    assert {index for branch, index in CRITERION_THREE_HOLDINGS if branch == 0} == set(
        SCAN_USED_RECEIVE
    )
    assert {index for branch, index in CRITERION_THREE_HOLDINGS if branch == 1} == set(
        SCAN_USED_CHANGE
    )
    holdings = {
        address_at(branch, index): holding
        for (branch, index), holding in CRITERION_THREE_HOLDINGS.items()
    }
    return AddressBook(holdings=holdings, **options)  # type: ignore[arg-type]


@dataclass
class DerivationSpy:
    """Records every `derive_child` the Esplora provider makes, as `(parent, index)`.

    `parent` is `"account"` for the key itself, or the branch the parent key is. The real
    function still runs: the spy observes, it does not replace.
    """

    branch_of: dict[bytes, int]
    calls: list[tuple[str | int, int]] = field(default_factory=list)

    def children(self, branch: int) -> list[int]:
        return [index for parent, index in self.calls if parent == branch]

    def accounts(self) -> list[int]:
        return [index for parent, index in self.calls if parent == "account"]


def spy_on_derivation(monkeypatch: pytest.MonkeyPatch, key: str = SCAN_KEY) -> DerivationSpy:
    """Wrap the provider's `derive_child` for one test. Children of `key` only."""
    real = extended_keys.derive_child
    parsed = parse_extended_public_key(key)
    branch_keys = {branch: real(parsed, branch) for branch in (RECEIVE_BRANCH, CHANGE_BRANCH)}
    spy = DerivationSpy(
        branch_of={key.public_key: branch for branch, key in branch_keys.items() if key is not None}
    )

    def spying(parent: ExtendedPublicKey | DerivedKey, index: int) -> DerivedKey | None:
        label: str | int = (
            "account" if isinstance(parent, ExtendedPublicKey) else spy.branch_of[parent.public_key]
        )
        spy.calls.append((label, index))
        return real(parent, index)

    monkeypatch.setattr(bitcoin, "derive_child", spying)
    return spy


async def insert_key_wallet(
    session: AsyncSession,
    *,
    user_id: int,
    created_at: datetime,
    key: str = SCAN_KEY,
    archived: bool = False,
) -> int:
    """An extended-key wallet row, written directly, as `create_wallet` stores one.

    `tests/balance_harness.py`'s `insert_wallet` leaves `kind` to the column's default,
    which is `address`; this is the one place a test writes the other kind.
    """
    result = await session.execute(
        text(
            "INSERT INTO wallets (user_id, chain_key, address_canonical, address_display, "
            "label, archived_at, created_at, updated_at, kind) "
            "VALUES (:user_id, 'bitcoin', :key, :key, NULL, :archived_at, :now, :now, "
            "'extended_key') RETURNING id"
        ),
        {
            "user_id": user_id,
            "key": key,
            "archived_at": sqlite_timestamp(created_at) if archived else None,
            "now": sqlite_timestamp(created_at),
        },
    )
    wallet_id: int = result.scalar_one()
    await session.commit()
    return wallet_id
