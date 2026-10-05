"""Spec 031 through the HTTP API: an extended public key registered, and never served (R8).

`POST /api/wallets` takes an extended public key in `address` on Bitcoin. Every response
after that -- the create, the list, a patch, the list of archived wallets, a duplicate's
409, and the balance endpoints -- carries `kind` and at most the masked form: the first
four characters, one HORIZONTAL ELLIPSIS, the last four. The database holds the key whole;
that is where the sync reads it from.

**Two forms are stored** (review finding S1): the display form as typed, and a canonical
form re-serialised at depth 0 -- same version, chain code and public key; depth, parent
fingerprint and child number zeroed. Derivation reads only the first three, so two exports
of one account that differ in the other three are one wallet, and a second one is a 409
rather than a doubled balance. The canonical form is computed independently, in
`tests/extended_key_forms.py`, from the serialisation format rather than by calling the
function under test.

Each refusal is the existing field-level 422 with the reason as `type`, so the frontend's
one code path keeps working, and none of them echoes what was sent.

Every key here is a test-network form (R11). The private-key refusals use a prefix and four
characters: the refusal is by prefix, so nothing key-shaped is needed, or written.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest
from sqlalchemy import text

from portfolio.api.errors import PROBLEM_CONTENT_TYPE
from portfolio.domain.addresses import REJECTION_MESSAGES, AddressRejection
from portfolio.services.wallets import (
    DUPLICATE_DETAIL,
    DUPLICATE_KEY_ARCHIVED_DETAIL,
    DUPLICATE_KEY_DETAIL,
)
from tests.address_vectors import BIP173_TESTNET_P2WPKH, SYNTHETIC_TPUB
from tests.auth.conftest import JSON_HEADERS
from tests.extended_key_forms import (
    POSITION_END,
    POSITION_START,
    TPUB_VERSION,
    VPUB_VERSION,
    depth_zero,
    payload_of,
    private_key_shaped_run,
    reserialised,
    with_run_inside,
)
from tests.extended_key_vectors import (
    BIP32_TV1_M,
    BIP49_ACCOUNT_UPUB,
    MULTISIG_PREFIXES,
    PRIVATE_PREFIXES,
    SCAN_KEY,
    SINGLE_SIG_PREFIXES,
    short,
)
from tests.wallets_harness import lose_the_race

if TYPE_CHECKING:
    from httpx import AsyncClient, Response
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

WALLETS: Final = "/api/wallets"
ELLIPSIS: Final = "\N{HORIZONTAL ELLIPSIS}"

#: One accepted key of each test-network prefix: `tpub`, `upub` and `vpub`.
TEST_KEYS: Final = (BIP32_TV1_M, BIP49_ACCOUNT_UPUB, SCAN_KEY)


def masked(key: str) -> str:
    """R8, written out rather than imported, so the service cannot drift from the spec."""
    return f"{key[:4]}{ELLIPSIS}{key[-4:]}"


def assert_key_absent(body: str, key: str) -> None:
    """The key, and any run of its hidden middle long enough to search for, is not in `body`.

    Twelve characters is the length the router's address tests already treat as disclosing.
    The middle is checked in overlapping windows, so a response that served the key with its
    first and last four characters cut off still fails.
    """
    assert key not in body
    middle = key[4:-4]
    for start in range(len(middle) - 11):
        window = middle[start : start + 12]
        assert window not in body, f"a part of the key's hidden middle was served: {window!r}"


async def create(
    client: AsyncClient, address: str, *, chain_key: str = "bitcoin", label: str | None = None
) -> Response:
    body: dict[str, Any] = {"chain_key": chain_key, "address": address}
    if label is not None:
        body["label"] = label
    return await client.post(WALLETS, json=body, headers=JSON_HEADERS)


async def stored(sessionmaker: async_sessionmaker[AsyncSession]) -> list[tuple[Any, ...]]:
    async with sessionmaker() as session:
        result = await session.execute(
            text("SELECT kind, chain_key, address_canonical, address_display FROM wallets")
        )
        return [tuple(row) for row in result.all()]


def assert_refused_as(response: Response, reason: AddressRejection) -> None:
    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    (error,) = response.json()["errors"]
    assert error["loc"] == ["body", "address"]
    assert error["type"] == reason.value
    assert error["msg"] == REJECTION_MESSAGES[reason]


# --------------------------------------------------------------------------------------
# Accepted, stored whole, served masked
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", TEST_KEYS, ids=["tpub", "upub", "vpub"])
async def test_a_public_key_is_created_and_served_masked(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    key: str,
) -> None:
    response = await create(signed_in_api_client, key, label="Savings")

    assert response.status_code == 201, response.text
    created = response.json()
    assert created["kind"] == "extended_key"
    assert created["address"] == masked(key)
    assert len(created["address"]) == 9
    assert created["label"] == "Savings"
    assert_key_absent(response.text, key)
    assert_key_absent(response.text, depth_zero(key))
    assert await stored(api_sessionmaker) == [("extended_key", "bitcoin", depth_zero(key), key)]


async def test_the_key_is_masked_on_every_route_that_serves_the_wallet(
    signed_in_api_client: AsyncClient,
) -> None:
    created = (await create(signed_in_api_client, SCAN_KEY)).json()
    wallet = f"{WALLETS}/{created['id']}"

    responses = [
        await signed_in_api_client.get(WALLETS),
        await signed_in_api_client.patch(wallet, json={"label": "Renamed"}, headers=JSON_HEADERS),
        await signed_in_api_client.patch(wallet, json={"archived": True}, headers=JSON_HEADERS),
        await signed_in_api_client.get(WALLETS, params={"include_archived": "true"}),
        await signed_in_api_client.patch(wallet, json={"archived": False}, headers=JSON_HEADERS),
    ]

    for response in responses:
        assert response.status_code == 200, response.text
        assert_key_absent(response.text, SCAN_KEY)
    assert responses[1].json()["address"] == masked(SCAN_KEY)
    assert responses[1].json()["label"] == "Renamed"
    (listed,) = responses[3].json()["wallets"]
    assert (listed["kind"], listed["address"], listed["archived"]) == (
        "extended_key",
        masked(SCAN_KEY),
        True,
    )


async def test_the_key_is_in_no_balance_response_either(
    signed_in_api_client: AsyncClient,
) -> None:
    created = (await create(signed_in_api_client, SCAN_KEY)).json()

    for path in (
        "/api/balances/current",
        f"{WALLETS}/{created['id']}/balances",
        "/api/balances/runs",
    ):
        response = await signed_in_api_client.get(path)
        assert response.status_code == 200, f"{path}: {response.text}"
        assert_key_absent(response.text, SCAN_KEY)


async def test_an_address_wallet_says_so_and_is_served_whole(
    signed_in_api_client: AsyncClient,
) -> None:
    response = await create(signed_in_api_client, BIP173_TESTNET_P2WPKH)

    assert response.status_code == 201, response.text
    assert (response.json()["kind"], response.json()["address"]) == (
        "address",
        BIP173_TESTNET_P2WPKH,
    )


async def test_surrounding_whitespace_is_stripped_before_the_key_is_stored(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    response = await create(signed_in_api_client, f"  {SCAN_KEY}\n")

    assert response.status_code == 201, response.text
    assert response.json()["address"] == masked(SCAN_KEY)
    assert await stored(api_sessionmaker) == [
        ("extended_key", "bitcoin", depth_zero(SCAN_KEY), SCAN_KEY)
    ]


async def test_the_same_key_twice_is_a_conflict_that_does_not_quote_it(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await create(signed_in_api_client, SCAN_KEY)

    response = await create(signed_in_api_client, f" {SCAN_KEY} ", label="again")

    assert response.status_code == 409, response.text
    assert_key_absent(response.text, SCAN_KEY)
    assert len(await stored(api_sessionmaker)) == 1


# --------------------------------------------------------------------------------------
# One account, one wallet, however it was exported (S1)
# --------------------------------------------------------------------------------------


def test_the_independent_canonical_form_is_what_it_says() -> None:
    """The control for the tests below: `depth_zero` really moves an account key, and only it.

    BIP32's test vector 1 master key is already at depth 0 with a zero fingerprint and child
    number, so it is its own canonical form; the BIP-84 account key is at depth 3, so it is
    not, and only the nine position bytes differ.
    """
    assert depth_zero(BIP32_TV1_M) == BIP32_TV1_M
    assert depth_zero(SCAN_KEY) != SCAN_KEY
    account = payload_of(SCAN_KEY)
    canonical = payload_of(depth_zero(SCAN_KEY))
    assert account[POSITION_START] == 3, "BIP-84's account key is at depth 3"
    assert canonical[:POSITION_START] == account[:POSITION_START]
    assert canonical[POSITION_START:POSITION_END] == bytes(9)
    assert canonical[POSITION_END:] == account[POSITION_END:]
    assert depth_zero(SCAN_KEY).startswith("vpub")


@pytest.mark.parametrize(
    "position",
    [
        pytest.param(bytes(9), id="the canonical form itself"),
        pytest.param(bytes([1]) + bytes.fromhex("aabbccdd") + (5).to_bytes(4, "big"), id="depth 1"),
        pytest.param(bytes([3]) + bytes(4) + bytes(4), id="depth 3 with no parent"),
    ],
)
async def test_another_export_of_the_same_account_is_a_duplicate(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    position: bytes,
) -> None:
    await create(signed_in_api_client, SCAN_KEY)
    other_export = reserialised(SCAN_KEY, position=position)
    assert other_export != SCAN_KEY

    response = await create(signed_in_api_client, other_export, label="again")

    assert response.status_code == 409, response.text
    assert_key_absent(response.text, other_export)
    assert_key_absent(response.text, SCAN_KEY)
    assert len(await stored(api_sessionmaker)) == 1


async def test_the_canonical_export_first_then_the_account_export_is_a_duplicate_too(
    signed_in_api_client: AsyncClient,
) -> None:
    first = await create(signed_in_api_client, depth_zero(SCAN_KEY))
    assert first.status_code == 201, first.text

    response = await create(signed_in_api_client, SCAN_KEY)

    assert response.status_code == 409, response.text


async def test_the_same_bytes_under_another_version_are_another_wallet(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A P2WPKH and a P2PKH version over one key derive different addresses: two wallets."""
    assert SCAN_KEY.startswith("vpub")
    as_p2pkh = reserialised(SCAN_KEY, version=TPUB_VERSION)
    assert as_p2pkh.startswith("tpub")
    assert reserialised(as_p2pkh, version=VPUB_VERSION) == depth_zero(SCAN_KEY)

    first = await create(signed_in_api_client, SCAN_KEY)
    second = await create(signed_in_api_client, as_p2pkh)

    assert (first.status_code, second.status_code) == (201, 201), second.text
    assert first.json()["address"] != second.json()["address"]
    assert {row[2] for row in await stored(api_sessionmaker)} == {
        depth_zero(SCAN_KEY),
        depth_zero(as_p2pkh),
    }


