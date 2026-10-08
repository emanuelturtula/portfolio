"""The balance rebuild: each wallet's past daily balances, from its transactions (spec 038).

For every active wallet, the chain's provider reads the confirmed history of each address the
wallet owns -- its own address, or every address an extended key has derived and used -- and
`domain.balance_history.rebuild_daily` walks the effects back from the balance they were
checked against. A complete rebuild replaces the wallet's rows in `reconstructed_balances`;
anything less keeps the rows it had (R6) and says why.

**Wallets are rebuilt one at a time, and each is its own transaction.** A wallet that fails or
proves incomplete does not stop the next one, and a crash leaves every finished wallet stored.
Requests go out sequentially through the shared client, so the per-host floor holds.

**Never imported by a router.** Only `main.py` and `cli.py` build it, which is what keeps the
`prices-are-never-fetched-in-a-request` reasoning true for chains too: no request path asks a
chain for a history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from typing import TYPE_CHECKING

from portfolio.domain.addresses import AddressInvalidError
from portfolio.domain.balance_history import Effect, rebuild_daily
from portfolio.domain.chains import WalletKind
from portfolio.providers.base import TransactionHistoryReader
from portfolio.providers.errors import ProviderError
from portfolio.repositories.derived_addresses import DerivedAddressRepository
from portfolio.repositories.reconstructed_balances import ReconstructedBalanceRepository
from portfolio.repositories.wallets import WalletRepository
from portfolio.services.prices import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date, datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.db.models import Wallet
    from portfolio.providers.base import AddressHistory
    from portfolio.repositories.derived_addresses import DerivedAddressRecord
    from portfolio.services.balance_sync import ChainProviderFor

__all__ = [
    "BalanceRebuildService",
    "RebuildOutcome",
    "RebuildReport",
    "WalletRebuild",
    "build_balance_rebuild_service",
]


class RebuildOutcome(StrEnum):
    """What became of one wallet's rebuild. The member is its wire form.

    * `rebuilt` -- the history proved complete; the wallet's rows were replaced.
    * `incomplete` -- a history did not prove itself (R1) or the walk did not reach zero (R4);
      the rows it had are kept. `reason` says which.
    * `failed` -- the provider raised; `reason` is the error's class name.
    * `unsupported` -- the chain's provider cannot read a history.
    """

    REBUILT = "rebuilt"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class WalletRebuild:
    """One wallet's outcome. `days` and `first_day` describe what was stored, when anything was."""

    wallet_id: int
    chain_key: str
    outcome: RebuildOutcome
    days: int
    first_day: date | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class RebuildReport:
    """Every active wallet's outcome, in wallet id order."""

    wallets: tuple[WalletRebuild, ...]


class BalanceRebuildService:
    """Reads every active wallet's history and stores the complete ones. Owns its commits."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        wallets: WalletRepository,
        derived: DerivedAddressRepository,
        rebuilt: ReconstructedBalanceRepository,
        provider_for: ChainProviderFor,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._wallets = wallets
        self._derived = derived
        self._rebuilt = rebuilt
        self._provider_for = provider_for
        self._clock = clock

    async def rebuild(self) -> RebuildReport:
        """Rebuild every active wallet, one at a time. Never raises for a provider's failure."""
        now = self._clock()
        today = now.astimezone(UTC).date()
        wallets = sorted(await self._wallets.list_all_active(), key=lambda wallet: wallet.id)
        derived = await self._derived.list_for_wallets(
            [wallet.id for wallet in wallets if wallet.kind == WalletKind.EXTENDED_KEY.value]
        )
        outcomes = [
            await self._rebuild_one(wallet, derived.get(wallet.id, ()), today=today, now=now)
            for wallet in wallets
        ]
        return RebuildReport(wallets=tuple(outcomes))

    async def _rebuild_one(
        self,
        wallet: Wallet,
        derived: Sequence[DerivedAddressRecord],
        *,
        today: date,
        now: datetime,
    ) -> WalletRebuild:
        """Read, rebuild and, when complete, store one wallet."""
        try:
            provider = self._provider_for(wallet.chain_key)
        except ProviderError as exc:
            return _outcome(wallet, RebuildOutcome.FAILED, reason=type(exc).__name__)
        if not isinstance(provider, TransactionHistoryReader):
            return _outcome(wallet, RebuildOutcome.UNSUPPORTED)
        addresses = (
            [record.address for record in derived if record.used]
            if wallet.kind == WalletKind.EXTENDED_KEY.value
            else [wallet.address_canonical]
        )
        histories: list[AddressHistory] = []
        try:
            for address in addresses:
                histories.append(await provider.address_history(address))
        except (ProviderError, AddressInvalidError) as exc:
            return _outcome(wallet, RebuildOutcome.FAILED, reason=type(exc).__name__)

        unproven = next((h.incomplete for h in histories if h.incomplete is not None), None)
        if unproven is not None:
            return _outcome(wallet, RebuildOutcome.INCOMPLETE, reason=unproven.value)
        result = rebuild_daily(
            (
                Effect(occurred_at=effect.occurred_at, delta=effect.delta)
                for history in histories
                for effect in history.effects
            ),
            balance=sum(history.balance for history in histories),
            today=today,
        )
        if result.refused is not None:
            return _outcome(wallet, RebuildOutcome.INCOMPLETE, reason=result.refused.value)

        await self._rebuilt.replace_for_wallet(
            wallet.id,
            result.days,
            decimals=provider.capabilities.decimals,
            rebuilt_at=now,
        )
        await self._session.commit()
        return WalletRebuild(
            wallet_id=wallet.id,
            chain_key=wallet.chain_key,
            outcome=RebuildOutcome.REBUILT,
            days=len(result.days),
            first_day=result.days[0].day if result.days else None,
            reason=None,
        )


def _outcome(
    wallet: Wallet, outcome: RebuildOutcome, *, reason: str | None = None
) -> WalletRebuild:
    """An outcome that stored nothing."""
    return WalletRebuild(
        wallet_id=wallet.id,
        chain_key=wallet.chain_key,
        outcome=outcome,
        days=0,
        first_day=None,
        reason=reason,
    )


def build_balance_rebuild_service(
    session: AsyncSession,
    *,
    provider_for: ChainProviderFor,
    clock: Callable[[], datetime] = utc_now,
) -> BalanceRebuildService:
    """Assemble the rebuild over one session and the caller's way of getting a provider.

    `provider_for` has no default, for the reason the balance sync's has none: building a
    provider needs the process-wide HTTP client, whose lifetime is the caller's.
    """
    return BalanceRebuildService(
        session=session,
        wallets=WalletRepository(session),
        derived=DerivedAddressRepository(session),
        rebuilt=ReconstructedBalanceRepository(session),
        provider_for=provider_for,
        clock=clock,
    )
