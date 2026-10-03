"""Spec 030 (#23), criterion 8: the five new sections of `GET /api/health/detail`, over HTTP.

The whole stack runs -- the session middleware, the router, the dependency that reads the
timers off `app.state`, `HealthService`, the repositories and SQLite -- through the real
lifespan, signed in as the owner. What is pinned:

* **every state of every section is reachable through the endpoint**: each timer state from
  real `IntervalScheduler`s the test starts and stops; each chain, account and price state
  from rows planted the way the syncs write them; each holdings-check state from the real
  reconciliation, whose counts are cross-checked against `GET /api/accounting/reconciliation`;
  and `unavailable` for every section that can fail, while the others answer;
* **the exact shape**, key for key, so nothing beyond the spec -- no interval, path, URL,
  key, tolerance or age limit -- is served; and the chain's provider text is not;
* **no provider is called**: a transport that records every request, a chain registry that
  records every provider it is asked for, and a simulated venue that counts its calls all
  stay at zero across repeated reads;
* **`401` without a session**, and nothing added to the public allowlist;
* **the OpenAPI document** declares every state as an enum of exactly its wire forms.

Instants are taken from the real clock, because the service judges ages against it: a
price planted "five minutes old" is five minutes older than the test's run.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import httpx
from sqlalchemy import text

from portfolio.api.middleware import PUBLIC_API_PATHS
from portfolio.domain.chains import ChainKey
from portfolio.domain.exchanges import ExchangeKey
from portfolio.providers.http import build_http_client
from portfolio.providers.registry import CHAIN_PROVIDERS
from portfolio.repositories.exchange_sync_runs import ExchangeSyncErrorKind
from portfolio.repositories.exchanges import ExchangeAccountRepository
from portfolio.repositories.prices import PriceRepository
from portfolio.repositories.sync_runs import (
    ChainOutcome,
    SyncErrorKind,
    SyncRunRepository,
    SyncRunStatus,
    SyncTrigger,
)
from portfolio.services.prices import STALE_AFTER
from portfolio.services.reconciliation import ReconciliationService
from portfolio.services.scheduler import IntervalScheduler, utc_now
from tests.accounting_harness import plant_price
from tests.address_vectors import BIP173_TESTNET_P2WPKH, BIP173_TESTNET_P2WSH, KASPA_TESTNET_V0
from tests.api.test_accounting import application, plant, recompute
from tests.api.test_reconciliation import (
    RECONCILIATION,
    Readings,
    account_id,
    add_wallet,
    plant_synced,
    plant_the_specs_example,
)
from tests.exchange_sync_harness import SimulatedVenue
from tests.services.test_reconciliation_service import (
    buy,
    fail_balances,
    plant_reading,
    set_sync_status,
    store_balances,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest
    from fastapi import FastAPI
    from httpx import AsyncClient

DETAIL: Final = "/api/health/detail"
SECTIONS: Final = {"backup", "schedulers", "chains", "exchanges", "prices", "reconciliation"}
FAILED_DETAIL: Final = "the provider said something only the run log should keep"
DEADLOCK_TIMEOUT: Final = 5

#: Every key of every section, as the spec writes the shape. Exactly these, and no other.
SCHEDULER_KEYS: Final = {"name", "state", "last_tick_at", "last_tick_succeeded"}
CHAIN_KEYS: Final = {"chain_key", "state", "last_success_at", "last_error_kind"}
EXCHANGE_KEYS: Final = {
    "exchange_key",
    "sync_state",
    "last_synced_at",
    "balances_state",
    "balances_read_at",
}
PRICES_KEYS: Final = {"state", "latest_fetched_at"}
RECONCILIATION_KEYS: Final = {
    "state",
    "computed_at",
    "assets_compared",
    "assets_mismatched",
    "sources_not_compared",
}


async def detail(client: AsyncClient) -> dict[str, Any]:
    response = await client.get(DETAIL)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert set(body) == SECTIONS
    return body


def wire(moment: datetime) -> str:
    """An instant as the API writes it: ISO 8601 in UTC, ending in `Z`."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parsed(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def explode(*_arguments: object, **_keywords: object) -> Any:
    message = "a section's read failed"
    raise RuntimeError(message)


