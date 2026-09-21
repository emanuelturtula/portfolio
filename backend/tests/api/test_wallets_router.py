"""Criteria 1, 3, 4 and 9: the four routes, over a real database and a real session.

These go through the whole stack -- middleware, router, service, repository, SQLite --
because the things being asserted are contracts between those layers: that a `DELETE`
leaves a row behind, that a duplicate is refused whatever its archived state, and that a
422 never carries the address back to the client.

Two assertions here deliberately read the **table** rather than the API. A router test that
only ever reads itself back cannot tell a soft archive from a hard delete: both answer
`204`, and both make the wallet vanish from `GET /api/wallets`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import text

from portfolio.api import dependencies
from portfolio.api.errors import PROBLEM_CONTENT_TYPE
from portfolio.services.wallets import WalletService, build_wallet_service
from tests.address_vectors import (
    BIP173_MIXED_CASE,
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WPKH_UPPERCASE,
    BIP173_UNKNOWN_HRP,
    BIP350_TESTNET_V1,
    BIP350_V0_WITH_BECH32M,
    CORE_REGTEST_P2WPKH,
    CORE_SIGNET_P2PKH,
    CORE_TESTNET4_P2SH,
    CORE_UNKNOWN_VERSION_BYTE,
    DERIVED_V1_WITH_BECH32,
    HOMOGLYPH_KELVIN,
    KASPA_NAMED_CORRUPTIONS,
    KASPA_TESTNET_V0,
    KASPA_TESTNET_V1_KEY,
    NAMED_CORRUPTIONS,
    SYNTHETIC_TPUB,
    TWO_HUNDRED_CHARACTERS,
)
from tests.auth.conftest import JSON_HEADERS

if TYPE_CHECKING:
    from httpx import AsyncClient, Response
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

WALLETS: Final = "/api/wallets"
BITCOIN: Final = "bitcoin"
KASPA: Final = "kaspa"

WALLET_FIELDS: Final = {
    "id",
    "chain_key",
    "address",
    "label",
    "archived",
    "created_at",
    "updated_at",
}


async def create(
    client: AsyncClient,
    address: str = BIP173_TESTNET_P2WPKH,
    *,
    chain_key: str = BITCOIN,
    label: str | None = "Cold storage",
) -> Response:
    body: dict[str, Any] = {"chain_key": chain_key, "address": address}
    if label is not None:
        body["label"] = label
    return await client.post(WALLETS, json=body, headers=JSON_HEADERS)


async def create_ok(
    client: AsyncClient, address: str = BIP173_TESTNET_P2WPKH, **kwargs: Any
) -> dict[str, Any]:
    """Create a wallet and fail the test loudly if the server refused it."""
    response = await create(client, address, **kwargs)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def listed(client: AsyncClient, *, include_archived: bool = False) -> list[dict[str, Any]]:
    params = {"include_archived": "true"} if include_archived else None
    response = await client.get(WALLETS, params=params)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"wallets"}
    wallets: list[dict[str, Any]] = payload["wallets"]
    return wallets


async def rows(sessionmaker: async_sessionmaker[AsyncSession]) -> list[tuple[Any, ...]]:
    """Every wallet row as SQLite has it, read outside the request that wrote it."""
    session: AsyncSession
    async with sessionmaker() as session:
        result = await session.execute(
            text(
                "SELECT id, chain_key, address_canonical, address_display, label, archived_at "
                "FROM wallets ORDER BY id"
            )
        )
        return [tuple(row) for row in result.all()]


# --------------------------------------------------------------------------------------
# Criterion 1: the four routes
# --------------------------------------------------------------------------------------


async def test_create_list_patch_archive_round_trip(signed_in_api_client: AsyncClient) -> None:
    """Criterion 1: every route, in the order a user actually meets them."""
    created = await create_ok(signed_in_api_client, BIP173_TESTNET_P2WPKH, label="Cold storage")

    assert set(created) == WALLET_FIELDS
    assert created["chain_key"] == BITCOIN
    assert created["address"] == BIP173_TESTNET_P2WPKH
    assert created["label"] == "Cold storage"
    assert created["archived"] is False
    assert datetime.fromisoformat(created["created_at"]).tzinfo is not None
    assert datetime.fromisoformat(created["updated_at"]).tzinfo is not None

    assert await listed(signed_in_api_client) == [created]

    patched = await signed_in_api_client.patch(
        f"{WALLETS}/{created['id']}",
        json={"label": "Hardware wallet"},
        headers=JSON_HEADERS,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["label"] == "Hardware wallet"
    assert patched.json()["id"] == created["id"]
    assert patched.json()["address"] == BIP173_TESTNET_P2WPKH

    archived = await signed_in_api_client.delete(f"{WALLETS}/{created['id']}", headers=JSON_HEADERS)
    assert archived.status_code == 204
    assert archived.content == b""

    assert await listed(signed_in_api_client) == []
    still_there = await listed(signed_in_api_client, include_archived=True)
    assert [wallet["id"] for wallet in still_there] == [created["id"]]
    assert still_there[0]["archived"] is True


async def test_delete_soft_archives_the_row(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 1 and criterion 6: `DELETE` sets a timestamp, it does not remove a row.

    Asserted against the **table**, not against the API. `GET /api/wallets` hides an
    archived wallet by design, so a hard delete and a soft archive are indistinguishable
    from outside -- which is exactly why a test that only reads the API back would stay
    green if `archive_wallet` were replaced with a `DELETE FROM wallets`.
    """
    created = await create_ok(signed_in_api_client)
    before = await rows(api_sessionmaker)
    assert [row[0] for row in before] == [created["id"]]
    assert before[0][5] is None

    response = await signed_in_api_client.delete(f"{WALLETS}/{created['id']}", headers=JSON_HEADERS)

    assert response.status_code == 204
    after = await rows(api_sessionmaker)
    assert len(after) == 1, "the row was removed; archiving must preserve it"
    assert after[0][0] == created["id"], "the row survived but lost its id"
    assert after[0][5] is not None, "the row survived but was not marked archived"
    assert after[0][2] == before[0][2], "archiving must not rewrite the address"


