"""Spec 031, R8 at the service boundary: the mask, and the view that applies it.

`view_of` is the one place a wallet row becomes something a router can serve, so it is
where an extended key has to stop being whole. The router suite proves the response; this
proves the rule, including its floor for a string too short to have a middle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

import pytest

from portfolio.db.models import Wallet
from portfolio.domain.chains import WalletKind
from portfolio.services.wallets import (
    MASK_ELLIPSIS,
    MASK_VISIBLE_CHARACTERS,
    mask_extended_key,
    view_of,
)
from tests.address_vectors import BIP173_TESTNET_P2WPKH
from tests.extended_key_vectors import BIP32_TV1_M, BIP49_ACCOUNT_UPUB, SCAN_KEY

CREATED_AT: Final = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def a_wallet(*, kind: str, address: str) -> Wallet:
    return Wallet(
        id=11,
        user_id=1,
        chain_key="bitcoin",
        address_canonical=address,
        address_display=address,
        label=None,
        archived_at=None,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
        kind=kind,
    )


def test_the_mask_constants_are_the_spec_s() -> None:
    assert MASK_VISIBLE_CHARACTERS == 4
    assert MASK_ELLIPSIS == "…"
    assert len(MASK_ELLIPSIS) == 1, "one character, not three full stops"


@pytest.mark.parametrize("key", [BIP32_TV1_M, BIP49_ACCOUNT_UPUB, SCAN_KEY])
def test_a_key_is_masked_to_four_an_ellipsis_and_four(key: str) -> None:
    assert len(key) == 111
    assert mask_extended_key(key) == key[:4] + "…" + key[-4:]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        pytest.param("", "…", id="empty"),
        pytest.param("tpub", "…", id="a prefix alone"),
        pytest.param("tpub8Zgx", "…", id="eight, nothing hidden"),
        pytest.param("tpub8Zgx9", "tpub…Zgx9", id="nine, one hidden"),
    ],
)
def test_a_string_with_no_middle_to_hide_is_masked_entirely(given: str, expected: str) -> None:
    """The floor: never serve a string whole because it was too short to mask."""
    assert mask_extended_key(given) == expected
    if len(given) <= 8:
        assert given == "" or given not in mask_extended_key(given)


def test_the_view_of_a_key_wallet_carries_its_kind_and_only_the_mask() -> None:
    view = view_of(a_wallet(kind="extended_key", address=SCAN_KEY))

    assert view.kind is WalletKind.EXTENDED_KEY
    assert view.address == SCAN_KEY[:4] + "…" + SCAN_KEY[-4:]
    assert SCAN_KEY[4:-4] not in repr(view)
    assert SCAN_KEY not in repr(view)


def test_the_view_of_an_address_wallet_is_unchanged() -> None:
    view = view_of(a_wallet(kind="address", address=BIP173_TESTNET_P2WPKH))

    assert view.kind is WalletKind.ADDRESS
    assert view.address == BIP173_TESTNET_P2WPKH


def test_a_row_with_a_kind_the_domain_does_not_know_is_refused() -> None:
    """`view_of` reads `kind` strictly: an unknown one is a defect, not an address."""
    with pytest.raises(ValueError, match="'xpub_wallet'"):
        view_of(a_wallet(kind="xpub_wallet", address=SCAN_KEY))
