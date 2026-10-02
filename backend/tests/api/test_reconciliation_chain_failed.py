"""Spec 028 (#116) over HTTP: a wallet whose chain failed, through the endpoints the owner uses.

The whole stack runs, and **both syncs are the real ones**. `POST /api/balances/sync` runs the
real balance sync, which opens the run, reads each chain, and writes `sync_runs`,
`sync_run_chains` and `balance_snapshots` itself; `POST /api/exchanges/sync` runs the real
exchange sync against a simulated venue. Only the bottom of each is replaced: a chain
provider that answers from a dictionary or fails on command (`tests/balance_harness.py`), and
a venue that answers from the fills and balances it holds.

So nothing here plants a run by hand. What is asserted includes that the rows the balance
sync really writes are the rows `GET /api/accounting/reconciliation` reads its verdict from,
which a test over planted rows cannot show: `tests/services/test_reconciliation_chain_failed.py`
plants them, and covers the cases no real sync can be made to produce on demand (a run still
in flight, an interrupted one).

## What is pinned

* **The scenario of the issue, start to finish** (criterion 7): coins read in a wallet, sent
  to a venue, the chain failing on the next balance sync. The asset stays a `match`.
* **The wire shape of `wallets`**: the two new fields, their types, their place before
  `oldest_observed_at`, on the raw text and in the OpenAPI document.
* **`failed_chains` agrees with `GET /api/balances/runs`**, where the owner is sent for the
  reason.
* **The consequences the rule has**: a wallet added or restored after the failed run is left
  out at once, and a chain refused as `address_rejected` failed like any other.
* **`401` without a session**, and no chain is asked by a `GET`.
* **The endpoint's published description** names the fourth count and the chains (ruling R4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from httpx import ASGITransport, AsyncClient

from portfolio.domain.addresses import AddressInvalidError, AddressRejection
from portfolio.domain.chains import ChainKey
from portfolio.domain.exchanges import ExchangeKey
from portfolio.providers.errors import ProviderRateLimitedError, ProviderUnavailableError
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WSH,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V1_KEY,
)
from tests.api.test_accounting import application
from tests.auth.conftest import BASE_URL, JSON_HEADERS
from tests.balance_harness import StubChainProvider, stub_chain_providers
from tests.exchange_sync_harness import SimulatedVenue, held, make_fill

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from fastapi import FastAPI

RECONCILIATION: Final = "/api/accounting/reconciliation"
BALANCE_SYNC: Final = "/api/balances/sync"
BALANCE_RUNS: Final = "/api/balances/runs"
EXCHANGE_SYNC: Final = "/api/exchanges/sync"
WALLETS: Final = "/api/wallets"

BITCOIN: Final = "bitcoin"
KASPA: Final = "kaspa"

#: One thousand KAS in sompi, and 0.4995 BTC in satoshis: what the histories below account for.
THOUSAND_KAS: Final = 1000 * 100_000_000
BTC_HELD: Final = 49_950_000

#: The `wallets` object in the order spec 028 gives it.
WALLETS_ORDER: Final = [
    "compared",
    "stale",
    "unread",
    "chain_failed",
    "failed_chains",
    "oldest_observed_at",
]

NOTHING_LEFT_OUT: Final[dict[str, Any]] = {"chain_failed": 0, "failed_chains": []}


def down() -> ProviderUnavailableError:
    return ProviderUnavailableError("no configured endpoint answered")


def kas_bought(now: datetime) -> SimulatedVenue:
    """A venue whose history is one buy of 1000 KAS, fee paid in the quote, holding nothing."""
    return SimulatedVenue(
        [
            make_fill(
                7001,
                now - timedelta(minutes=30),
                symbol="KASUSDT",
                base_asset="KAS",
                quantity="1000",
                price="0.1",
                quote_quantity="100",
                fee_amount="0.1",
                fee_asset="USDT",
            )
        ],
        balances=(),
    )


async def add_wallet(client: AsyncClient, chain_key: str, address: str) -> int:
    response = await client.post(
        WALLETS, json={"chain_key": chain_key, "address": address}, headers=JSON_HEADERS
    )
    assert response.status_code == 201, response.text
    identifier: int = response.json()["id"]
    return identifier


async def sync_balances(client: AsyncClient, expected_status: str) -> dict[str, Any]:
    """One manual balance sync through the endpoint, and the run summary it answers."""
    response = await client.post(BALANCE_SYNC, headers=JSON_HEADERS)
    assert response.status_code == 200, response.text
    summary: dict[str, Any] = response.json()
    assert summary["status"] == expected_status, summary
    assert summary["joined"] is False
    return summary


async def sync_exchanges(client: AsyncClient) -> None:
    response = await client.post(EXCHANGE_SYNC, headers=JSON_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "success", response.text


async def reconciliation(client: AsyncClient) -> tuple[dict[str, Any], str]:
    response = await client.get(RECONCILIATION)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body, response.text


def by_asset(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["asset"]: entry for entry in body["assets"]}


def left_out(body: dict[str, Any]) -> dict[str, Any]:
    """The two fields of spec 028, apart from the counts beside them."""
    return {key: body["wallets"][key] for key in ("chain_failed", "failed_chains")}


def counts(body: dict[str, Any]) -> tuple[int, int, int, int]:
    wallets = body["wallets"]
    return wallets["compared"], wallets["stale"], wallets["unread"], wallets["chain_failed"]


def failed_in_the_newest_finished_run(runs: list[dict[str, Any]]) -> list[str]:
    """The chains `GET /api/balances/runs` reports failed in the newest run that ended."""
    newest = next(run for run in runs if run["status"] in {"success", "partial", "failed"})
    return sorted(chain["chain_key"] for chain in newest["chains"] if chain["status"] == "failed")


# --------------------------------------------------------------------------------------
# The schema
# --------------------------------------------------------------------------------------


def test_the_schema_declares_the_two_fields_before_the_oldest_reading(app: FastAPI) -> None:
    """Criterion 9, at its source: what `schema.ts` is generated from.

    Both fields are required, so the generated type has neither as optional and a client
    never has to guess what an absent list means. `chain_key` is a plain string, as a
    wallet's is: a chain this build of the frontend has no name for still has a key to show.
    """
    schemas = app.openapi()["components"]["schemas"]
    wallets = schemas["WalletsReadResponse"]
    entry = schemas["FailedChainResponse"]

    assert list(wallets["properties"]) == WALLETS_ORDER
    assert wallets["required"] == WALLETS_ORDER
    assert wallets["properties"]["chain_failed"]["type"] == "integer"
    failed_chains = wallets["properties"]["failed_chains"]
    assert failed_chains["type"] == "array"
    assert failed_chains["items"] == {"$ref": "#/components/schemas/FailedChainResponse"}
    assert "anyOf" not in failed_chains, "a list, never null"
    assert list(entry["properties"]) == ["chain_key", "wallets"]
    assert entry["required"] == ["chain_key", "wallets"]
    assert entry["type"] == "object"
    assert entry["properties"]["chain_key"]["type"] == "string"
    assert "enum" not in entry["properties"]["chain_key"]
    assert "$ref" not in entry["properties"]["chain_key"]
    assert entry["properties"]["wallets"]["type"] == "integer"
    assert "WalletNotComparedReason" not in schemas, (
        "the reason a wallet is left out is counted, not served: it has no wire form"
    )


def test_the_published_description_names_the_fourth_count_and_the_chains(app: FastAPI) -> None:
    """Ruling R4: what `/api/docs` and the generated client's comment say the endpoint serves.

    It listed three counts of wallets. It now says a wallet can be left out for its chain,
    that each such chain is named, by which sync that is judged, and the exception: a later
    sync that has already read the wallet.
    """
    operation = app.openapi()["paths"][RECONCILIATION]["get"]
    description = " ".join(operation["description"].split())

    assert "how many wallets were compared" in description
    assert "how many had a reading too old" in description
    assert "how many were never read" in description
    assert (
        "how many were left out because the latest finished balance sync could not read "
        "their chain, with each such chain named"
    ) in description
    assert "unless a later sync has already read it" in description
    assert "`max_reading_age_hours`" in description
    assert "nothing is read from a chain or a venue here" in description


def test_the_published_schemas_describe_the_rule_with_its_exception(app: FastAPI) -> None:
    """What `schema.ts` carries as the comment on each type: which wallets are compared,
    in the rule's full form, and where the reason for a failure is (rulings R2 and R4)."""
    schemas = app.openapi()["components"]["schemas"]
    wallets = " ".join(schemas["WalletsReadResponse"]["description"].split())
    entry = " ".join(schemas["FailedChainResponse"]["description"].split())

    assert (
        "their chain did not fail in the latest finished balance sync, or a later sync has "
        "read them since"
    ) in wallets
    assert "each under the first reason that applies" in wallets
    assert "`failed_chains` names the chains behind `chain_failed`, sorted by `chain_key`" in (
        wallets
    )
    assert "the entries' `wallets` add up to `chain_failed`" in wallets
    assert "never zero" in entry
    assert "It does not say why the chain failed: `GET /api/balances/runs` does." in entry