# --------------------------------------------------------------------------------------
# The timers
# --------------------------------------------------------------------------------------


class GatedSleep:
    """Parks a real scheduler's loop after each tick until the test lets it go."""

    def __init__(self) -> None:
        self._arrived = asyncio.Event()
        self._go = asyncio.Event()

    async def __call__(self, delay: int) -> None:
        del delay
        self._arrived.set()
        await self._go.wait()
        self._go.clear()

    async def parked(self) -> None:
        await asyncio.wait_for(self._arrived.wait(), timeout=DEADLOCK_TIMEOUT)
        self._arrived.clear()


async def never_ran() -> datetime | None:
    return None


def real_timer(
    name: str,
    *,
    clock: Callable[[], datetime] = utc_now,
    fails: bool = False,
) -> tuple[IntervalScheduler, GatedSleep]:
    async def work(at_startup: bool) -> None:
        del at_startup
        if fails:
            message = "a tick that failed"
            raise RuntimeError(message)

    sleep = GatedSleep()
    scheduler = IntervalScheduler(
        name=name,
        interval_minutes=15,
        last_run_at=never_ran,
        run=work,
        clock=clock,
        sleep=sleep,
    )
    return scheduler, sleep


async def test_a_lifespan_with_every_timer_switched_off_serves_four_disabled_timers(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (_app, client):
        body = await detail(client)

    assert body["schedulers"] == [
        {"name": name, "state": "disabled", "last_tick_at": None, "last_tick_succeeded": None}
        for name in ("balance-sync", "price-refresh", "exchange-sync", "backup")
    ]


async def test_every_timer_state_is_served_from_real_timers(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ok`, `late`, `stopped` and `disabled`, each from an `IntervalScheduler` on `app.state`.

    The late one's clock is a day behind, so its last tick is a day older than the instant the
    service judges it at. The stopped one ticked and failed before it was stopped, so its
    `last_tick_succeeded` is `false` -- a state the page words on its own.
    """
    del api_environment
    day_ago = datetime.now(UTC) - timedelta(days=1)
    ok, ok_sleep = real_timer("balance-sync")
    late, late_sleep = real_timer("price-refresh", clock=lambda: day_ago)
    stopped, stopped_sleep = real_timer("backup", fails=True)
    async with application(monkeypatch) as (app, client):
        try:
            for scheduler, sleep in ((ok, ok_sleep), (late, late_sleep), (stopped, stopped_sleep)):
                await scheduler.start()
                await sleep.parked()
            await stopped.stop()
            app.state.balance_scheduler = ok
            app.state.price_scheduler = late
            app.state.exchange_scheduler = None
            app.state.backup_scheduler = stopped

            body = await detail(client)
        finally:
            await ok.stop()
            await late.stop()

    timers = {timer["name"]: timer for timer in body["schedulers"]}
    assert [timer["name"] for timer in body["schedulers"]] == [
        "balance-sync",
        "price-refresh",
        "exchange-sync",
        "backup",
    ]
    assert all(set(timer) == SCHEDULER_KEYS for timer in body["schedulers"])
    assert timers["balance-sync"]["state"] == "ok"
    assert timers["balance-sync"]["last_tick_succeeded"] is True
    assert parsed(timers["balance-sync"]["last_tick_at"]) == ok.last_tick_finished_at
    assert timers["price-refresh"]["state"] == "late"
    assert timers["price-refresh"]["last_tick_at"] == wire(day_ago)
    assert timers["exchange-sync"] == {
        "name": "exchange-sync",
        "state": "disabled",
        "last_tick_at": None,
        "last_tick_succeeded": None,
    }
    assert timers["backup"]["state"] == "stopped"
    assert timers["backup"]["last_tick_succeeded"] is False


# --------------------------------------------------------------------------------------
# The chains
# --------------------------------------------------------------------------------------


async def finished_run(app: FastAPI, finished_at: datetime, *chains: ChainOutcome) -> None:
    async with app.state.db_sessionmaker() as session:
        repository = SyncRunRepository(session)
        opened = await repository.open_run(
            trigger=SyncTrigger.MANUAL, started_at=finished_at, wallets_total=len(chains)
        )
        run_id = opened.id
        await session.commit()
        await repository.finish_run(
            run_id,
            status=SyncRunStatus.PARTIAL,
            finished_at=finished_at,
            duration_ms=1,
            wallets_succeeded=1,
            wallets_failed=1,
            chains=chains,
        )
        await session.commit()


async def test_every_chain_state_is_served_and_the_providers_text_is_not(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ok` and `failing` from a finished run, `never` for a chain only a wallet names."""
    del api_environment
    earlier = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    latest = earlier + timedelta(hours=1)
    async with application(monkeypatch) as (app, client):
        await add_wallet(app, ChainKey.KASPA, KASPA_TESTNET_V0)
        await finished_run(
            app,
            earlier,
            ChainOutcome(chain_key="bitcoin", status=SyncRunStatus.SUCCESS, wallets_read=1),
        )
        await finished_run(
            app,
            latest,
            ChainOutcome(
                chain_key="bitcoin",
                status=SyncRunStatus.FAILED,
                wallets_read=0,
                error_kind=SyncErrorKind.UNAVAILABLE,
                detail=FAILED_DETAIL,
            ),
        )
        response = await client.get(DETAIL)

    assert response.status_code == 200
    assert FAILED_DETAIL not in response.text
    assert response.json()["chains"] == {
        "state": "ok",
        "items": [
            {
                "chain_key": "bitcoin",
                "state": "failing",
                "last_success_at": wire(earlier),
                "last_error_kind": "unavailable",
            },
            {
                "chain_key": "kaspa",
                "state": "never",
                "last_success_at": None,
                "last_error_kind": None,
            },
        ],
    }


async def test_a_chain_that_succeeded_last_is_ok_with_no_kind(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    finished = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    async with application(monkeypatch) as (app, client):
        await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
        await finished_run(
            app,
            finished,
            ChainOutcome(chain_key="bitcoin", status=SyncRunStatus.SUCCESS, wallets_read=1),
        )
        body = await detail(client)

    assert body["chains"]["items"] == [
        {
            "chain_key": "bitcoin",
            "state": "ok",
            "last_success_at": wire(finished),
            "last_error_kind": None,
        }
    ]


# --------------------------------------------------------------------------------------
# The exchanges
# --------------------------------------------------------------------------------------


async def test_every_exchange_state_is_served(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bitget synced and read; BingX refused its key and its last balance read failed.

    R4: `last_synced_at` is the last *successful* sync, set by `mark_synced` together with
    `ok`, and left as it was by a later failure.
    """
    del api_environment
    now = datetime.now(UTC).replace(microsecond=0)
    synced_at = now - timedelta(minutes=30)
    read_at = now - timedelta(minutes=29)
    async with application(monkeypatch) as (app, client):
        await plant(app, {ExchangeKey.BITGET: [], ExchangeKey.BINGX: []})
        factory = app.state.db_sessionmaker
        bitget = await account_id(app, ExchangeKey.BITGET)
        bingx = await account_id(app, ExchangeKey.BINGX)
        async with factory() as session:
            accounts = ExchangeAccountRepository(session)
            await accounts.mark_synced(bitget, synced_at=synced_at)
            await accounts.mark_synced(bingx, synced_at=synced_at)
            await session.commit()
        await set_sync_status(factory, bingx, "auth_failed")
        await store_balances(factory, bitget, (), read_at)
        await store_balances(factory, bingx, (), read_at)
        await fail_balances(factory, bingx, ExchangeSyncErrorKind.AUTH)
        body = await detail(client)

    assert body["exchanges"] == {
        "state": "ok",
        "items": [
            {
                "exchange_key": "bingx",
                "sync_state": "auth_failed",
                "last_synced_at": wire(synced_at),
                "balances_state": "failing",
                "balances_read_at": wire(read_at),
            },
            {
                "exchange_key": "bitget",
                "sync_state": "ok",
                "last_synced_at": wire(synced_at),
                "balances_state": "ok",
                "balances_read_at": wire(read_at),
            },
        ],
    }


async def test_an_account_never_synced_and_never_read_is_served_as_such(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant(app, {ExchangeKey.BITGET: []})
        await set_sync_status(
            app.state.db_sessionmaker, await account_id(app, ExchangeKey.BITGET), "error"
        )
        first = await detail(client)
        await set_sync_status(
            app.state.db_sessionmaker, await account_id(app, ExchangeKey.BITGET), "never_synced"
        )
        second = await detail(client)

    assert first["exchanges"]["items"] == [
        {
            "exchange_key": "bitget",
            "sync_state": "error",
            "last_synced_at": None,
            "balances_state": "never",
            "balances_read_at": None,
        }
    ]
    assert second["exchanges"]["items"][0]["sync_state"] == "never_synced"


# --------------------------------------------------------------------------------------
# The prices
# --------------------------------------------------------------------------------------


async def test_every_price_state_is_served(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`never`, then `stale` past the age limit, then `fresh`; `unavailable` is below."""
    del api_environment
    now = datetime.now(UTC).replace(microsecond=0)
    old = now - STALE_AFTER - timedelta(minutes=5)
    recent = now - timedelta(minutes=5)
    async with application(monkeypatch) as (app, client):
        never = (await detail(client))["prices"]
        async with app.state.db_sessionmaker() as session:
            await plant_price(session, symbol="BTC", amount=Decimal(1), as_of=old)
        stale = (await detail(client))["prices"]
        async with app.state.db_sessionmaker() as session:
            await plant_price(session, symbol="KAS", amount=Decimal(1), as_of=recent)
        fresh = (await detail(client))["prices"]

    assert never == {"state": "never", "latest_fetched_at": None}
    assert stale == {"state": "stale", "latest_fetched_at": wire(old)}
    assert fresh == {"state": "fresh", "latest_fetched_at": wire(recent)}


# --------------------------------------------------------------------------------------
# The reconciliation, cross-checked against its own endpoint
# --------------------------------------------------------------------------------------


async def reconciliation_counts(client: AsyncClient) -> dict[str, Any]:
    """What the health detail must say, worked out from the reconciliation's own document."""
    response = await client.get(RECONCILIATION)
    assert response.status_code == 200, response.text
    body = response.json()
    wallets = body["wallets"]
    return {
        "computed_at": body["computed_at"],
        "assets_compared": len(body["assets"]),
        "assets_mismatched": sum(1 for asset in body["assets"] if asset["status"] != "match"),
        "sources_not_compared": (
            sum(1 for venue in body["exchanges"] if venue["not_compared_reason"] is not None)
            + wallets["stale"]
            + wallets["unread"]
            + wallets["chain_failed"]
        ),
    }


def same_instant(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left is right
    return parsed(left) == parsed(right)


async def test_an_owner_with_nothing_to_compare_is_match(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R8: the startup recompute's empty snapshot, nothing held, nothing left out."""
    del api_environment
    async with application(monkeypatch) as (_app, client):
        served = (await detail(client))["reconciliation"]
        expected = await reconciliation_counts(client)

    assert served["state"] == "match"
    assert served["computed_at"] is not None
    assert same_instant(served["computed_at"], expected["computed_at"])
    assert (
        served["assets_compared"],
        served["assets_mismatched"],
        served["sources_not_compared"],
    ) == (0, 0, 0)


async def test_the_specs_example_is_a_mismatch_with_the_endpoints_counts(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ETH is `history_over` and SOL `history_short`: a mismatch, whatever else is true."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_the_specs_example(app, Readings())
        served = (await detail(client))["reconciliation"]
        expected = await reconciliation_counts(client)

    assert served["state"] == "mismatch"
    assert set(served) == RECONCILIATION_KEYS
    assert served["assets_mismatched"] >= 2
    assert {key: served[key] for key in expected if key != "computed_at"} == {
        key: value for key, value in expected.items() if key != "computed_at"
    }
    assert same_instant(served["computed_at"], expected["computed_at"])


async def test_a_wallet_left_unread_makes_a_matching_check_incomplete(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BTC matches; one wallet was never read and the venue's balances never were either."""
    del api_environment
    readings = Readings()
    async with application(monkeypatch) as (app, client):
        await plant_synced(app, {ExchangeKey.BITGET: [buy(1001, 0, "BTC", "0.5", "30000")]})
        read = await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
        await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WSH)
        await plant_reading(
            app.state.db_sessionmaker,
            wallet_id=read,
            confirmed=50_000_000,
            observed_at=readings.newer,
        )
        await recompute(app)
        served = (await detail(client))["reconciliation"]
        expected = await reconciliation_counts(client)

    assert served["state"] == "incomplete"
    assert served["assets_mismatched"] == 0
    assert served["sources_not_compared"] >= 1
    assert {key: served[key] for key in expected if key != "computed_at"} == {
        key: value for key, value in expected.items() if key != "computed_at"
    }


async def test_no_snapshot_is_not_computed(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del api_environment
    async with application(monkeypatch) as (app, client):
        async with app.state.db_sessionmaker() as session:
            await session.execute(text("DELETE FROM accounting_snapshots"))
            await session.commit()
        served = (await detail(client))["reconciliation"]

    assert served == {
        "state": "not_computed",
        "computed_at": None,
        "assets_compared": 0,
        "assets_mismatched": 0,
        "sources_not_compared": 0,
    }


# --------------------------------------------------------------------------------------
# A section that fails is `unavailable`, and the others answer
# --------------------------------------------------------------------------------------

UNAVAILABLE: Final = {
    "chains": {"state": "unavailable", "items": []},
    "exchanges": {"state": "unavailable", "items": []},
    "prices": {"state": "unavailable", "latest_fetched_at": None},
    "reconciliation": {
        "state": "unavailable",
        "computed_at": None,
        "assets_compared": None,
        "assets_mismatched": None,
        "sources_not_compared": None,
    },
}

READS: Final = {
    "chains": (SyncRunRepository, "chain_histories"),
    "exchanges": (ExchangeAccountRepository, "list_for_user"),
    "prices": (PriceRepository, "latest_fetched_at"),
    "reconciliation": (ReconciliationService, "reconciliation"),
}


async def test_each_failing_section_is_unavailable_while_the_rest_answer(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One section at a time, through the endpoint: a `200`, never a `500`."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
        await plant(app, {ExchangeKey.BITGET: []})
        async with app.state.db_sessionmaker() as session:
            await plant_price(session, symbol="BTC", amount=Decimal(1), as_of=datetime.now(UTC))
        healthy = await detail(client)
        bodies: dict[str, dict[str, Any]] = {}
        for section, (owner, method) in READS.items():
            with monkeypatch.context() as patch:
                patch.setattr(owner, method, explode)
                bodies[section] = await detail(client)

    for section, body in bodies.items():
        assert body[section] == UNAVAILABLE[section], section
        for other in SECTIONS - {section}:
            assert body[other] == healthy[other], f"{other} changed when {section} failed"
    assert healthy["chains"]["items"][0]["chain_key"] == "bitcoin"
    assert healthy["exchanges"]["items"][0]["exchange_key"] == "bitget"
    assert healthy["prices"]["state"] == "fresh"


# --------------------------------------------------------------------------------------
# The shape: nothing beyond the spec, so no configuration value
# --------------------------------------------------------------------------------------


async def test_every_section_has_exactly_the_specs_keys(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A populated body, walked key by key: an interval or a tolerance would be a new key."""
    del api_environment
    async with application(monkeypatch) as (app, client):
        await plant_the_specs_example(app, Readings())
        await finished_run(
            app,
            datetime.now(UTC),
            ChainOutcome(chain_key="bitcoin", status=SyncRunStatus.SUCCESS, wallets_read=2),
        )
        async with app.state.db_sessionmaker() as session:
            await plant_price(session, symbol="BTC", amount=Decimal(1), as_of=datetime.now(UTC))
        body = await detail(client)

    assert set(body["backup"]) == {
        "state",
        "latest_at",
        "count",
        "last_attempt_at",
        "last_error_kind",
    }
    assert all(set(timer) == SCHEDULER_KEYS for timer in body["schedulers"])
    assert set(body["chains"]) == {"state", "items"}
    assert body["chains"]["items"]
    assert all(set(chain) == CHAIN_KEYS for chain in body["chains"]["items"])
    assert set(body["exchanges"]) == {"state", "items"}
    assert len(body["exchanges"]["items"]) == 2
    assert all(set(account) == EXCHANGE_KEYS for account in body["exchanges"]["items"])
    assert set(body["prices"]) == PRICES_KEYS
    assert set(body["reconciliation"]) == RECONCILIATION_KEYS
    assert "tolerance" not in str(body)
    assert "interval" not in str(body)


# --------------------------------------------------------------------------------------
# No provider is called
# --------------------------------------------------------------------------------------


class Witness:
    """Records every vendor a read could have reached, and refuses each one."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.providers: list[object] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.host)
        message = f"the health detail called a vendor at {request.url.host}"
        raise AssertionError(message)

    def create(self, *arguments: object, **keywords: object) -> object:
        self.providers.append((arguments, keywords))
        message = "the health detail asked the registry for a chain provider"
        raise AssertionError(message)


async def test_reading_the_detail_calls_no_provider(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wallets on both chains, a configured venue, and five reads: nothing is asked of anyone.

    The HTTP client every chain index and price vendor goes through is built over a transport
    that records and refuses; the chain registry records every provider it is asked for; and
    the simulated venue counts its page, discovery and balance calls.
    """
    del api_environment
    witness = Witness()
    monkeypatch.setattr(
        "portfolio.main.build_http_client",
        lambda: build_http_client(transport=httpx.MockTransport(witness.handler)),
    )
    monkeypatch.setattr(CHAIN_PROVIDERS, "create", witness.create)
    venue = SimulatedVenue()
    async with application(monkeypatch, {ExchangeKey.BITGET: venue}) as (app, client):
        await add_wallet(app, ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
        await add_wallet(app, ChainKey.KASPA, KASPA_TESTNET_V0)
        await plant(app, {ExchangeKey.BITGET: []})
        for _ in range(5):
            await detail(client)

    assert witness.requests == []
    assert witness.providers == []
    assert venue.calls == []
    assert venue.symbol_calls == 0
    assert venue.balance_calls == 0


# --------------------------------------------------------------------------------------
# Authentication and the schema
# --------------------------------------------------------------------------------------


async def test_the_detail_is_401_without_a_session_and_not_on_the_allowlist(
    api_client: AsyncClient,
) -> None:
    response = await api_client.get(DETAIL)

    assert response.status_code == 401
    assert DETAIL not in PUBLIC_API_PATHS
    assert "schedulers" not in response.text


def test_the_schema_declares_every_state_as_its_wire_forms(app: FastAPI) -> None:
    """What the generated TypeScript unions are built from, so the page's tables are total."""
    document = app.openapi()
    operation = document["paths"][DETAIL]["get"]
    schemas = document["components"]["schemas"]

    assert operation["operationId"] == "getHealthDetail"
    assert operation["summary"] == (
        "Report how the backups, timers, syncs, prices and holdings check stand"
    )
    assert set(schemas["HealthDetailResponse"]["required"]) == SECTIONS
    assert schemas["SchedulerName"]["enum"] == [
        "balance-sync",
        "price-refresh",
        "exchange-sync",
        "backup",
    ]
    assert schemas["SchedulerState"]["enum"] == ["ok", "late", "stopped", "disabled"]
    assert schemas["SectionState"]["enum"] == ["ok", "unavailable"]
    assert schemas["SourceState"]["enum"] == ["ok", "failing", "never"]
    assert schemas["PriceHealthState"]["enum"] == ["fresh", "stale", "never", "unavailable"]
    assert schemas["ReconciliationHealthState"]["enum"] == [
        "match",
        "mismatch",
        "incomplete",
        "not_computed",
        "unavailable",
    ]
    for name, keys in (
        ("SchedulerStatusResponse", SCHEDULER_KEYS),
        ("ChainHealthResponse", CHAIN_KEYS),
        ("ExchangeHealthResponse", EXCHANGE_KEYS),
        ("PricesHealthResponse", PRICES_KEYS),
        ("ReconciliationHealthResponse", RECONCILIATION_KEYS),
        ("ChainsHealthResponse", {"state", "items"}),
        ("ExchangesHealthResponse", {"state", "items"}),
    ):
        assert set(schemas[name]["properties"]) == keys, name
        assert set(schemas[name]["required"]) == keys, name