async def test_delete_twice_returns_204_both_times(signed_in_api_client: AsyncClient) -> None:
    """Idempotent: the end state the caller asked for is the end state they get.

    `404` on the second call would be defensible and is the wrong answer here -- a retry
    after a dropped response is the common case, and it is not an error.
    """
    created = await create_ok(signed_in_api_client)
    path = f"{WALLETS}/{created['id']}"

    first = await signed_in_api_client.delete(path, headers=JSON_HEADERS)
    second = await signed_in_api_client.delete(path, headers=JSON_HEADERS)

    assert (first.status_code, second.status_code) == (204, 204)


@pytest.fixture
def stepping_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the wallet service a clock that never returns the same instant twice.

    The archive tests below assert that a request which changes nothing **writes**
    nothing, by reading the timestamps before and after. With the real clock that
    assertion is only as good as the gap between two `datetime.now` calls: on a fast
    machine, or one whose clock has coarse resolution, a write can land on the same value
    it replaced and "unchanged" then means "changed, indistinguishably".

    That is not hypothetical. Under the mutation that removes the no-op guard, one of
    these tests passed in a batched run and failed when run alone -- the mutant survived
    because two wall-clock reads happened to agree. A test whose verdict depends on how
    fast the host is has no verdict.

    Stepping a whole second per call removes the coincidence entirely: every write moves
    every timestamp, visibly, so "unchanged" can only mean "not written".

    Patched at the composition root rather than inside the service: `build_wallet_service`
    binds `utc_now` as a default argument at definition time, so replacing the module
    attribute would do nothing at all -- quietly, which is the worst way for a test double
    to fail.
    """
    ticks = count()
    base = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

    def clock() -> datetime:
        return base + timedelta(seconds=next(ticks))

    def build(session: AsyncSession) -> WalletService:
        return build_wallet_service(session, clock=clock)

    monkeypatch.setattr(dependencies, "build_wallet_service", build)


def test_the_stepping_clock_really_steps() -> None:
    """The double's own guard: a clock that returned one value would prove nothing."""
    ticks = count()
    base = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    readings = [base + timedelta(seconds=next(ticks)) for _ in range(3)]

    assert len(set(readings)) == 3
    assert readings[1] - readings[0] == timedelta(seconds=1)


