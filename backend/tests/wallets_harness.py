"""The duplicate race of the wallet registry, simulated, for every suite that needs to lose it.

`WalletService.create_wallet` checks for a duplicate with `find_by_canonical`, then inserts,
and treats `uq_wallets_user_chain_address` as the authority when the two disagree. That second
path is only reached by a request that loses a race, and a race is simulated here rather than
waited for. Shared by the logging suite (`tests/security/test_address_logging.py`), which
proves a lost race logs no address, and the extended-key suite
(`tests/api/test_wallets_extended_keys.py`), which proves a key that loses it is told the
key's own 409 sentence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from portfolio.repositories.wallets import WalletRepository

if TYPE_CHECKING:
    import pytest

    from portfolio.db.models import Wallet


def lose_the_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the duplicate pre-check miss **once**, then behave normally again.

    That single call is the whole race. Two requests arrive together, both run
    `find_by_canonical` before either has committed, both find nothing, both insert, and
    the second meets `uq_wallets_user_chain_address`. By the time the service looks again
    to establish *why* the insert was refused, the competing row is committed and visible
    -- so the recovery lookup must see it.

    Patching the method to answer `None` unconditionally, which is what this helper did
    first, models something else entirely: a database that has lost the row. The service
    correctly refuses to call that a conflict, re-raises, and the test then measures the
    handling of a bug rather than the handling of a race. A simulation that is wrong in
    that direction is worse than none, because it fails and looks like a real defect.
    """
    real = WalletRepository.find_by_canonical
    missed = False

    async def absent_once(
        self: WalletRepository,
        *,
        user_id: int,
        chain_key: str,
        address_canonical: str,
    ) -> Wallet | None:
        nonlocal missed
        if not missed:
            missed = True
            return None
        return await real(
            self,
            user_id=user_id,
            chain_key=chain_key,
            address_canonical=address_canonical,
        )

    monkeypatch.setattr(WalletRepository, "find_by_canonical", absent_once)