# --------------------------------------------------------------------------------------
# Refused, with the reason as `type`, and nothing echoed or stored
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("chain_key", ["bitcoin", "kaspa"])
@pytest.mark.parametrize("prefix", PRIVATE_PREFIXES)
async def test_a_private_key_is_refused_by_name_on_either_chain(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    prefix: str,
    chain_key: str,
) -> None:
    sent = short(prefix)

    response = await create(signed_in_api_client, sent, chain_key=chain_key)

    assert_refused_as(response, AddressRejection.PRIVATE_KEY)
    assert sent not in response.text
    assert await stored(api_sessionmaker) == []


@pytest.mark.parametrize("prefix", MULTISIG_PREFIXES)
async def test_a_multisig_key_is_refused_by_name(
    signed_in_api_client: AsyncClient, prefix: str
) -> None:
    response = await create(signed_in_api_client, short(prefix))

    assert_refused_as(response, AddressRejection.EXTENDED_KEY_MULTISIG)


@pytest.mark.parametrize("prefix", [*SINGLE_SIG_PREFIXES, *MULTISIG_PREFIXES])
async def test_a_public_key_under_kaspa_is_named_as_an_extended_key(
    signed_in_api_client: AsyncClient, prefix: str
) -> None:
    """R2a: Kaspa extended keys are a non-goal, and a Bitcoin one under Kaspa is named."""
    response = await create(signed_in_api_client, short(prefix), chain_key="kaspa")

    assert_refused_as(response, AddressRejection.EXTENDED_KEY)