async def archive_columns(
    sessionmaker: async_sessionmaker[AsyncSession], wallet_id: int
) -> tuple[Any, Any]:
    """`(archived_at, updated_at)`, read outside the request that wrote them.

    Both, because they fail independently. An implementation can leave `archived_at`
    alone and still stamp `updated_at` on a request that changed nothing, and #10 will
    order its polling decisions off these columns.
    """
    session: AsyncSession
    async with sessionmaker() as session:
        result = await session.execute(
            text("SELECT archived_at, updated_at FROM wallets WHERE id = :id"),
            {"id": wallet_id},
        )
        archived_at, updated_at = result.one()
        return archived_at, updated_at


async def archived_at_of(sessionmaker: async_sessionmaker[AsyncSession], wallet_id: int) -> Any:
    """Just the archive timestamp, for the assertions that only care about it."""
    return (await archive_columns(sessionmaker, wallet_id))[0]


async def test_delete_twice_leaves_the_archive_timestamp_unchanged(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    stepping_clock: None,
) -> None:
    """Idempotent means the *state* is unchanged, not merely that the status code is 204.

    `test_delete_twice_returns_204_both_times` is satisfied by an implementation that
    re-stamps `archived_at` on every call, which is not idempotent -- it rewrites when the
    wallet was retired, on a request that asked for no change. A retry after a dropped
    response would silently move the date, and once #10 stores balance snapshots that date
    is the boundary the history is read against.
    """
    created = await create_ok(signed_in_api_client)
    path = f"{WALLETS}/{created['id']}"

    await signed_in_api_client.delete(path, headers=JSON_HEADERS)
    first = await archived_at_of(api_sessionmaker, created["id"])
    await signed_in_api_client.delete(path, headers=JSON_HEADERS)
    second = await archived_at_of(api_sessionmaker, created["id"])

    assert first is not None
    assert second == first, "the second DELETE rewrote when the wallet was retired"


async def test_patch_archiving_an_already_archived_wallet_leaves_the_timestamp_unchanged(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    stepping_clock: None,
) -> None:
    """The same rule through the other door, which is the one that had no test.

    `DELETE` and `PATCH {"archived": true}` ask for the same end state, so they have to
    reach the same one. Pinned separately because they are separate code paths: whichever
    of the two was wrong could have been "fixed" to match the other, in either direction,
    without a single test noticing which way it went.
    """
    created = await create_ok(signed_in_api_client)
    await signed_in_api_client.delete(f"{WALLETS}/{created['id']}", headers=JSON_HEADERS)
    before = await archived_at_of(api_sessionmaker, created["id"])

    response = await signed_in_api_client.patch(
        f"{WALLETS}/{created['id']}", json={"archived": True}, headers=JSON_HEADERS
    )

    assert response.status_code == 200, response.text
    assert response.json()["archived"] is True
    after = await archived_at_of(api_sessionmaker, created["id"])
    assert before is not None
    assert after == before, "PATCH re-stamped a wallet that was already archived"


async def test_unarchiving_then_archiving_again_does_move_the_timestamp(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    stepping_clock: None,
) -> None:
    """The other half, without which "never move it" would be an equally passing rule.

    Restoring a wallet and retiring it again is a genuinely new retirement, so the
    timestamp must move. A test suite that only pinned "unchanged" would be satisfied by
    an implementation that stamped `archived_at` once and never again.
    """
    created = await create_ok(signed_in_api_client)
    path = f"{WALLETS}/{created['id']}"
    await signed_in_api_client.delete(path, headers=JSON_HEADERS)
    first = await archived_at_of(api_sessionmaker, created["id"])

    await signed_in_api_client.patch(path, json={"archived": False}, headers=JSON_HEADERS)
    assert await archived_at_of(api_sessionmaker, created["id"]) is None
    await signed_in_api_client.patch(path, json={"archived": True}, headers=JSON_HEADERS)

    assert await archived_at_of(api_sessionmaker, created["id"]) != first


