"""Criteria 2, 7 and 8: the four endpoints, over a real database and a real session.

These drive the whole stack -- middleware, router, service, repository, SQLite -- because
what is being asserted is the contract *between* those layers. Two things are stubbed and
each for a stated reason:

* **The vendor, not the service.** `tests/balance_harness.py` replaces
  `ChainProviderRegistry.create`, so the sync endpoint runs the real service, the real
  repositories and the real transaction, and only the HTTP call at the bottom is a
  dictionary. Driving two vendors' JSON from here would make a test about the API also a
  test about Esplora's document shape, which `tests/providers/chains/` already owns.
* **Nothing else.** Every snapshot, price and run these tests read back was written either
  by the application or by raw `INSERT`, never by a mock of a repository.

## The scheduler is off in this suite, deliberately, and that is a claim about the flag

`tests/auth/conftest.py` sets `PORTFOLIO_BALANCE_SYNC_ENABLED=false` for every suite that
enters the real lifespan, and its comment gives the network reason. There is a second
reason here: with the loop on, a fresh database has no finished run, so startup begins one
-- and a manual `POST` arriving while that run is in flight **joins** it by design,
returning `joined: true` and the startup run's zero counts. Every assertion about a manual
run would be a race with a background task.

That makes this suite's basis an assertion in its own right, and it is named rather than
buried: `test_a_manual_sync_answers_even_though_the_scheduler_is_disabled`. The setting
turns the *schedule* off, not the endpoint. An operator debugging a vendor stops the
automatic calls and still needs to be able to press the button; and the spec's API contract
gives `POST /api/balances/sync` no refusal to return if it could not. If that is ever
changed, it is that one test that fails first and says so.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import text

from portfolio.api.errors import PROBLEM_CONTENT_TYPE
from portfolio.config import get_settings
from portfolio.domain.chains import ChainKey
from portfolio.providers.errors import ProviderRateLimitedError, ProviderUnavailableError
from portfolio.services.prices import PriceUnavailable
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP350_TESTNET_V1,
    CORE_REGTEST_P2WPKH,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V1_KEY,
)
from tests.auth.conftest import JSON_HEADERS
from tests.balance_harness import (
    KASPA_SUPPLY_SOMPI,
    MAX_SAFE_INTEGER,
    StubChainProvider,
    snapshots,
    sqlite_timestamp,
    stub_chain_providers,
    sync_runs,
)

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SYNC: Final = "/api/balances/sync"
CURRENT: Final = "/api/balances/current"
RUNS: Final = "/api/balances/runs"
WALLETS: Final = "/api/wallets"

BITCOIN: Final = "bitcoin"
KASPA: Final = "kaspa"

#: One satoshi over 1.23456789 BTC, the spec's own example value.
BTC_UNITS: Final = 123_456_789
BTC_QUANTITY: Final = Decimal("1.23456789")

#: Ten KAS in sompi. Small enough to read, and it is the value the spec's `unpriced` block
#: renders as `"10.00000000"`.
KAS_UNITS: Final = 1_000_000_000
KAS_QUANTITY: Final = Decimal("10.00000000")

#: Deliberately different per currency, so that a response claiming one currency and doing
#: the arithmetic in the other is a failing test rather than an equal number.
BTC_PRICE: Final[dict[str, Decimal]] = {"USD": Decimal("2000.00"), "EUR": Decimal("1000.00")}
KAS_PRICE: Final[dict[str, Decimal]] = {"USD": Decimal("0.50"), "EUR": Decimal("0.25")}

PRICE_SOURCE: Final = "a-vendor"

#: Fixed instants for the snapshot history. Two of them straddle a day boundary at the
#: microsecond, which is what `test_the_history_orders_across_a_boundary_a_string_would_get_wrong`
#: is built on.
FIRST_SEEN: Final = datetime(2026, 9, 9, 23, 59, 59, 999999, tzinfo=UTC)
SECOND_SEEN: Final = datetime(2026, 9, 10, 0, 0, 0, 1, tzinfo=UTC)
THIRD_SEEN: Final = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# Fixtures written by hand, because the point is what the read path does with a row
# --------------------------------------------------------------------------------------


async def create_wallet(
    client: AsyncClient,
    address: str = BIP173_TESTNET_P2WPKH,
    *,
    chain_key: str = BITCOIN,
    label: str | None = "cold",
) -> int:
    """Register a wallet through the API and return its id, failing loudly on a refusal."""
    body: dict[str, Any] = {"chain_key": chain_key, "address": address}
    if label is not None:
        body["label"] = label
    response = await client.post(WALLETS, json=body, headers=JSON_HEADERS)
    assert response.status_code == 201, response.text
    identifier: int = response.json()["id"]
    return identifier


async def insert_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    started_at: datetime,
    status: str = "success",
    trigger: str = "scheduled",
    wallets_total: int = 1,
) -> int:
    """A finished `sync_runs` row, written by hand so a snapshot has something to point at."""
    async with sessionmaker() as session:
        result = await session.execute(
            text(
                "INSERT INTO sync_runs "
                "(trigger, status, started_at, finished_at, duration_ms, "
                " wallets_total, wallets_succeeded, wallets_failed) "
                "VALUES (:trigger, :status, :started_at, :finished_at, 1, :total, :total, 0) "
                "RETURNING id"
            ),
            {
                "trigger": trigger,
                "status": status,
                "started_at": sqlite_timestamp(started_at),
                "finished_at": sqlite_timestamp(started_at),
                "total": wallets_total,
            },
        )
        run_id: int = result.scalar_one()
        await session.commit()
        return run_id


async def insert_snapshot(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    wallet_id: int,
    run_id: int,
    confirmed: int,
    observed_at: datetime,
    pending: int | None = None,
    decimals: int = 8,
) -> None:
    """One `balance_snapshots` row, exactly as the writer would have left it."""
    async with sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO balance_snapshots "
                "(wallet_id, sync_run_id, confirmed, pending, decimals, observed_at) "
                "VALUES (:wallet_id, :run_id, :confirmed, :pending, :decimals, :observed_at)"
            ),
            {
                "wallet_id": wallet_id,
                "run_id": run_id,
                "confirmed": confirmed,
                "pending": pending,
                "decimals": decimals,
                "observed_at": sqlite_timestamp(observed_at),
            },
        )
        await session.commit()


async def insert_price(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    symbol: str,
    currency: str,
    amount: Decimal,
    as_of: datetime,
    source: str = PRICE_SOURCE,
) -> None:
    """A `prices` row in the fixed-point text `NumericText(12)` writes.

    Formatted rather than handed a `Decimal`, because these go in through `text()` and the
    column's type decorator is therefore not on the path. Twelve places is `PRICE_SCALE`,
    and a fixture that wrote a different number of them would be storing a string the
    application can read but never produces.
    """
    async with sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO prices (asset_id, quote_currency, amount, source, as_of, fetched_at) "
                "VALUES ((SELECT id FROM assets WHERE symbol = :symbol), "
                ":currency, :amount, :source, :as_of, :as_of)"
            ),
            {
                "symbol": symbol,
                "currency": currency,
                "amount": f"{amount:.12f}",
                "source": source,
                "as_of": sqlite_timestamp(as_of),
            },
        )
        await session.commit()


async def price_everything(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    age: timedelta = timedelta(minutes=5),
) -> datetime:
    """Both assets, both currencies, fresh enough not to be stale. Returns the instant used.

    Relative to the real clock rather than to a fixed date, because staleness is decided
    against `datetime.now` inside a service this suite has no way to inject a clock into.
    Five minutes is comfortably inside `STALE_AFTER`, and no test suite runs for an hour.
    """
    as_of = datetime.now(UTC) - age
    for currency in ("USD", "EUR"):
        await insert_price(
            sessionmaker, symbol="BTC", currency=currency, amount=BTC_PRICE[currency], as_of=as_of
        )
        await insert_price(
            sessionmaker, symbol="KAS", currency=currency, amount=KAS_PRICE[currency], as_of=as_of
        )
    return as_of


async def history(client: AsyncClient, wallet_id: int, **params: Any) -> dict[str, Any]:
    response = await client.get(f"{WALLETS}/{wallet_id}/balances", params=params or None)
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


# --------------------------------------------------------------------------------------
# Criterion 2: the manual endpoint
# --------------------------------------------------------------------------------------


async def test_a_manual_sync_returns_the_run_summary(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 2, end to end: two chains read, one summary back, and rows behind it.

    The summary is asserted field by field against the spec's example shape rather than by
    `set(payload)` alone, because a body with the right keys and the wrong counts is the
    failure a shape check cannot see. The database is then read separately: a summary is
    built by the same call that wrote the rows, so a summary agreeing with itself would
    prove nothing about what survived the transaction.
    """
    bitcoin_wallet = await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    kaspa_wallet = await create_wallet(
        signed_in_api_client, KASPA_TESTNET_V0, chain_key=KASPA, label="hot"
    )
    bitcoin = StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS})
    kaspa = StubChainProvider(ChainKey.KASPA, {KASPA_TESTNET_V0: KAS_UNITS})
    registry = stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: bitcoin, ChainKey.KASPA: kaspa})

    response = await signed_in_api_client.post(SYNC, headers=JSON_HEADERS)

    assert response.status_code == 200, response.text
    summary = response.json()
    assert set(summary) == {
        "run_id",
        "trigger",
        "joined",
        "status",
        "started_at",
        "finished_at",
        "duration_ms",
        "wallets_total",
        "wallets_succeeded",
        "wallets_failed",
        "chains",
    }
    assert summary["trigger"] == "manual"
    assert summary["joined"] is False
    assert summary["status"] == "success"
    assert (summary["wallets_total"], summary["wallets_succeeded"], summary["wallets_failed"]) == (
        2,
        2,
        0,
    )
    assert isinstance(summary["duration_ms"], int)
    assert summary["duration_ms"] >= 0
    assert summary["finished_at"] is not None
    chains = {chain["chain_key"]: chain for chain in summary["chains"]}
    assert set(chains) == {BITCOIN, KASPA}
    assert all(chain["status"] == "success" for chain in chains.values())
    assert all(chain["error_kind"] is None and chain["detail"] is None for chain in chains.values())
    assert {chain["wallets_read"] for chain in chains.values()} == {1}

    # The harness was really on the path, and each chain was really asked.
    assert sorted(registry.created) == [BITCOIN, KASPA]
    assert bitcoin.calls == [(BIP173_TESTNET_P2WPKH,)]
    assert kaspa.calls == [(KASPA_TESTNET_V0,)]

    stored = await snapshots(api_sessionmaker)
    assert {(row["wallet_id"], row["confirmed"]) for row in stored} == {
        (bitcoin_wallet, BTC_UNITS),
        (kaspa_wallet, KAS_UNITS),
    }
    assert {row["sync_run_id"] for row in stored} == {summary["run_id"]}


