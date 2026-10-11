"""Spec 044 over HTTP, the whole stack: portfolios, scoped reads, and moving between them.

The scenario is `test_portfolio_summary`'s: 0.4 BTC at 60000 and 6000 KAS at 0.1, both wallets
in the owner's `Main`. A test that needs a second portfolio creates `Trading` and moves the
Kaspa wallet there, so `Main` is worth 24000 and `Trading` 600.

Every address is a testnet one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from portfolio.domain.chains import ChainKey
from tests.address_vectors import BIP173_TESTNET_P2WPKH, BIP173_TESTNET_P2WSH, KASPA_TESTNET_V0
from tests.api.test_exchange_operations import BITGET_FILLS, TANGEM_SWAP, upload_body
from tests.api.test_portfolio_history import HISTORY, plant_two_days
from tests.api.test_portfolio_history import application as history_application
from tests.api.test_portfolio_summary import application, dec, plant_the_scenario
from tests.auth.conftest import BASE_URL

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

PORTFOLIOS: Final = "/api/portfolios"
WALLETS: Final = "/api/wallets"
OPERATIONS: Final = "/api/exchange-operations"
MOVE: Final = "/api/exchange-operations/move"
JSON: Final = {"Content-Type": "application/json"}

SCOPED_READS: Final = (
    "/api/portfolio/summary",
    "/api/portfolio/changes",
    "/api/portfolio/history",
    "/api/investment",
    "/api/exchange-operations",
    "/api/wallets",
    "/api/balances/current",
)


async def portfolios(client: AsyncClient) -> list[dict[str, Any]]:
    response = await client.get(PORTFOLIOS)
    assert response.status_code == 200, response.text
    listed: list[dict[str, Any]] = response.json()["portfolios"]
    return listed


async def create(client: AsyncClient, name: str) -> dict[str, Any]:
    response = await client.post(PORTFOLIOS, json={"name": name})
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def ok(client: AsyncClient, path: str) -> dict[str, Any]:
    response = await client.get(path)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def wallet_ids(app: FastAPI) -> dict[str, int]:
    """The scenario's wallets by chain."""
    async with app.state.db_sessionmaker() as session:
        rows = await session.execute(text("SELECT chain_key, id FROM wallets"))
        return {str(chain): int(wallet) for chain, wallet in rows.all()}