@pytest.mark.parametrize("archived", [True, False], ids=["archive", "unarchive"])
async def test_patch_that_asks_for_the_state_the_wallet_is_already_in_writes_nothing(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    stepping_clock: None,
    archived: bool,
) -> None:
    """All four combinations of the archive setter, not the three that come to mind.

    Archiving an archived wallet is the obvious no-op. **Unarchiving an already-active one
    is the one that hides**, because the wallet is unarchived before and after either way:
    the only visible difference is `updated_at`, which nothing else asserts. An
    unconditional write there would be invisible to every other test in this file.

    `updated_at` is asserted as well as `archived_at` for the same reason -- a request
    that changed nothing must leave no trace, or "when did this row last change" stops
    meaning anything.
    """
    created = await create_ok(signed_in_api_client)
    path = f"{WALLETS}/{created['id']}"
    if archived:
        await signed_in_api_client.delete(path, headers=JSON_HEADERS)
    before = await archive_columns(api_sessionmaker, created["id"])

    response = await signed_in_api_client.patch(
        path, json={"archived": archived}, headers=JSON_HEADERS
    )

    assert response.status_code == 200, response.text
    assert response.json()["archived"] is archived
    assert await archive_columns(api_sessionmaker, created["id"]) == before, (
        "a PATCH asking for the state the wallet was already in wrote to the row"
    )