async def test_a_manual_sync_answers_even_though_the_scheduler_is_disabled(
    signed_in_api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`PORTFOLIO_BALANCE_SYNC_ENABLED=false` stops the schedule, not the button.

    This is the assertion the whole suite rests on, named so that a change of mind about
    the flag fails here first rather than as eleven unrelated tests going red at once.
    The reasoning is in the module docstring: an operator who turns the schedule off to
    stop hammering a vendor still needs to trigger a read by hand, and the spec's API
    contract gives this endpoint no refusal to return if it could not.
    """
    assert get_settings().balance_sync_enabled is False
    await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    stub_chain_providers(
        monkeypatch,
        {ChainKey.BITCOIN: StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS})},
    )

    response = await signed_in_api_client.post(SYNC, headers=JSON_HEADERS)

    assert response.status_code == 200, response.text


async def test_the_sync_endpoint_requires_a_session(api_client: AsyncClient) -> None:
    """Rule 8 at the one endpoint that costs a vendor a request. No cookie, no read."""
    response = await api_client.post(SYNC, headers=JSON_HEADERS)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)


async def test_a_sync_with_no_wallets_is_a_success_with_zero_counts(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty page, which is what every fresh deployment's first run looks like.

    A run over no wallets is a `success` with zero counts and no chain rows -- not a crash,
    and not a `failed` that would make an operator go looking for a vendor outage on the
    day they installed the thing.
    """
    registry = stub_chain_providers(monkeypatch, {})

    response = await signed_in_api_client.post(SYNC, headers=JSON_HEADERS)

    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["status"] == "success"
    assert (summary["wallets_total"], summary["wallets_succeeded"], summary["wallets_failed"]) == (
        0,
        0,
        0,
    )
    assert summary["chains"] == []
    assert registry.created == [], "no wallets means no provider is built at all"
    assert len(await sync_runs(api_sessionmaker)) == 1, "criterion 4: the row is still written"


async def test_an_archived_wallet_is_not_read(
    signed_in_api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archiving is what stops a wallet costing a vendor a request every fifteen minutes.

    The counterpart to `test_an_archived_wallet_still_answers_with_its_history`: the row
    and its history stay, and the address is never asked about again. A sync that still
    read it would make archiving a display preference rather than a decision.
    """
    live = await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    retired = await create_wallet(signed_in_api_client, BIP350_TESTNET_V1, label="retired")
    assert (
        await signed_in_api_client.delete(f"{WALLETS}/{retired}", headers=JSON_HEADERS)
    ).status_code == 204
    bitcoin = StubChainProvider(
        ChainKey.BITCOIN,
        {BIP173_TESTNET_P2WPKH: BTC_UNITS, BIP350_TESTNET_V1: 1},
    )
    stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: bitcoin})

    summary = (await signed_in_api_client.post(SYNC, headers=JSON_HEADERS)).json()

    assert summary["wallets_total"] == 1
    assert bitcoin.calls == [(BIP173_TESTNET_P2WPKH,)], "the archived address must not be asked"
    assert live != retired


# --------------------------------------------------------------------------------------
# Criterion 4 seen from outside: the runs endpoint
# --------------------------------------------------------------------------------------


async def test_the_runs_endpoint_reports_the_run_newest_first(
    signed_in_api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 4 without opening the database, which is what makes it observable.

    Two runs, so "newest first" is a statement rather than a coincidence of one row, and
    the chain rows are asserted to travel with their own run instead of being pooled.
    """
    await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    stub_chain_providers(
        monkeypatch,
        {ChainKey.BITCOIN: StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS})},
    )
    first = (await signed_in_api_client.post(SYNC, headers=JSON_HEADERS)).json()
    second = (await signed_in_api_client.post(SYNC, headers=JSON_HEADERS)).json()
    assert first["run_id"] != second["run_id"], "a second call must start a second run"

    response = await signed_in_api_client.get(RUNS)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"runs"}
    assert [run["run_id"] for run in payload["runs"]] == [second["run_id"], first["run_id"]]
    assert all(run["status"] == "success" for run in payload["runs"])
    assert all(
        [chain["chain_key"] for chain in run["chains"]] == [BITCOIN] for run in payload["runs"]
    )


