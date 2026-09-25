"""The read-side balance service, for the one rule the HTTP layer never lets it see.

Everything else `BalanceService` does is driven end to end through the router in
`tests/api/test_balances.py`, which is where its contracts are worth asserting: against the
JSON a client actually receives. This module covers the case that suite cannot reach.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest

from portfolio.domain.chains import ChainKey
from portfolio.services.auth import Principal
from portfolio.services.balances import HistoryCursor, build_balance_service
from tests.address_vectors import BIP173_TESTNET_P2WPKH
from tests.balance_harness import insert_user, insert_wallet

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SOME_INSTANT: Final = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)


async def test_a_history_page_cannot_start_both_at_an_instant_and_after_a_cursor(
    service_session: AsyncSession,
) -> None:
    """`since` and a cursor together are refused by the service too, not only by the schema.

    The request schema turns the pair into a 422 before the service is reached, so no HTTP
    test can get here. The service refuses it anyway, because the CLI and any future
    importer call it without a schema in front: they are two answers to "where does this
    page start", and no rule combines them honestly. Silently preferring one would return a
    page from a place the caller did not ask about.

    The wallet is real and belongs to the caller, so the refusal is about the arguments and
    not about the wallet lookup that follows them.
    """
    user_id = await insert_user(service_session)
    wallet_id = await insert_wallet(
        service_session,
        user_id=user_id,
        chain_key=ChainKey.BITCOIN,
        address=BIP173_TESTNET_P2WPKH,
    )
    service = build_balance_service(service_session)
    principal = Principal(user_id=user_id, username="owner", session_id=1)

    with pytest.raises(ValueError, match="not both"):
        await service.wallet_history(
            principal,
            wallet_id,
            since=SOME_INSTANT,
            after=HistoryCursor(observed_at=SOME_INSTANT, snapshot_id=1),
        )

    page = await service.wallet_history(principal, wallet_id, since=SOME_INSTANT)
    assert page.snapshots == (), "either one alone is an ordinary request"