async def test_a_unicode_homoglyph_address_is_refused(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A string that folds to a valid address but is not one, refused at the boundary.

    It matters here and not only in the domain because `display` is what this endpoint
    hands back. Stored, the owner would be shown a string carrying a Kelvin sign, copy it,
    and find that nothing accepts it.
    """
    response = await create(signed_in_api_client, HOMOGLYPH_KELVIN)

    assert response.status_code == 422, response.text
    assert [error for error in response.json()["errors"] if error["loc"][-1] == "address"]
    assert HOMOGLYPH_KELVIN not in response.text
    assert await rows(api_sessionmaker) == []


async def test_list_excludes_archived_unless_asked(signed_in_api_client: AsyncClient) -> None:
    """Criterion 1: the default hides retired wallets; `?include_archived=true` does not."""
    live = await create_ok(signed_in_api_client, BIP173_TESTNET_P2WPKH, label="Live")
    retired = await create_ok(signed_in_api_client, BIP350_TESTNET_V1, label="Retired")
    await signed_in_api_client.delete(f"{WALLETS}/{retired['id']}", headers=JSON_HEADERS)

    default = await listed(signed_in_api_client)
    everything = await listed(signed_in_api_client, include_archived=True)

    assert [wallet["id"] for wallet in default] == [live["id"]]
    assert {wallet["id"] for wallet in everything} == {live["id"], retired["id"]}
    assert {wallet["id"]: wallet["archived"] for wallet in everything} == {
        live["id"]: False,
        retired["id"]: True,
    }


async def test_patch_can_unarchive_a_wallet(signed_in_api_client: AsyncClient) -> None:
    """Un-archiving is an explicit act on a row the user can see, not a re-add."""
    created = await create_ok(signed_in_api_client)
    await signed_in_api_client.delete(f"{WALLETS}/{created['id']}", headers=JSON_HEADERS)

    response = await signed_in_api_client.patch(
        f"{WALLETS}/{created['id']}",
        json={"archived": False},
        headers=JSON_HEADERS,
    )

    assert response.status_code == 200, response.text
    assert response.json()["archived"] is False
    assert [wallet["id"] for wallet in await listed(signed_in_api_client)] == [created["id"]]


async def test_patch_leaves_the_label_alone_when_it_is_omitted(
    signed_in_api_client: AsyncClient,
) -> None:
    """An omitted field and a null field are different requests.

    Without that distinction, archiving a wallet from a UI that sends only `{"archived":
    true}` would silently erase the label the user typed.
    """
    created = await create_ok(signed_in_api_client, label="Cold storage")

    response = await signed_in_api_client.patch(
        f"{WALLETS}/{created['id']}", json={"archived": True}, headers=JSON_HEADERS
    )

    assert response.status_code == 200, response.text
    assert response.json()["label"] == "Cold storage"


async def test_patch_with_a_null_label_clears_it(signed_in_api_client: AsyncClient) -> None:
    """The other half of the same distinction: an explicit null does erase it."""
    created = await create_ok(signed_in_api_client, label="Cold storage")

    response = await signed_in_api_client.patch(
        f"{WALLETS}/{created['id']}", json={"label": None}, headers=JSON_HEADERS
    )

    assert response.status_code == 200, response.text
    assert response.json()["label"] is None


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_an_unknown_wallet_id_is_404(signed_in_api_client: AsyncClient, method: str) -> None:
    """A wallet that never existed is not a wallet that was archived."""
    response = await signed_in_api_client.request(
        method, f"{WALLETS}/999999", json={"label": "nope"}, headers=JSON_HEADERS
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert response.json()["status"] == 404


# --------------------------------------------------------------------------------------
# Criterion 2, seen from the API
# --------------------------------------------------------------------------------------


async def test_the_response_carries_the_display_form_not_the_canonical_one(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 2: the user sees what they typed; the constraint sees one spelling.

    The uppercase rendering is the only input for which the two forms differ, so it is the
    only input that can tell them apart. Publishing both would invite a client to pick the
    wrong one, so the canonical form must not appear in the body at all.
    """
    created = await create_ok(signed_in_api_client, BIP173_TESTNET_P2WPKH_UPPERCASE)

    assert created["address"] == BIP173_TESTNET_P2WPKH_UPPERCASE

    stored = await rows(api_sessionmaker)
    assert stored[0][2] == BIP173_TESTNET_P2WPKH, "canonical must be the lower-case form"
    assert stored[0][3] == BIP173_TESTNET_P2WPKH_UPPERCASE, "display must be as typed"

    body = json.dumps(created)
    assert BIP173_TESTNET_P2WPKH not in body, "the canonical form is not part of the contract"
    assert "canonical" not in body


@pytest.mark.parametrize(
    "address",
    [
        BIP173_TESTNET_P2WPKH,
        BIP350_TESTNET_V1,
        CORE_REGTEST_P2WPKH,
        CORE_SIGNET_P2PKH,
        CORE_TESTNET4_P2SH,
    ],
    ids=["tb1 v0", "tb1 v1", "bcrt1 v0", "base58 p2pkh", "base58 p2sh"],
)
async def test_every_accepted_form_survives_a_round_trip(
    signed_in_api_client: AsyncClient,
    address: str,
) -> None:
    """Criterion 2: each address form the codecs accept comes back out unaltered."""
    created = await create_ok(signed_in_api_client, address, label=None)

    assert created["address"] == address
    assert created["label"] is None
    assert [wallet["address"] for wallet in await listed(signed_in_api_client)] == [address]


# --------------------------------------------------------------------------------------
# Criterion 3: a malformed address is a field-level 422 that says nothing
# --------------------------------------------------------------------------------------

MALFORMED: Final[tuple[tuple[str, str], ...]] = (
    ("single character typo", NAMED_CORRUPTIONS[0][2]),
    ("base58 single character typo", NAMED_CORRUPTIONS[6][2]),
    ("v0 with a bech32m checksum", BIP350_V0_WITH_BECH32M),
    ("v1 with a bech32 checksum", DERIVED_V1_WITH_BECH32),
    ("mixed case", BIP173_MIXED_CASE),
    ("unknown human readable part", BIP173_UNKNOWN_HRP),
    ("unknown base58 version byte", CORE_UNKNOWN_VERSION_BYTE),
    ("an extended public key", SYNTHETIC_TPUB),
    ("empty", ""),
    ("whitespace", "   "),
    ("two hundred characters", TWO_HUNDRED_CHARACTERS),
)


@pytest.mark.parametrize(
    "address", [address for _name, address in MALFORMED], ids=[name for name, _ in MALFORMED]
)
async def test_malformed_address_returns_422_with_field_error(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    address: str,
) -> None:
    """Criterion 3: every malformed shape is a 422 problem document with an `errors` array.

    The shape is the one a Pydantic failure already produces, so the frontend needs one
    code path whether the rule that refused the input lived in the schema or in the domain.
    """
    response = await create(signed_in_api_client, address)

    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)

    body = response.json()
    assert body["status"] == 422
    assert body["title"]
    assert body["instance"] == WALLETS
    errors = body["errors"]
    assert isinstance(errors, list)
    assert errors, "a field-level error is the whole point of the 422"
    located = [error for error in errors if error["loc"][-1] == "address"]
    assert located, f"no error pointed at the address field: {errors}"
    for error in errors:
        assert set(error) >= {"loc", "msg", "type"}
        assert error["msg"], "an error with no message cannot be shown to anyone"

    assert await rows(api_sessionmaker) == [], "a refused address must not be stored"