async def test_the_runs_limit_is_bounded_at_both_ends(signed_in_api_client: AsyncClient) -> None:
    """`limit` is 1..200. Outside that it is a 422, not a clamp nobody was told about."""
    assert (await signed_in_api_client.get(RUNS, params={"limit": 0})).status_code == 422
    assert (await signed_in_api_client.get(RUNS, params={"limit": 201})).status_code == 422
    assert (await signed_in_api_client.get(RUNS, params={"limit": 1})).status_code == 200
    assert (await signed_in_api_client.get(RUNS, params={"limit": 200})).status_code == 200


async def test_a_failed_chain_reaches_the_runs_endpoint_with_its_kind(
    signed_in_api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kind and the detail survive the round trip to the operator who has to act on it.

    A summary that reported `status: "failed"` and dropped `error_kind` would send somebody
    to read a log; the whole argument for a per-chain vocabulary is that they should not
    have to. The detail is the provider's own message, and it is asserted **not** to carry
    the address -- `providers/errors.py` promises that and this is the endpoint where the
    promise is cashed.
    """
    await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    await create_wallet(signed_in_api_client, KASPA_TESTNET_V0, chain_key=KASPA, label="hot")
    stub_chain_providers(
        monkeypatch,
        {
            ChainKey.BITCOIN: StubChainProvider(
                ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS}
            ),
            ChainKey.KASPA: StubChainProvider(
                ChainKey.KASPA,
                raises=ProviderRateLimitedError("the vendor asked us to slow down"),
            ),
        },
    )
    await signed_in_api_client.post(SYNC, headers=JSON_HEADERS)

    runs = (await signed_in_api_client.get(RUNS)).json()["runs"]

    chains = {chain["chain_key"]: chain for chain in runs[0]["chains"]}
    assert runs[0]["status"] == "partial"
    assert chains[KASPA]["error_kind"] == "rate_limited"
    assert chains[KASPA]["detail"] == "the vendor asked us to slow down"
    assert chains[BITCOIN]["error_kind"] is None
    body = json.dumps(runs)
    assert KASPA_TESTNET_V0 not in body
    assert BIP173_TESTNET_P2WPKH not in body


# --------------------------------------------------------------------------------------
# Criterion 7: current balances, valued
# --------------------------------------------------------------------------------------


async def test_current_balances_are_valued_against_the_price_cache(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 7: a snapshot times a cached price, with every money field a string.

    The expected total is computed from the currency the response *says* it is in, so a
    body that labelled itself EUR and did the arithmetic in USD fails here. The two prices
    differ by a factor of two for exactly that reason, and the default is pinned separately
    in `test_the_quote_currency_defaults_to_euro_and_can_be_chosen` -- the spec shows the
    field in the body and never says where it comes from, so the two halves are asserted
    apart rather than one of them being assumed inside the other.
    """
    bitcoin_wallet = await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    kaspa_wallet = await create_wallet(
        signed_in_api_client, KASPA_TESTNET_V0, chain_key=KASPA, label="hot"
    )
    run_id = await insert_run(api_sessionmaker, started_at=THIRD_SEEN, wallets_total=2)
    await insert_snapshot(
        api_sessionmaker,
        wallet_id=bitcoin_wallet,
        run_id=run_id,
        confirmed=BTC_UNITS,
        observed_at=THIRD_SEEN,
    )
    await insert_snapshot(
        api_sessionmaker,
        wallet_id=kaspa_wallet,
        run_id=run_id,
        confirmed=KAS_UNITS,
        observed_at=THIRD_SEEN,
    )
    await price_everything(api_sessionmaker)

    response = await signed_in_api_client.get(CURRENT)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"quote_currency", "total", "complete", "as_of", "wallets", "unpriced"}
    currency = payload["quote_currency"]
    assert currency in {"EUR", "USD"}
    expected = BTC_QUANTITY * BTC_PRICE[currency] + KAS_QUANTITY * KAS_PRICE[currency]
    assert Decimal(payload["total"]) == expected
    assert payload["complete"] is True
    assert payload["unpriced"] == []

    by_wallet = {wallet["wallet_id"]: wallet for wallet in payload["wallets"]}
    assert set(by_wallet) == {bitcoin_wallet, kaspa_wallet}
    btc = by_wallet[bitcoin_wallet]
    assert set(btc) == {
        "wallet_id",
        "chain_key",
        "label",
        "asset_symbol",
        "confirmed",
        "pending",
        "decimals",
        "quantity",
        "value",
        "price",
        "observed_at",
    }
    assert (btc["chain_key"], btc["asset_symbol"], btc["label"]) == (BITCOIN, "BTC", "cold")
    assert btc["decimals"] == 8
    assert Decimal(btc["quantity"]) == BTC_QUANTITY
    assert Decimal(btc["value"]) == BTC_QUANTITY * BTC_PRICE[currency]
    assert btc["price"]["source"] == PRICE_SOURCE
    assert btc["price"]["stale"] is False
    assert Decimal(btc["price"]["amount"]) == BTC_PRICE[currency]
    assert by_wallet[kaspa_wallet]["asset_symbol"] == "KAS"