async def test_a_whole_test_key_under_kaspa_is_refused_without_echoing_it(
    signed_in_api_client: AsyncClient,
) -> None:
    response = await create(signed_in_api_client, SCAN_KEY, chain_key="kaspa")

    assert_refused_as(response, AddressRejection.EXTENDED_KEY)
    assert_key_absent(response.text, SCAN_KEY)


async def test_a_key_whose_bytes_are_not_a_point_is_refused_without_echoing_it(
    signed_in_api_client: AsyncClient,
) -> None:
    response = await create(signed_in_api_client, SYNTHETIC_TPUB)

    assert_refused_as(response, AddressRejection.INVALID_PUBLIC_KEY)
    assert_key_absent(response.text, SYNTHETIC_TPUB)


async def test_a_mistyped_key_is_a_checksum_failure_without_echoing_it(
    signed_in_api_client: AsyncClient,
) -> None:
    position = 60
    replacement = "2" if SCAN_KEY[position] != "2" else "3"
    mistyped = SCAN_KEY[:position] + replacement + SCAN_KEY[position + 1 :]

    response = await create(signed_in_api_client, mistyped)

    assert_refused_as(response, AddressRejection.BAD_CHECKSUM)
    assert_key_absent(response.text, mistyped)


# --------------------------------------------------------------------------------------
# R2b over HTTP, and a key's own 409 sentences
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("chain_key", ["bitcoin", "kaspa"])
async def test_a_private_key_run_after_text_is_refused_by_name_and_not_echoed(
    signed_in_api_client: AsyncClient,
    api_sessionmaker: async_sessionmaker[AsyncSession],
    chain_key: str,
) -> None:
    run = private_key_shaped_run("vprv")

    response = await create(signed_in_api_client, f"my key: {run}", chain_key=chain_key)

    assert_refused_as(response, AddressRejection.PRIVATE_KEY)
    assert run not in response.text
    assert run[4:24] not in response.text
    assert await stored(api_sessionmaker) == []