@pytest.mark.parametrize(
    "address", [address for _name, address in MALFORMED], ids=[name for name, _ in MALFORMED]
)
async def test_validation_error_does_not_contain_the_address(
    signed_in_api_client: AsyncClient,
    address: str,
) -> None:
    """Criterion 3 and criterion 7's other half.

    A 422 is the one response that carries user input back, and from there into whatever
    the browser logs. So the *whole body* is searched, not just the `msg`: the address must
    not reappear in `detail`, in `instance`, in a `loc`, or in an `input` echo -- Pydantic
    v2 adds exactly such an echo by default, which is the accident this test is aimed at.
    """
    response = await create(signed_in_api_client, address)
    assert response.status_code == 422

    body = response.text

    if not address.strip():
        # The empty string and a run of spaces are substrings of every response ever
        # written, so a containment assertion on them says nothing. What is worth
        # asserting is that the field was still named -- the refusal has to be legible
        # even when there is nothing to quote.
        assert [error for error in response.json()["errors"] if error["loc"][-1] == "address"]
        return

    assert address not in body
    if len(address) > 12:
        # A prefix is as good as the whole string to anyone reading it out of a log.
        assert address[:12] not in body
        assert address[-12:] not in body


async def test_a_kaspa_address_round_trips_with_its_prefix(
    signed_in_api_client: AsyncClient,
) -> None:
    """The second chain, end to end. The `kaspatest:` prefix is part of the address."""
    created = await create_ok(
        signed_in_api_client, KASPA_TESTNET_V1_KEY, chain_key=KASPA, label="Kaspa"
    )

    assert created["chain_key"] == KASPA
    assert created["address"] == KASPA_TESTNET_V1_KEY
    assert created["address"].startswith("kaspatest:")


@pytest.mark.parametrize(
    "address",
    [KASPA_NAMED_CORRUPTIONS[0][2], KASPA_NAMED_CORRUPTIONS[2][2]],
    ids=["v0 typo", "v1 typo"],
)
async def test_a_mistyped_kaspa_address_is_422_and_is_not_echoed(
    signed_in_api_client: AsyncClient,
    address: str,
) -> None:
    """The failure this issue exists to prevent, on the chain with no published BIP."""
    response = await create(signed_in_api_client, address, chain_key=KASPA)

    assert response.status_code == 422, response.text
    assert [error for error in response.json()["errors"] if error["loc"][-1] == "address"]
    assert address not in response.text
    assert address[:20] not in response.text


async def test_an_unknown_chain_key_is_422_and_names_the_chain_field(
    signed_in_api_client: AsyncClient,
) -> None:
    """A chain nobody implemented is a refusal, not a 500 from the `CHECK` constraint."""
    response = await create(signed_in_api_client, BIP173_TESTNET_P2WPKH, chain_key="ethereum")

    assert response.status_code == 422, response.text
    errors = response.json()["errors"]
    assert [error for error in errors if error["loc"][-1] == "chain_key"], errors
    assert BIP173_TESTNET_P2WPKH not in response.text


async def test_a_bitcoin_address_is_refused_under_the_kaspa_chain_key(
    signed_in_api_client: AsyncClient,
) -> None:
    """The registry dispatches on the chain key, so the pair has to agree."""
    response = await create(signed_in_api_client, BIP173_TESTNET_P2WPKH, chain_key=KASPA)

    assert response.status_code == 422, response.text
    assert BIP173_TESTNET_P2WPKH not in response.text


# --------------------------------------------------------------------------------------
# Criterion 4: a duplicate is always 409
# --------------------------------------------------------------------------------------


async def test_duplicate_address_returns_409(signed_in_api_client: AsyncClient) -> None:
    """Criterion 4: the second `POST` of the same address is a conflict."""
    first = await create_ok(signed_in_api_client, BIP173_TESTNET_P2WPKH)

    response = await create(signed_in_api_client, BIP173_TESTNET_P2WPKH, label="again")

    assert response.status_code == 409, response.text
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert response.json()["status"] == 409
    assert [wallet["id"] for wallet in await listed(signed_in_api_client)] == [first["id"]]
    assert BIP173_TESTNET_P2WPKH not in response.text


async def test_a_duplicate_in_a_different_case_is_still_a_duplicate(
    signed_in_api_client: AsyncClient,
) -> None:
    """The reason uniqueness is on the canonical column and not on the display one."""
    await create_ok(signed_in_api_client, BIP173_TESTNET_P2WPKH)

    response = await create(signed_in_api_client, BIP173_TESTNET_P2WPKH_UPPERCASE)

    assert response.status_code == 409, response.text