async def test_the_quote_currency_defaults_to_euro_and_can_be_chosen(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The default is EUR and the query parameter overrides it, with the arithmetic following.

    Two separate claims, and the second is what makes the first worth making: an endpoint
    that ignored the parameter and always valued in EUR would still label the body `"USD"`
    if only the label were asserted. The BTC price differs by a factor of two between the
    two currencies precisely so that the total tells them apart.

    A currency nothing prices is a third case and it is here too. `GBP` is not one of the
    two `prices.quote_currency` admits, and the spec does not say whether that is a 422 or
    an unpriced valuation -- so what is asserted is the one answer that is wrong either
    way: **it must not come back `complete` with a total of zero.** A portfolio reported as
    fully valued at nothing is the exact failure #9's completeness flag exists to prevent,
    and it is the shape both plausible implementations have to avoid.
    """
    wallet = await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    run_id = await insert_run(api_sessionmaker, started_at=THIRD_SEEN)
    await insert_snapshot(
        api_sessionmaker,
        wallet_id=wallet,
        run_id=run_id,
        confirmed=BTC_UNITS,
        observed_at=THIRD_SEEN,
    )
    await price_everything(api_sessionmaker)

    default = (await signed_in_api_client.get(CURRENT)).json()
    chosen = (await signed_in_api_client.get(CURRENT, params={"quote_currency": "USD"})).json()
    refused = await signed_in_api_client.get(CURRENT, params={"quote_currency": "GBP"})

    assert default["quote_currency"] == "EUR"
    assert Decimal(default["total"]) == BTC_QUANTITY * BTC_PRICE["EUR"]
    assert chosen["quote_currency"] == "USD"
    assert Decimal(chosen["total"]) == BTC_QUANTITY * BTC_PRICE["USD"]
    assert Decimal(chosen["total"]) != Decimal(default["total"])
    if refused.status_code == 200:
        assert refused.json()["complete"] is False, (
            "a currency nothing prices must not report a complete valuation"
        )
    else:
        assert refused.status_code == 422


async def test_an_unpriced_asset_makes_the_total_incomplete(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """#9's contract, re-asserted at the endpoint: the total omits what it could not price.

    The total is asserted **after** `complete` and the named asset, deliberately: an
    implementation that silently dropped the Kaspa holding produces exactly this number,
    and only the other two assertions can tell the two apart.

    `reason` is asserted against the shipped `PriceUnavailable` vocabulary rather than
    against the spec's example, which writes `"no_price"` -- a string that is not one of the
    four members `services/prices.py` defines. A reason nothing can branch on is the
    failure that enum exists to prevent.
    """
    bitcoin_wallet = await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    kaspa_wallet = await create_wallet(
        signed_in_api_client, KASPA_TESTNET_V0, chain_key=KASPA, label="hot"
    )
    run_id = await insert_run(api_sessionmaker, started_at=THIRD_SEEN, wallets_total=2)
    for wallet_id, units in ((bitcoin_wallet, BTC_UNITS), (kaspa_wallet, KAS_UNITS)):
        await insert_snapshot(
            api_sessionmaker,
            wallet_id=wallet_id,
            run_id=run_id,
            confirmed=units,
            observed_at=THIRD_SEEN,
        )
    as_of = datetime.now(UTC) - timedelta(minutes=5)
    for currency in ("USD", "EUR"):
        await insert_price(
            api_sessionmaker,
            symbol="BTC",
            currency=currency,
            amount=BTC_PRICE[currency],
            as_of=as_of,
        )

    payload = (await signed_in_api_client.get(CURRENT)).json()

    currency = payload["quote_currency"]
    assert payload["complete"] is False
    assert [line["asset_symbol"] for line in payload["unpriced"]] == ["KAS"]
    assert Decimal(payload["unpriced"][0]["quantity"]) == KAS_QUANTITY
    assert payload["unpriced"][0]["reason"] in set(PriceUnavailable), (
        "the reason must be one of the four members a caller can branch on"
    )
    assert payload["unpriced"][0]["reason"] == PriceUnavailable.NEVER_FETCHED
    assert Decimal(payload["total"]) == BTC_QUANTITY * BTC_PRICE[currency]


async def test_a_wallet_with_no_snapshot_reports_null_rather_than_zero(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A zero balance and an unread wallet are different facts, and the dashboard may say which.

    The wallet with a real zero is the control: without it, `confirmed: null` would be
    indistinguishable from "the endpoint returns null for everything it has not valued".
    """
    unread = await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH, label="never read")
    empty = await create_wallet(signed_in_api_client, BIP350_TESTNET_V1, label="really empty")
    run_id = await insert_run(api_sessionmaker, started_at=THIRD_SEEN)
    await insert_snapshot(
        api_sessionmaker,
        wallet_id=empty,
        run_id=run_id,
        confirmed=0,
        observed_at=THIRD_SEEN,
    )
    await price_everything(api_sessionmaker)

    payload = (await signed_in_api_client.get(CURRENT)).json()

    by_wallet = {wallet["wallet_id"]: wallet for wallet in payload["wallets"]}
    assert set(by_wallet) == {unread, empty}, "an unread wallet still appears; it is not omitted"
    assert by_wallet[unread]["confirmed"] is None
    assert by_wallet[unread]["observed_at"] is None
    assert by_wallet[empty]["confirmed"] == "0"
    assert by_wallet[empty]["observed_at"] is not None
    assert Decimal(by_wallet[empty]["quantity"]) == 0