# --------------------------------------------------------------------------------------
# Criterion 7: the scenario of the issue, through both real syncs
# --------------------------------------------------------------------------------------


async def test_coins_sent_to_a_venue_while_the_chain_is_down_are_not_counted_twice(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue, start to finish, in four readings of the endpoint.

    1. 1000 KAS bought at Bitget and withdrawn to a wallet. The balance sync reads the
       wallet, the exchange sync reads the venue: 1000 in the history, 1000 held, a match.
    2. The owner deposits the 1000 KAS back. The exchange sync reads them at the venue; the
       balance sync after it cannot read Kaspa. The wallet's last reading still says 1000
       and is minutes old. Summed with the venue's it is 2000 held against 1000:
       `history_short` for 1000 KAS that do not exist. With the wallet left out it is 1000
       against 1000, a match, and the source names Kaspa.
    3. Another failed balance sync changes nothing.
    4. The chain answers again and the wallet holds nothing: compared, and still a match.
    """
    del api_environment
    now = datetime.now(UTC).replace(microsecond=0)
    venue = kas_bought(now)
    kaspa = StubChainProvider(ChainKey.KASPA, {KASPA_TESTNET_V0: THOUSAND_KAS})
    registry = stub_chain_providers(monkeypatch, {ChainKey.KASPA: kaspa})
    async with application(monkeypatch, {ExchangeKey.BITGET: venue}) as (_app, client):
        await add_wallet(client, KASPA, KASPA_TESTNET_V0)
        await sync_balances(client, "success")
        await sync_exchanges(client)
        in_the_wallet, _raw = await reconciliation(client)

        venue.balances = [held("KAS", "1000")]
        await sync_exchanges(client)
        kaspa.raises = down()
        failed = await sync_balances(client, "failed")
        at_the_venue, raw = await reconciliation(client)
        runs = (await client.get(BALANCE_RUNS)).json()["runs"]

        await sync_balances(client, "failed")
        still_failing, _raw = await reconciliation(client)

        stub_chain_providers(
            monkeypatch, {ChainKey.KASPA: StubChainProvider(ChainKey.KASPA, {KASPA_TESTNET_V0: 0})}
        )
        await sync_balances(client, "success")
        recovered, _raw = await reconciliation(client)

    assert registry.created.count(KASPA) == 3, "the stubbed chain was reached by each sync"
    assert len(kaspa.calls) == 3

    assert by_asset(in_the_wallet)["KAS"] == {
        "asset": "KAS",
        "history_quantity": "1000.000000000000000000",
        "wallet_quantity": "1000.000000000000000000",
        "exchange_quantity": "0.000000000000000000",
        "held_quantity": "1000.000000000000000000",
        "difference": "0.000000000000000000",
        "status": "match",
    }
    assert counts(in_the_wallet) == (1, 0, 0, 0)
    assert left_out(in_the_wallet) == NOTHING_LEFT_OUT
    assert in_the_wallet["wallets"]["oldest_observed_at"] is not None

    assert (failed["wallets_succeeded"], failed["wallets_failed"]) == (0, 1)
    assert by_asset(at_the_venue)["KAS"] == {
        "asset": "KAS",
        "history_quantity": "1000.000000000000000000",
        "wallet_quantity": "0.000000000000000000",
        "exchange_quantity": "1000.000000000000000000",
        "held_quantity": "1000.000000000000000000",
        "difference": "0.000000000000000000",
        "status": "match",
    }
    assert "history_short" not in raw
    assert at_the_venue["wallets"] == {
        "compared": 0,
        "stale": 0,
        "unread": 0,
        "chain_failed": 1,
        "failed_chains": [{"chain_key": "kaspa", "wallets": 1}],
        "oldest_observed_at": None,
    }
    assert failed_in_the_newest_finished_run(runs) == [KASPA]
    assert at_the_venue["exchanges"][0]["not_compared_reason"] is None

    assert still_failing["wallets"] == at_the_venue["wallets"]
    assert still_failing["assets"] == at_the_venue["assets"]

    assert by_asset(recovered)["KAS"] == by_asset(at_the_venue)["KAS"]
    assert counts(recovered) == (1, 0, 0, 0)
    assert left_out(recovered) == NOTHING_LEFT_OUT
    assert recovered["wallets"]["oldest_observed_at"] is not None


async def test_the_same_transfer_with_the_chain_read_counts_the_wallet_at_what_it_holds(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control on the scenario: the venue's reading is real, and a wallet that is read is
    summed with it.

    The coins are at the venue and the wallet is read still holding 1000, as it would be if
    the deposit were a second 1000 KAS the history does not know. That is 2000 held against
    1000 and `history_short` is the right answer. So the `match` above is the wallet being
    left out, and not a venue reading that never reaches the comparison.
    """
    del api_environment
    now = datetime.now(UTC).replace(microsecond=0)
    venue = kas_bought(now)
    venue.balances = [held("KAS", "1000")]
    stub_chain_providers(
        monkeypatch,
        {ChainKey.KASPA: StubChainProvider(ChainKey.KASPA, {KASPA_TESTNET_V0: THOUSAND_KAS})},
    )
    async with application(monkeypatch, {ExchangeKey.BITGET: venue}) as (_app, client):
        await add_wallet(client, KASPA, KASPA_TESTNET_V0)
        await sync_balances(client, "success")
        await sync_exchanges(client)
        body, _raw = await reconciliation(client)

    kas = by_asset(body)["KAS"]
    assert kas["wallet_quantity"] == "1000.000000000000000000"
    assert kas["exchange_quantity"] == "1000.000000000000000000"
    assert kas["held_quantity"] == "2000.000000000000000000"
    assert kas["status"] == "history_short"
    assert counts(body) == (1, 0, 0, 0)
    assert left_out(body) == NOTHING_LEFT_OUT


# --------------------------------------------------------------------------------------
# The shape, and what it agrees with
# --------------------------------------------------------------------------------------


async def test_one_chain_failing_leaves_out_its_wallets_and_keeps_the_others(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bitcoin is read and Kaspa is throttled: a `partial` run.

    The Bitcoin wallet is compared on the reading that run wrote. The two Kaspa wallets are
    left out, one with a reading from the run before and one that was never read. The entry
    names the chain the runs endpoint reports failed, and does not carry the reason: the run
    log has it.
    """
    del api_environment
    now = datetime.now(UTC).replace(microsecond=0)
    venue = SimulatedVenue(
        [make_fill(5001, now - timedelta(minutes=10))], balances=(held("KAS", "5"),)
    )
    kaspa = StubChainProvider(ChainKey.KASPA, {KASPA_TESTNET_V0: THOUSAND_KAS})
    stub_chain_providers(
        monkeypatch,
        {
            ChainKey.BITCOIN: StubChainProvider(
                ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_HELD}
            ),
            ChainKey.KASPA: kaspa,
        },
    )
    async with application(monkeypatch, {ExchangeKey.BITGET: venue}) as (_app, client):
        await add_wallet(client, BITCOIN, BIP173_TESTNET_P2WPKH)
        await add_wallet(client, KASPA, KASPA_TESTNET_V0)
        await sync_balances(client, "success")
        await sync_exchanges(client)
        before, _raw = await reconciliation(client)

        await add_wallet(client, KASPA, KASPA_TESTNET_V1_KEY)
        kaspa.raises = ProviderRateLimitedError("the vendor asked us to slow down")
        summary = await sync_balances(client, "partial")
        body, raw = await reconciliation(client)
        runs = (await client.get(BALANCE_RUNS)).json()["runs"]

    assert counts(before) == (2, 0, 0, 0)
    assert by_asset(before)["KAS"]["wallet_quantity"] == "1000.000000000000000000"

    assert (summary["wallets_succeeded"], summary["wallets_failed"]) == (1, 2)
    assert body["wallets"]["compared"] == 1
    assert body["wallets"]["stale"] == 0
    assert body["wallets"]["unread"] == 0, "never read, on a failed chain: chain_failed"
    assert body["wallets"]["chain_failed"] == 2
    assert body["wallets"]["failed_chains"] == [{"chain_key": "kaspa", "wallets": 2}]
    assert sum(counts(body)) == 3
    assert body["wallets"]["oldest_observed_at"] is not None, "the Bitcoin wallet's reading"
    rows = by_asset(body)
    assert rows["BTC"]["wallet_quantity"] == "0.499500000000000000"
    assert rows["BTC"]["status"] == "match"
    assert rows["KAS"]["wallet_quantity"] == "0.000000000000000000"
    assert rows["KAS"]["exchange_quantity"] == "5.000000000000000000"
    assert failed_in_the_newest_finished_run(runs) == [
        entry["chain_key"] for entry in body["wallets"]["failed_chains"]
    ]
    newest_kaspa = next(chain for chain in runs[0]["chains"] if chain["chain_key"] == KASPA)
    assert newest_kaspa["error_kind"] == "rate_limited"
    assert "rate_limited" not in raw, "why the chain failed is the run log's to say"
    assert "slow down" not in raw
    assert KASPA_TESTNET_V0 not in raw, "no address is served by the holdings check"
    assert KASPA_TESTNET_V1_KEY not in raw


async def test_both_chains_failing_are_listed_by_chain_key_in_the_order_of_the_spec(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `wallets` object as text: field order, the entries' own field order, and Bitcoin
    before Kaspa although the Kaspa wallet was registered first."""
    del api_environment
    stub_chain_providers(
        monkeypatch,
        {
            ChainKey.BITCOIN: StubChainProvider(ChainKey.BITCOIN, raises=down()),
            ChainKey.KASPA: StubChainProvider(ChainKey.KASPA, raises=down()),
        },
    )
    async with application(monkeypatch) as (_app, client):
        await add_wallet(client, KASPA, KASPA_TESTNET_V0)
        await add_wallet(client, BITCOIN, BIP173_TESTNET_P2WPKH)
        await add_wallet(client, BITCOIN, BIP173_TESTNET_P2WSH)
        await sync_balances(client, "failed")
        body, raw = await reconciliation(client)

    assert (
        '"wallets":{"compared":0,"stale":0,"unread":0,"chain_failed":3,'
        '"failed_chains":[{"chain_key":"bitcoin","wallets":2},{"chain_key":"kaspa","wallets":1}],'
        '"oldest_observed_at":null}'
    ) in raw
    assert list(body["wallets"]) == WALLETS_ORDER
    assert all(type(entry["wallets"]) is int for entry in body["wallets"]["failed_chains"])
    assert type(body["wallets"]["chain_failed"]) is int
    assert body["assets"] == [], "nothing held is compared, and nothing is invented"


async def test_with_no_chain_failed_the_two_fields_are_zero_and_an_empty_list(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never absent and never `null`: before any sync, and after one that read every chain."""
    del api_environment
    stub_chain_providers(
        monkeypatch,
        {ChainKey.BITCOIN: StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_HELD})},
    )
    async with application(monkeypatch) as (_app, client):
        _body, nothing_yet = await reconciliation(client)
        await add_wallet(client, BITCOIN, BIP173_TESTNET_P2WPKH)
        unread, unread_raw = await reconciliation(client)
        await sync_balances(client, "success")
        read, read_raw = await reconciliation(client)

    for raw in (nothing_yet, unread_raw, read_raw):
        assert '"chain_failed":0,"failed_chains":[],' in raw
    assert counts(unread) == (0, 0, 1, 0), "no run has finished: unread, as before the rule"
    assert counts(read) == (1, 0, 0, 0)


# --------------------------------------------------------------------------------------
# The consequences of the rule
# --------------------------------------------------------------------------------------


async def test_a_wallet_added_or_restored_after_the_failed_run_is_left_out_at_once(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's verdict is on the chain, so it covers a wallet the run never saw.

    Kaspa failed. A Kaspa wallet registered afterwards has no reading and is `chain_failed`,
    not `unread`: reading it needs the chain the last sync could not read. Archiving a
    wallet takes it out of every count, and restoring it puts it back under the same reason.
    """
    del api_environment
    stub_chain_providers(
        monkeypatch, {ChainKey.KASPA: StubChainProvider(ChainKey.KASPA, raises=down())}
    )
    async with application(monkeypatch) as (_app, client):
        first = await add_wallet(client, KASPA, KASPA_TESTNET_V0)
        await sync_balances(client, "failed")
        one, _raw = await reconciliation(client)

        await add_wallet(client, KASPA, KASPA_TESTNET_V1_KEY)
        added, _raw = await reconciliation(client)

        archive = await client.delete(f"{WALLETS}/{first}", headers=JSON_HEADERS)
        assert archive.status_code == 204, archive.text
        archived, _raw = await reconciliation(client)

        restore = await client.patch(
            f"{WALLETS}/{first}", json={"archived": False}, headers=JSON_HEADERS
        )
        assert restore.status_code == 200, restore.text
        restored, _raw = await reconciliation(client)

        added_on_bitcoin = await add_wallet(client, BITCOIN, BIP173_TESTNET_P2WPKH)
        other_chain, _raw = await reconciliation(client)

    assert added_on_bitcoin > first
    assert [counts(body) for body in (one, added, archived, restored)] == [
        (0, 0, 0, 1),
        (0, 0, 0, 2),
        (0, 0, 0, 1),
        (0, 0, 0, 2),
    ]
    assert [body["wallets"]["failed_chains"] for body in (one, added, archived, restored)] == [
        [{"chain_key": "kaspa", "wallets": 1}],
        [{"chain_key": "kaspa", "wallets": 2}],
        [{"chain_key": "kaspa", "wallets": 1}],
        [{"chain_key": "kaspa", "wallets": 2}],
    ]
    assert counts(other_chain) == (0, 0, 1, 2), "Bitcoin has no row in that run: unread"
    assert other_chain["wallets"]["failed_chains"] == [{"chain_key": "kaspa", "wallets": 2}]


async def test_a_chain_refused_for_its_address_failed_like_any_other(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`address_rejected` is the owner's configuration and not a vendor outage, and the chain
    still was not read: nothing says what its wallets hold, so they are left out."""
    del api_environment
    bitcoin = StubChainProvider(ChainKey.BITCOIN, {BIP173_TESTNET_P2WPKH: BTC_HELD})
    stub_chain_providers(monkeypatch, {ChainKey.BITCOIN: bitcoin})
    async with application(monkeypatch) as (_app, client):
        await add_wallet(client, BITCOIN, BIP173_TESTNET_P2WPKH)
        await sync_balances(client, "success")
        read, _raw = await reconciliation(client)

        bitcoin.raises = AddressInvalidError(AddressRejection.WRONG_NETWORK)
        await sync_balances(client, "failed")
        body, _raw = await reconciliation(client)
        runs = (await client.get(BALANCE_RUNS)).json()["runs"]

    assert counts(read) == (1, 0, 0, 0)
    assert runs[0]["chains"][0]["error_kind"] == "address_rejected"
    assert counts(body) == (0, 0, 0, 1)
    assert body["wallets"]["failed_chains"] == [{"chain_key": "bitcoin", "wallets": 1}]
    assert body["wallets"]["oldest_observed_at"] is None


# --------------------------------------------------------------------------------------
# Authentication, and what a read asks
# --------------------------------------------------------------------------------------


async def test_the_failed_chains_are_not_served_without_a_session(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chain failed and a wallet is left out, so a body that leaked would have it to leak."""
    del api_environment
    stub_chain_providers(
        monkeypatch, {ChainKey.KASPA: StubChainProvider(ChainKey.KASPA, raises=down())}
    )
    async with application(monkeypatch) as (app, client):
        await add_wallet(client, KASPA, KASPA_TESTNET_V0)
        await sync_balances(client, "failed")
        signed_in, _raw = await reconciliation(client)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url=BASE_URL) as anonymous:
            response = await anonymous.get(RECONCILIATION)

    assert signed_in["wallets"]["chain_failed"] == 1, "the control: there is something to leak"
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "failed_chains" not in response.text
    assert "chain_failed" not in response.text
    assert "kaspa" not in response.text


async def test_reading_the_reconciliation_asks_no_chain(
    api_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verdict is read from the stored run. A failing chain is not asked again by a
    `GET`, and five requests later the provider has been called once: by the sync."""
    del api_environment
    kaspa = StubChainProvider(ChainKey.KASPA, raises=down())
    registry = stub_chain_providers(monkeypatch, {ChainKey.KASPA: kaspa})
    async with application(monkeypatch) as (_app, client):
        await add_wallet(client, KASPA, KASPA_TESTNET_V0)
        await sync_balances(client, "failed")
        for _ in range(5):
            body, _raw = await reconciliation(client)
        runs = (await client.get(BALANCE_RUNS)).json()["runs"]

    assert body["wallets"]["chain_failed"] == 1
    assert len(kaspa.calls) == 1
    assert registry.created == [KASPA]
    assert len(runs) == 1, "a read opens no run"