async def split(app: FastAPI, client: AsyncClient) -> tuple[int, int]:
    """Plant the scenario, create `Trading`, and move the Kaspa wallet there."""
    await plant_the_scenario(app)
    (main,) = await portfolios(client)
    trading = await create(client, "Trading")
    kaspa = (await wallet_ids(app))[ChainKey.KASPA.value]
    moved = await client.patch(f"{WALLETS}/{kaspa}", json={"portfolio_id": trading["id"]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["portfolio_id"] == trading["id"]
    return int(main["id"]), int(trading["id"])


# --------------------------------------------------------------------------------------
# Authentication (criterion 7)
# --------------------------------------------------------------------------------------


async def test_every_new_path_requires_a_session(api_environment: Path) -> None:
    del api_environment
    async with application() as (app, _client):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
            responses = [
                await anonymous.get(PORTFOLIOS),
                await anonymous.post(PORTFOLIOS, json={"name": "x"}),
                await anonymous.patch(f"{PORTFOLIOS}/1", json={"name": "x"}),
                await anonymous.delete(f"{PORTFOLIOS}/1", headers=JSON),
                await anonymous.post(MOVE, json={"portfolio_id": 1}),
                await anonymous.get("/api/portfolio/summary?portfolio=1"),
            ]

    assert [response.status_code for response in responses] == [401] * 6


# --------------------------------------------------------------------------------------
# The registry (criterion 2)
# --------------------------------------------------------------------------------------


async def test_the_first_write_files_in_a_main_made_for_it(api_environment: Path) -> None:
    """R8: an account with no portfolio gets `Main` from its first wallet."""
    del api_environment
    async with application() as (_app, client):
        before = await portfolios(client)
        response = await client.post(
            WALLETS, json={"chain_key": "bitcoin", "address": BIP173_TESTNET_P2WPKH}
        )
        after = await portfolios(client)

    assert before == []
    assert response.status_code == 201, response.text
    assert [(entry["name"], entry["wallets"], entry["operations"]) for entry in after] == [
        ("Main", 1, 0)
    ]
    assert response.json()["portfolio_id"] == after[0]["id"]


async def test_a_portfolio_is_created_renamed_and_listed_oldest_first(
    api_environment: Path,
) -> None:
    del api_environment
    async with application() as (app, client):
        await plant_the_scenario(app)
        trading = await create(client, "  Trading  ")
        duplicate = await client.post(PORTFOLIOS, json={"name": "Trading"})
        blank = await client.post(PORTFOLIOS, json={"name": "   "})
        renamed = await client.patch(f"{PORTFOLIOS}/{trading['id']}", json={"name": "Retirement"})
        same = await client.patch(f"{PORTFOLIOS}/{trading['id']}", json={"name": "Retirement"})
        taken = await client.patch(f"{PORTFOLIOS}/{trading['id']}", json={"name": "Main"})
        missing = await client.patch(f"{PORTFOLIOS}/999", json={"name": "Other"})
        listed = await portfolios(client)

    assert trading["name"] == "Trading"
    assert duplicate.status_code == 409
    assert blank.status_code == 422
    assert renamed.status_code == 200
    assert same.status_code == 200
    assert taken.status_code == 409
    assert missing.status_code == 404
    assert [(entry["name"], entry["wallets"]) for entry in listed] == [
        ("Main", 2),
        ("Retirement", 0),
    ]


async def test_only_an_empty_portfolio_that_is_not_the_last_is_deleted(
    api_environment: Path,
) -> None:
    """R10, each refusal saying which."""
    del api_environment
    async with application() as (app, client):
        await plant_the_scenario(app)
        (main,) = await portfolios(client)
        last = await client.delete(f"{PORTFOLIOS}/{main['id']}", headers=JSON)
        spare = await create(client, "Spare")
        not_empty = await client.delete(f"{PORTFOLIOS}/{main['id']}", headers=JSON)
        deleted = await client.delete(f"{PORTFOLIOS}/{spare['id']}", headers=JSON)
        again = await client.delete(f"{PORTFOLIOS}/{spare['id']}", headers=JSON)
        listed = await portfolios(client)

    assert last.status_code == 409
    assert "only portfolio" in last.json()["detail"]
    assert not_empty.status_code == 409
    assert "wallets or operations" in not_empty.json()["detail"]
    assert deleted.status_code == 204
    assert again.status_code == 404
    assert [entry["name"] for entry in listed] == ["Main"]


# --------------------------------------------------------------------------------------
# Scoped reads (criterion 3) and moving a wallet (criterion 4)
# --------------------------------------------------------------------------------------


async def test_every_scoped_read_refuses_a_portfolio_that_is_not_the_callers(
    api_environment: Path,
) -> None:
    del api_environment
    async with application() as (app, client):
        await plant_the_scenario(app)
        responses = [await client.get(f"{path}?portfolio=999") for path in SCOPED_READS]

    assert [response.status_code for response in responses] == [404] * len(SCOPED_READS)


async def test_each_portfolio_has_its_own_figures_and_all_is_their_sum(
    api_environment: Path,
) -> None:
    del api_environment
    async with application() as (app, client):
        main, trading = await split(app, client)
        everything = await ok(client, "/api/portfolio/summary")
        in_main = await ok(client, f"/api/portfolio/summary?portfolio={main}")
        in_trading = await ok(client, f"/api/portfolio/summary?portfolio={trading}")
        main_wallets = await ok(client, f"{WALLETS}?portfolio={main}")
        trading_balances = await ok(
            client, f"/api/balances/current?quote_currency=USD&portfolio={trading}"
        )

    assert dec(everything["total_value"]) == dec("24600")
    assert dec(in_main["total_value"]) == dec("24000")
    assert [holding["asset"] for holding in in_main["holdings"]] == ["BTC"]
    assert dec(in_trading["total_value"]) == dec("600")
    assert [wallet["chain_key"] for wallet in main_wallets["wallets"]] == ["bitcoin"]
    assert [wallet["chain_key"] for wallet in trading_balances["wallets"]] == ["kaspa"]


async def test_a_moved_wallet_takes_its_history_with_it(api_environment: Path) -> None:
    """R6: the whole series follows the wallet; the portfolio it left keeps none of it."""
    del api_environment
    async with history_application() as (app, client):
        btc = await plant_two_days(app)
        (main,) = await portfolios(client)
        before = await ok(client, f"{HISTORY}?range=30d&portfolio={main['id']}")
        trading = await create(client, "Trading")
        moved = await client.patch(f"{WALLETS}/{btc}", json={"portfolio_id": trading["id"]})
        everything = await ok(client, f"{HISTORY}?range=30d")
        in_trading = await ok(client, f"{HISTORY}?range=30d&portfolio={trading['id']}")
        in_main = await ok(client, f"{HISTORY}?range=30d&portfolio={main['id']}")

    assert moved.status_code == 200, moved.text
    assert before["points"] == everything["points"]
    assert in_trading["points"] == everything["points"]
    assert dec(in_trading["points"][-1]["value"]) == dec("24400")
    assert in_main["points"]
    assert all(point["assets"] == {} for point in in_main["points"])


async def test_a_wallet_cannot_be_filed_in_or_moved_to_another_owners_portfolio(
    api_environment: Path,
) -> None:
    del api_environment
    async with application() as (app, client):
        await plant_the_scenario(app)
        kaspa = (await wallet_ids(app))[ChainKey.KASPA.value]
        created = await client.post(
            WALLETS,
            json={"chain_key": "bitcoin", "address": BIP173_TESTNET_P2WSH, "portfolio_id": 999},
        )
        moved = await client.patch(f"{WALLETS}/{kaspa}", json={"portfolio_id": 999})

    assert created.status_code == 404
    assert moved.status_code == 404


async def test_an_address_another_portfolio_holds_is_the_same_conflict(
    api_environment: Path,
) -> None:
    """R2."""
    del api_environment
    async with application() as (app, client):
        _main, trading = await split(app, client)
        again = await client.post(
            WALLETS,
            json={"chain_key": "kaspa", "address": KASPA_TESTNET_V0, "portfolio_id": trading},
        )

    assert again.status_code == 409


# --------------------------------------------------------------------------------------
# Operations per portfolio (criteria 4 and 5)
# --------------------------------------------------------------------------------------


async def test_operations_are_filed_listed_and_figured_per_portfolio(
    api_environment: Path,
) -> None:
    """R3, R5 and R8: an upload with no portfolio goes to the first, an entry by hand to the
    one named, and each portfolio's invested figure is its own operations' against its own
    wallets."""
    del api_environment
    async with application() as (app, client):
        main, trading = await split(app, client)
        uploaded = await client.post(
            f"{OPERATIONS}/imports", json=upload_body("fills.csv", BITGET_FILLS.encode())
        )
        again = await client.post(
            f"{OPERATIONS}/imports",
            json={**upload_body("fills.csv", BITGET_FILLS.encode()), "portfolio_id": trading},
        )
        manual = await client.post(OPERATIONS, json={**TANGEM_SWAP, "portfolio_id": trading})
        in_main = await ok(client, f"{OPERATIONS}?portfolio={main}")
        in_trading = await ok(client, f"{OPERATIONS}?portfolio={trading}")
        everything = await ok(client, OPERATIONS)
        invested_main = await ok(client, f"/api/investment?portfolio={main}")
        invested_trading = await ok(client, f"/api/investment?portfolio={trading}")
        listed = await portfolios(client)

    assert uploaded.status_code == 201
    assert uploaded.json()["portfolio_id"] == main
    # Already stored in Main: not stored a second time in Trading.
    assert (again.json()["stored"], again.json()["already_stored"]) == (0, 2)
    assert manual.json()["portfolio_id"] == trading
    assert (in_main["count"], in_trading["count"], everything["count"]) == (2, 1, 3)
    assert in_main["assets"] == ["BTC"]
    assert in_trading["venues"] == ["Tangem"]
    assert [asset["asset"] for asset in invested_main["assets"]] == ["BTC"]
    assert dec(invested_main["overall"]["invested"]) == dec("13007")
    assert [asset["asset"] for asset in invested_trading["assets"]] == ["KAS"]
    assert dec(invested_trading["overall"]["invested"]) == dec("500")
    assert [(entry["wallets"], entry["operations"]) for entry in listed] == [(1, 2), (1, 1)]


async def test_a_filter_moves_its_operations_and_nothing_else(api_environment: Path) -> None:
    """R7."""
    del api_environment
    async with application() as (app, client):
        main, trading = await split(app, client)
        await client.post(
            f"{OPERATIONS}/imports", json=upload_body("fills.csv", BITGET_FILLS.encode())
        )
        await client.post(OPERATIONS, json=TANGEM_SWAP)
        moved = await client.post(
            MOVE,
            json={
                "portfolio_id": trading,
                "from_portfolio": main,
                "venue": "Bitget",
                "since": "2026-05-03T00:00:00Z",
            },
        )
        twice = await client.post(
            MOVE, json={"portfolio_id": trading, "venue": "Bitget", "since": "2026-05-03T00:00:00Z"}
        )
        unknown = await client.post(MOVE, json={"portfolio_id": 999})
        inverted = await client.post(
            MOVE,
            json={
                "portfolio_id": trading,
                "since": "2026-05-03T00:00:00Z",
                "until": "2026-05-02T00:00:00Z",
            },
        )
        in_trading = await ok(client, f"{OPERATIONS}?portfolio={trading}")

    assert moved.json() == {"moved": 1}
    assert twice.json() == {"moved": 0}
    assert unknown.status_code == 404
    assert inverted.status_code == 422
    assert [row["kind"] for row in in_trading["operations"]] == ["sell"]