async def test_only_the_newest_snapshot_of_a_wallet_is_current(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """ "Current" is the latest row, resolved by identity order rather than by an amount.

    The older row carries the **larger** balance on purpose. A read that picked a row by
    `MAX(confirmed)`, or that ordered by anything to do with the money, would return the
    stale one and every assertion about the sum would still look plausible.
    """
    wallet = await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    older = await insert_run(api_sessionmaker, started_at=FIRST_SEEN)
    newer = await insert_run(api_sessionmaker, started_at=THIRD_SEEN)
    await insert_snapshot(
        api_sessionmaker,
        wallet_id=wallet,
        run_id=older,
        confirmed=BTC_UNITS * 9,
        observed_at=FIRST_SEEN,
    )
    await insert_snapshot(
        api_sessionmaker,
        wallet_id=wallet,
        run_id=newer,
        confirmed=BTC_UNITS,
        observed_at=THIRD_SEEN,
    )
    await price_everything(api_sessionmaker)

    payload = (await signed_in_api_client.get(CURRENT)).json()

    wallets = payload["wallets"]
    assert len(wallets) == 1, "one wallet is one line, whatever its history"
    assert wallets[0]["confirmed"] == str(BTC_UNITS)
    assert payload["as_of"] is not None


async def test_the_current_endpoint_requires_a_session(api_client: AsyncClient) -> None:
    """Balances are the holdings themselves; this is the body that must never be public."""
    assert (await api_client.get(CURRENT)).status_code == 401


# --------------------------------------------------------------------------------------
# Criteria 7 and 8: one wallet's history
# --------------------------------------------------------------------------------------


@pytest.fixture
async def wallet_with_history(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[int, list[int]]:
    """One wallet, three snapshots, inserted newest first so ordering cannot come for free.

    The balances step across a digit boundary -- 9, 10, 11 whole coins -- so that a read
    which ordered by the amount as text would put 11 before 9 and this suite would see it.
    """
    wallet = await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    written = [
        (THIRD_SEEN, 1_100_000_000),
        (FIRST_SEEN, 900_000_000),
        (SECOND_SEEN, 1_000_000_000),
    ]
    for observed_at, confirmed in written:
        run_id = await insert_run(api_sessionmaker, started_at=observed_at)
        await insert_snapshot(
            api_sessionmaker,
            wallet_id=wallet,
            run_id=run_id,
            confirmed=confirmed,
            observed_at=observed_at,
        )
    return wallet, [900_000_000, 1_000_000_000, 1_100_000_000]


async def test_wallet_history_is_oldest_first(
    signed_in_api_client: AsyncClient,
    wallet_with_history: tuple[int, list[int]],
) -> None:
    """Criterion 8: a chart reads left to right, so the series arrives in that order.

    Rows were inserted out of order, so identity order and chronological order disagree --
    which is what makes this an assertion about the `ORDER BY` rather than about `rowid`.
    """
    wallet, expected = wallet_with_history

    payload = await history(signed_in_api_client, wallet)

    assert set(payload) == {"wallet_id", "decimals", "snapshots"}
    assert payload["wallet_id"] == wallet
    assert payload["decimals"] == 8
    assert [int(row["confirmed"]) for row in payload["snapshots"]] == expected
    observed = [row["observed_at"] for row in payload["snapshots"]]
    assert observed == sorted(observed)
    assert set(payload["snapshots"][0]) == {
        "observed_at",
        "confirmed",
        "pending",
        "quantity",
        "sync_run_id",
    }


async def test_the_history_orders_across_a_boundary_a_string_would_get_wrong(
    signed_in_api_client: AsyncClient,
    wallet_with_history: tuple[int, list[int]],
) -> None:
    """The spec's asymmetry, cashed: a timestamp may be compared in SQL and money may not.

    The three snapshots hold 9, 10 and 11 coins, in that chronological order. Ordering them
    by the *amount* as text would give 10, 11, 9 -- because `"10"` and `"11"` sort before
    `"9"` -- which is exactly the mistake rule 2 forbids and exactly what this sequence
    would expose. The control below computes that wrong order here rather than describing
    it, so the assertion above is not merely a sorted list agreeing with itself.

    The timestamps straddle midnight at the microsecond for the other half of the claim:
    `UtcDateTime` writes a fixed width, so `23:59:59.999999` on the ninth sorts before
    `00:00:00.000001` on the tenth both lexicographically and chronologically.
    """
    wallet, _expected = wallet_with_history

    payload = await history(signed_in_api_client, wallet)

    quantities = [row["quantity"] for row in payload["snapshots"]]
    assert quantities == sorted(quantities, key=Decimal)
    assert quantities != sorted(quantities), (
        "these values must sort differently as text and as numbers, or this proves nothing"
    )


async def test_since_filters_the_history(
    signed_in_api_client: AsyncClient,
    wallet_with_history: tuple[int, list[int]],
) -> None:
    """Criterion 8's window. `since` is inclusive, which is the boundary worth pinning.

    Asked for exactly the second snapshot's instant: an exclusive comparison drops it and
    a chart silently starts one point late every time somebody pages backwards through it.
    """
    wallet, _expected = wallet_with_history

    payload = await history(signed_in_api_client, wallet, since=SECOND_SEEN.isoformat())

    assert [int(row["confirmed"]) for row in payload["snapshots"]] == [1_000_000_000, 1_100_000_000]


async def test_since_in_the_future_is_an_empty_series_rather_than_an_error(
    signed_in_api_client: AsyncClient,
    wallet_with_history: tuple[int, list[int]],
) -> None:
    """The empty page. Nothing to chart is a fact about the window, not a failure."""
    wallet, _expected = wallet_with_history

    payload = await history(signed_in_api_client, wallet, since="2099-01-01T00:00:00Z")

    assert payload["snapshots"] == []


async def test_limit_is_bounded(
    signed_in_api_client: AsyncClient,
    wallet_with_history: tuple[int, list[int]],
) -> None:
    """`limit` is 1..1000, and outside that it is a 422 rather than a silent clamp.

    A clamp is the tempting alternative and it is worse: a client asking for 10,000 points
    would get 1,000 and have no way to know its window was cut.
    """
    wallet, _expected = wallet_with_history
    path = f"{WALLETS}/{wallet}/balances"

    assert (await signed_in_api_client.get(path, params={"limit": 0})).status_code == 422
    assert (await signed_in_api_client.get(path, params={"limit": 1001})).status_code == 422
    assert (await signed_in_api_client.get(path, params={"limit": 1000})).status_code == 200
    limited = await history(signed_in_api_client, wallet, limit=2)
    assert len(limited["snapshots"]) == 2


async def test_another_users_wallet_is_a_404(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A wallet id is a small integer, so enumeration is the attack this answer prevents.

    A `403` would confirm the wallet exists, which is the one bit worth protecting: the set
    of addresses this application watches *is* the owner's holdings. The row is inserted for
    a second user directly, because nothing in the API creates one.
    """
    async with api_sessionmaker() as session:
        result = await session.execute(
            text(
                "INSERT INTO users (username, password_hash, created_at) "
                "VALUES ('someone-else', 'not-a-hash', :created_at) RETURNING id"
            ),
            {"created_at": sqlite_timestamp(THIRD_SEEN)},
        )
        other_user = result.scalar_one()
        result = await session.execute(
            text(
                "INSERT INTO wallets (user_id, chain_key, address_canonical, address_display, "
                "label, archived_at, created_at, updated_at) "
                "VALUES (:user_id, 'bitcoin', :address, :address, NULL, NULL, :now, :now) "
                "RETURNING id"
            ),
            {
                "user_id": other_user,
                "address": CORE_REGTEST_P2WPKH,
                "now": sqlite_timestamp(THIRD_SEEN),
            },
        )
        their_wallet = result.scalar_one()
        await session.commit()

    response = await signed_in_api_client.get(f"{WALLETS}/{their_wallet}/balances")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert CORE_REGTEST_P2WPKH not in response.text


async def test_an_unknown_wallet_id_is_a_404(signed_in_api_client: AsyncClient) -> None:
    """The same answer for a wallet that never existed, so the two are indistinguishable."""
    assert (await signed_in_api_client.get(f"{WALLETS}/999999/balances")).status_code == 404


async def test_an_archived_wallet_still_answers_with_its_history(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Its history is the reason archiving is a timestamp rather than a `DELETE`.

    Archived after the snapshot was written, which is the real sequence: a wallet is
    retired because it is empty, and what the owner wants afterwards is the record of when
    it stopped holding anything.
    """
    wallet = await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    run_id = await insert_run(api_sessionmaker, started_at=THIRD_SEEN)
    await insert_snapshot(
        api_sessionmaker,
        wallet_id=wallet,
        run_id=run_id,
        confirmed=BTC_UNITS,
        observed_at=THIRD_SEEN,
    )
    assert (
        await signed_in_api_client.delete(f"{WALLETS}/{wallet}", headers=JSON_HEADERS)
    ).status_code == 204

    payload = await history(signed_in_api_client, wallet)

    assert [int(row["confirmed"]) for row in payload["snapshots"]] == [BTC_UNITS]


async def test_the_history_endpoint_requires_a_session(api_client: AsyncClient) -> None:
    """One wallet's balance history is the same disclosure as all of them, for one address."""
    assert (await api_client.get(f"{WALLETS}/1/balances")).status_code == 401


# --------------------------------------------------------------------------------------
# The wire format, which is not the money rule and is not optional either
# --------------------------------------------------------------------------------------


async def test_base_units_cross_the_wire_as_strings(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`confirmed` and `pending` are JSON strings, asserted against the raw text.

    `response.json()` would turn `"123456789"` and `123456789` into the same Python `int`
    for `pending`'s sake and into different objects for `confirmed`'s, so the check is made
    against the characters on the wire. That is the only place the difference exists.

    The tri-state is asserted in the same body: a `pending` of `null` means the chain does
    not answer the question, and it is not a zero and not the string `"None"`.
    """
    priced = await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    mempool = await create_wallet(
        signed_in_api_client, BIP350_TESTNET_V1, label="with a mempool delta"
    )
    run_id = await insert_run(api_sessionmaker, started_at=THIRD_SEEN, wallets_total=2)
    await insert_snapshot(
        api_sessionmaker,
        wallet_id=priced,
        run_id=run_id,
        confirmed=BTC_UNITS,
        observed_at=THIRD_SEEN,
    )
    await insert_snapshot(
        api_sessionmaker,
        wallet_id=mempool,
        run_id=run_id,
        confirmed=BTC_UNITS,
        pending=-500,
        observed_at=THIRD_SEEN,
    )
    await price_everything(api_sessionmaker)

    raw = (await signed_in_api_client.get(CURRENT)).text

    assert f'"confirmed":"{BTC_UNITS}"' in raw.replace(" ", "")
    assert '"pending":null' in raw.replace(" ", "")
    assert '"pending":"-500"' in raw.replace(" ", "")
    assert f'"confirmed":{BTC_UNITS}' not in raw.replace(" ", ""), "a JSON number is the bug"

    payload = json.loads(raw)
    by_wallet = {wallet["wallet_id"]: wallet for wallet in payload["wallets"]}
    assert by_wallet[priced]["pending"] is None
    assert by_wallet[mempool]["pending"] == "-500"


async def test_a_kaspa_balance_past_the_javascript_safe_integer_survives_the_round_trip(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The spec's measured case: 2.87e18 sompi, three hundred times `Number.MAX_SAFE_INTEGER`.

    The failure this prevents is silent -- the number arrives rounded, renders fine, and is
    wrong in the last digits -- so the assertion is made on the characters and then on what
    a JavaScript parser would have done to them. `json.loads` in Python is not the browser:
    it reads an arbitrary-precision integer, so a Python-level equality check would pass for
    a body that `JSON.parse` destroys. The `float` round trip below is the browser's
    behaviour, reproduced, and it is asserted to differ.
    """
    wallet = await create_wallet(
        signed_in_api_client, KASPA_TESTNET_V1_KEY, chain_key=KASPA, label="whale"
    )
    run_id = await insert_run(api_sessionmaker, started_at=THIRD_SEEN)
    await insert_snapshot(
        api_sessionmaker,
        wallet_id=wallet,
        run_id=run_id,
        confirmed=KASPA_SUPPLY_SOMPI,
        observed_at=THIRD_SEEN,
    )
    await price_everything(api_sessionmaker)

    raw = (await signed_in_api_client.get(CURRENT)).text

    assert KASPA_SUPPLY_SOMPI > MAX_SAFE_INTEGER, "the fixture has to be past the limit"
    assert f'"{KASPA_SUPPLY_SOMPI}"' in raw.replace(" ", "")
    payload = json.loads(raw)
    confirmed = payload["wallets"][0]["confirmed"]
    assert isinstance(confirmed, str)
    assert int(confirmed) == KASPA_SUPPLY_SOMPI
    assert int(float(confirmed)) != KASPA_SUPPLY_SOMPI, (
        "this value has to be one a double cannot hold, or the test proves nothing"
    )
    assert Decimal(payload["wallets"][0]["quantity"]) == Decimal(KASPA_SUPPLY_SOMPI).scaleb(-8)


async def test_every_money_field_is_a_string_and_none_is_a_json_number(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Rule 2 at the boundary, over the whole document rather than field by field.

    A test that named the five money fields would keep passing when a sixth was added, and
    the sixth is the one that would arrive as a number. Walking the parsed body and asking
    the type of everything under a money-ish name is what covers the field nobody has
    written yet.
    """
    wallet = await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    run_id = await insert_run(api_sessionmaker, started_at=THIRD_SEEN)
    await insert_snapshot(
        api_sessionmaker,
        wallet_id=wallet,
        run_id=run_id,
        confirmed=BTC_UNITS,
        pending=1,
        observed_at=THIRD_SEEN,
    )
    await price_everything(api_sessionmaker)
    money_names = {"total", "value", "quantity", "amount", "confirmed", "pending"}

    payload = json.loads((await signed_in_api_client.get(CURRENT)).text)
    payload["history"] = await history(signed_in_api_client, wallet)

    offences: list[str] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in money_names and not isinstance(value, str | type(None)):
                    offences.append(f"{path}.{key} is {type(value).__name__}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(payload, "$")

    assert offences == []
    # The walk has to have seen something, or an empty body would satisfy it.
    assert Decimal(payload["total"]) >= 0


async def test_the_walk_over_money_fields_can_actually_fail() -> None:
    """The control for the test above: a number under a money name has to be found.

    Without this, a walk with a typo in `money_names` -- or one that never recursed --
    would report no offences for a document full of them.
    """
    money_names = {"total", "value", "quantity", "amount", "confirmed", "pending"}
    document = {"wallets": [{"confirmed": 123, "label": "fine"}], "total": "1.00"}
    offences: list[str] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in money_names and not isinstance(value, str | type(None)):
                    offences.append(f"{path}.{key} is {type(value).__name__}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(document, "$")

    assert offences == ["$.wallets[0].confirmed is int"]


# --------------------------------------------------------------------------------------
# Failure isolation, seen from the endpoint rather than from the service
# --------------------------------------------------------------------------------------


async def test_one_chain_failing_still_returns_the_other_chains_balances(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 3 where the user meets it: sync, then read, with one vendor down.

    `tests/services/test_balance_sync.py` proves the isolation at the service. This proves
    the consequence the issue is actually about -- the dashboard still shows Bitcoin -- and
    it would fail for a service that rolled the whole run back on one chain's exception,
    which is the implementation a single `try` around the `gather` produces.
    """
    bitcoin_wallet = await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    await create_wallet(signed_in_api_client, KASPA_TESTNET_V0, chain_key=KASPA, label="hot")
    stub_chain_providers(
        monkeypatch,
        {
            ChainKey.BITCOIN: StubChainProvider(
                ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_UNITS}
            ),
            ChainKey.KASPA: StubChainProvider(
                ChainKey.KASPA, raises=ProviderUnavailableError("no configured endpoint answered")
            ),
        },
    )
    await price_everything(api_sessionmaker)

    summary = (await signed_in_api_client.post(SYNC, headers=JSON_HEADERS)).json()
    payload = (await signed_in_api_client.get(CURRENT)).json()

    assert summary["status"] == "partial"
    assert (summary["wallets_succeeded"], summary["wallets_failed"]) == (1, 1)
    by_wallet = {wallet["wallet_id"]: wallet for wallet in payload["wallets"]}
    assert by_wallet[bitcoin_wallet]["confirmed"] == str(BTC_UNITS)
    assert len(await snapshots(api_sessionmaker)) == 1, "only the chain that answered wrote"


async def test_a_naive_since_is_refused_rather_than_assumed_to_be_utc(
    signed_in_api_client: AsyncClient,
    wallet_with_history: tuple[int, list[int]],
) -> None:
    """A timestamp with no offset names no instant, and guessing one is how a chart lies.

    `2026-09-10T00:00:00` means a different moment in every timezone a browser might be in,
    and assuming UTC would silently shift a European user's window by two hours -- visible
    only as a chart that begins in slightly the wrong place. The refusal is a 422, which is
    something a client can act on.

    The aware form of the same instant is the control, so the refusal is about the missing
    offset rather than about the parameter being rejected in general.
    """
    wallet, _expected = wallet_with_history
    path = f"{WALLETS}/{wallet}/balances"

    naive = await signed_in_api_client.get(path, params={"since": "2026-09-10T00:00:00"})
    aware = await signed_in_api_client.get(path, params={"since": "2026-09-10T00:00:00Z"})

    assert naive.status_code == 422
    assert aware.status_code == 200


async def test_an_unpriced_wallet_carries_a_null_value_and_a_null_price(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A holding nothing could price has no value, and `null` is how that is said.

    Not `"0"`, and not the quantity. A zero would be a claim that the holding is worth
    nothing, which is a statement about the market rather than about the price cache, and it
    is the one number a renderer cannot tell from a real zero. The balance itself is still
    reported, because the chain answered perfectly well -- it is only the valuation that is
    missing, and the two facts are separate.
    """
    wallet = await create_wallet(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    run_id = await insert_run(api_sessionmaker, started_at=THIRD_SEEN)
    await insert_snapshot(
        api_sessionmaker,
        wallet_id=wallet,
        run_id=run_id,
        confirmed=BTC_UNITS,
        observed_at=THIRD_SEEN,
    )

    payload = (await signed_in_api_client.get(CURRENT)).json()

    line = payload["wallets"][0]
    assert line["confirmed"] == str(BTC_UNITS)
    assert Decimal(line["quantity"]) == BTC_QUANTITY
    assert line["value"] is None
    assert line["price"] is None
    assert payload["complete"] is False
    assert Decimal(payload["total"]) == 0