async def test_duplicate_of_archived_address_returns_409(
    signed_in_api_client: AsyncClient,
) -> None:
    """Criterion 4's interpretation: an archived row still occupies its slot.

    The alternative -- a partial constraint over unarchived rows -- would let this `POST`
    silently resurrect the retired wallet, with its old label and, once #10 lands, its old
    balance history, while looking to the user like a brand new one. The problem document
    says which case this is so the UI can offer to un-archive.
    """
    created = await create_ok(signed_in_api_client, BIP173_TESTNET_P2WPKH, label="Retired")
    await signed_in_api_client.delete(f"{WALLETS}/{created['id']}", headers=JSON_HEADERS)

    response = await create(signed_in_api_client, BIP173_TESTNET_P2WPKH, label="New")

    assert response.status_code == 409, response.text
    detail = response.json().get("detail", "")
    assert "archiv" in detail.lower(), f"the 409 must say the existing row is archived: {detail}"

    everything = await listed(signed_in_api_client, include_archived=True)
    assert [wallet["id"] for wallet in everything] == [created["id"]]
    assert everything[0]["label"] == "Retired", "the archived row must not have been rewritten"


async def test_the_409_for_a_live_row_and_an_archived_row_are_told_apart(
    signed_in_api_client: AsyncClient,
) -> None:
    """Both are 409; the detail differs, because the useful next action differs."""
    live = await create_ok(signed_in_api_client, BIP173_TESTNET_P2WPKH)
    live_conflict = await create(signed_in_api_client, BIP173_TESTNET_P2WPKH)

    retired = await create_ok(signed_in_api_client, BIP350_TESTNET_V1)
    await signed_in_api_client.delete(f"{WALLETS}/{retired['id']}", headers=JSON_HEADERS)
    archived_conflict = await create(signed_in_api_client, BIP350_TESTNET_V1)

    assert live["id"] != retired["id"]
    assert live_conflict.status_code == archived_conflict.status_code == 409
    assert live_conflict.json()["detail"] != archived_conflict.json()["detail"]


async def test_same_address_on_another_chain_is_accepted(
    signed_in_api_client: AsyncClient,
) -> None:
    """Criterion 4: uniqueness is scoped by chain, so a second chain does not collide.

    The literal case -- one identical string registered under two chain keys -- is not
    reachable through this API, and deliberately so: no string is a valid Bitcoin address
    *and* a valid Kaspa address, so the validator refuses the pair before the constraint is
    ever consulted. `tests/db/test_wallets_repository.py::
    test_the_same_address_is_allowed_on_another_chain` covers the constraint's own
    behaviour by inserting the identical string under both keys.

    What is reachable, and what this asserts, is the consequence that matters: registering
    a wallet on a second chain is not refused because a wallet already exists on the first.
    """
    first = await create_ok(signed_in_api_client, BIP173_TESTNET_P2WPKH, chain_key=BITCOIN)
    second = await create_ok(signed_in_api_client, KASPA_TESTNET_V0, chain_key=KASPA)

    wallets = await listed(signed_in_api_client)

    assert {wallet["id"] for wallet in wallets} == {first["id"], second["id"]}
    assert {wallet["chain_key"] for wallet in wallets} == {BITCOIN, KASPA}


# --------------------------------------------------------------------------------------
# Criterion 9: none of this is reachable without a session
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", WALLETS),
        ("POST", WALLETS),
        ("PATCH", f"{WALLETS}/1"),
        ("DELETE", f"{WALLETS}/1"),
    ],
)
async def test_the_wallet_routes_require_a_session(
    api_client: AsyncClient,
    method: str,
    path: str,
) -> None:
    """Criterion 9, named explicitly here as well as walked in the contract test.

    `tests/auth/test_route_contract.py` walks every registered route and would catch this
    on its own. It is repeated because a reader looking for "are wallets authenticated"
    should find the answer in the wallets test file, and because a route that vanished
    from the walk would pass there and fail here.
    """
    headers = {} if method == "GET" else JSON_HEADERS
    response = await api_client.request(method, path, json={}, headers=headers)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)


async def test_an_unauthenticated_post_writes_nothing(
    api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A 401 that had already inserted the row would be the worst of both answers."""
    response = await create(api_client, BIP173_TESTNET_P2WPKH)

    assert response.status_code == 401
    assert await rows(api_sessionmaker) == []