async def test_a_private_key_inside_an_over_long_paste_is_named_over_http(
    signed_in_api_client: AsyncClient,
) -> None:
    """The request schema's own length cap must not answer first with a generic 422."""
    run = private_key_shaped_run("tprv", 107)

    response = await create(signed_in_api_client, "x" * 300 + " " + run)

    assert_refused_as(response, AddressRejection.PRIVATE_KEY)
    assert run[4:24] not in response.text


async def test_a_public_key_with_a_run_in_its_body_is_registered(
    signed_in_api_client: AsyncClient,
) -> None:
    key = with_run_inside(SCAN_KEY, "uprv", 5)

    response = await create(signed_in_api_client, key)

    assert response.status_code == 201, response.text
    assert response.json()["address"] == masked(key)


async def test_a_duplicate_key_is_told_in_the_key_s_own_words(
    signed_in_api_client: AsyncClient,
) -> None:
    created = (await create(signed_in_api_client, SCAN_KEY)).json()

    live = await create(signed_in_api_client, depth_zero(SCAN_KEY))
    await signed_in_api_client.patch(
        f"{WALLETS}/{created['id']}", json={"archived": True}, headers=JSON_HEADERS
    )
    archived = await create(signed_in_api_client, SCAN_KEY)

    assert (live.status_code, archived.status_code) == (409, 409)
    assert live.json()["detail"] == DUPLICATE_KEY_DETAIL
    assert archived.json()["detail"] == DUPLICATE_KEY_ARCHIVED_DETAIL
    assert "another export of the same account key" in DUPLICATE_KEY_DETAIL


@pytest.mark.parametrize("archived", [False, True], ids=["live", "archived"])
async def test_a_key_that_loses_the_race_is_told_in_the_key_s_own_words(
    signed_in_api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    archived: bool,
) -> None:
    """A second export past the pre-check meets the constraint: still the key's sentence.

    The test above is answered by the pre-check. A request that passes it and then meets
    `uq_wallets_user_chain_address` -- a double-clicked submit -- is answered by the
    service's other `WalletAlreadyExistsError`, which must be told the kind too: the address
    sentence says nothing about another export of the same account, which is the case the
    key sentences exist for.
    """
    created = await create(signed_in_api_client, SCAN_KEY)
    assert created.status_code == 201, created.text
    if archived:
        await signed_in_api_client.patch(
            f"{WALLETS}/{created.json()['id']}", json={"archived": True}, headers=JSON_HEADERS
        )

    lose_the_race(monkeypatch)
    response = await create(signed_in_api_client, depth_zero(SCAN_KEY))

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        DUPLICATE_KEY_ARCHIVED_DETAIL if archived else DUPLICATE_KEY_DETAIL
    )
    assert_key_absent(response.text, SCAN_KEY)
    assert_key_absent(response.text, depth_zero(SCAN_KEY))


async def test_a_duplicate_address_keeps_the_address_s_words(
    signed_in_api_client: AsyncClient,
) -> None:
    await create(signed_in_api_client, BIP173_TESTNET_P2WPKH)

    response = await create(signed_in_api_client, BIP173_TESTNET_P2WPKH)

    assert response.status_code == 409
    assert response.json()["detail"] == DUPLICATE_DETAIL


async def test_an_over_long_paste_with_no_key_keeps_the_schema_s_length_refusal(
    signed_in_api_client: AsyncClient,
) -> None:
    response = await create(signed_in_api_client, "x" * 300)

    assert response.status_code == 422
    assert [error["type"] for error in response.json()["errors"]] == ["string_too_long"]


@pytest.mark.parametrize("value", [123, None, ["tprv8Zgx"]])
async def test_an_address_that_is_not_a_string_is_a_type_error_not_a_private_key(
    signed_in_api_client: AsyncClient, value: object
) -> None:
    response = await signed_in_api_client.post(
        WALLETS, json={"chain_key": "bitcoin", "address": value}, headers=JSON_HEADERS
    )

    assert response.status_code == 422
    assert [error["type"] for error in response.json()["errors"]] == ["string_type"]
