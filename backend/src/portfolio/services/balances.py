"""Reading balances back, valuing them, and listing what the sync did. **No provider here.**

This module imports `repositories` and `services/prices.py` and nothing from `providers`,
and that absence is structural rather than incidental. A router imports it, so any import of
`portfolio.providers.prices` from here -- direct or through anything else -- would make
`backend/.importlinter`'s `prices-are-never-fetched-in-a-request` contract fail, which it
has never done on a real chain and would correctly do here.

**That is why `ChainKey.asset_symbol` exists.** Valuing a balance needs the asset symbol for
a chain, and the obvious place to get one is `providers/prices/base.py`, which declares `BTC`
and `KAS` for the vendors' pair codes. Reaching for those from here is the one-line change
that turns the contract red. The symbol is a property of the chain, so it lives in `domain`,
which imports nothing.

The same reasoning is why `SyncRunSummary` is defined in `repositories/sync_runs.py` rather
than beside the sync service that fills it in: `GET /api/balances/runs` is a read, and the
read side must not import the module that imports a chain provider.

## An unread wallet is not an empty wallet

A wallet with no snapshot comes back with `confirmed`, `quantity`, `value` and `observed_at`
all `None` -- never a zero. A zero is a number, it renders, it sums, and nothing downstream
can tell it from an address that really holds nothing. This is the same rule
`services/prices.py` applies to a missing price, one layer over: *a portfolio silently
showing 0 is worse than one showing an error, because it is believed.*

## The total is the sum of the rows the endpoint prints

`PriceService.value_portfolio` is asked about one holding per **asset symbol**, with the
quantities summed, because that is the shape `unpriced` has to come back in -- a reason per
asset, not per wallet. The per-wallet `value` is then that symbol's price times that
wallet's quantity, and `total` is the sum of those per-wallet values rather than
`PortfolioValue.total`.

The two agree for every amount this product will ever hold, and summing the rows is still
the right one to publish: what a reader can check is that the column adds up, and a total
computed from a different partition of the same numbers is a total they cannot check. It is
summed in Python, over rows loaded whole, for the reason every money total in this
application is: `SUM()` on a `TEXT` money column coerces it to a float in SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from portfolio.domain.chains import ChainKey
from portfolio.domain.money import from_base_units
from portfolio.repositories.balances import BalanceRepository
from portfolio.repositories.sync_runs import SyncRunRepository
from portfolio.repositories.wallets import WalletRepository
from portfolio.services.prices import Holding, Price, build_price_service
from portfolio.services.wallets import WalletNotFoundError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.db.models import BalanceSnapshot, Wallet
    from portfolio.repositories.sync_runs import SyncRunSummary
    from portfolio.services.auth import Principal
    from portfolio.services.prices import PriceService, UnpricedHolding

__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_RUNS_LIMIT",
    "MAX_HISTORY_LIMIT",
    "MAX_RUNS_LIMIT",
    "BalanceService",
    "CurrentBalances",
    "SnapshotView",
    "WalletBalance",
    "WalletHistory",
    "build_balance_service",
    "utc_now",
]

DEFAULT_HISTORY_LIMIT: Final = 500
MAX_HISTORY_LIMIT: Final = 1000
DEFAULT_RUNS_LIMIT: Final = 50
MAX_RUNS_LIMIT: Final = 200
"""Page sizes, named here rather than in the request schema.

The schema refuses an out-of-range `limit` with a 422, which is the right answer to a client
that asked for one. These are the same numbers, applied again inside the service, because
the CLI and any future importer do not go through the schema -- the same argument
`MAX_LABEL_LENGTH` makes. Here they clamp rather than raise: an internal caller passing a
silly number should get a page, not an exception it has no better answer for.
"""


def utc_now() -> datetime:
    """The clock, in one place, so a test can replace it with a value it chose."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class WalletBalance:
    """One wallet's most recent reading, valued if its asset could be priced.

    **Six fields are nullable together and they mean one thing: nothing has been read yet.**
    `confirmed`, `decimals`, `quantity` and `observed_at` are `None` for a wallet no
    successful run has covered. `value` and `price` are `None` for that *and* for a wallet
    whose asset has no price, which are different facts -- `CurrentBalances.unpriced` is
    where the second one is named, with its reason.

    `confirmed` and `pending` are integer **base units** and stay integers all the way to
    the wire, where they are rendered as JSON strings. `quantity` is the same number as a
    `Decimal`, converted once here through `domain.money.from_base_units` so that no caller
    divides by a power of ten for itself.
    """

    wallet_id: int
    chain_key: str
    label: str | None
    asset_symbol: str
    confirmed: int | None
    pending: int | None
    decimals: int | None
    quantity: Decimal | None
    value: Decimal | None
    price: Price | None
    observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class CurrentBalances:
    """What the portfolio is worth now, what it could not value, and how old the reading is.

    **`total` read without `complete` is a number that silently omits a holding**, which is
    indistinguishable from a number that includes it. Every renderer has to look at
    `complete`, and `unpriced` is there so that what it says can be specific.

    `as_of` is the newest `observed_at` among the wallets that have one, and `None` when none
    does. It is the *newest* rather than the oldest deliberately: it answers "when was this
    page last updated", and each row carries its own `observed_at` for the wallet whose chain
    has been failing since Tuesday.
    """

    quote_currency: str
    total: Decimal
    complete: bool
    as_of: datetime | None
    wallets: tuple[WalletBalance, ...]
    unpriced: tuple[UnpricedHolding, ...]


@dataclass(frozen=True, slots=True)
class SnapshotView:
    """One historical reading of one wallet.

    No `decimals`: every row of one wallet's history carries the same exponent in practice,
    so it is published once on `WalletHistory` rather than repeated on every point of a
    chart. `quantity` is converted here, from the exponent the row itself recorded, so a
    reading taken before an asset's `decimals` was ever edited still reads correctly.
    """

    observed_at: datetime
    confirmed: int
    pending: int | None
    quantity: Decimal
    sync_run_id: int


@dataclass(frozen=True, slots=True)
class WalletHistory:
    """One wallet's readings, oldest first.

    `decimals` is `None` for a wallet that has never been read, because the exponent is a
    property of the *readings* and there are none. It is deliberately not filled in from
    `assets`: the whole reason the column is on the snapshot is that an asset row can be
    edited, and answering this question from `assets` would reintroduce exactly the
    reinterpretation the column exists to prevent.
    """

    wallet_id: int
    decimals: int | None
    snapshots: tuple[SnapshotView, ...]


class BalanceService:
    """Reads snapshots and the run log, and values holdings. Opens no socket, writes nothing.

    It takes no session, for the reason `PriceService` does not: every method here reads, so
    a session on this class would be a field nothing uses and would suggest this service owns
    a transaction. `build_balance_service` takes it, because the repositories need one.
    """

    def __init__(
        self,
        *,
        wallets: WalletRepository,
        balances: BalanceRepository,
        runs: SyncRunRepository,
        prices: PriceService,
    ) -> None:
        self._wallets = wallets
        self._balances = balances
        self._runs = runs
        self._prices = prices

    async def current_balances(
        self,
        principal: Principal,
        *,
        quote_currency: str,
    ) -> CurrentBalances:
        """Every active wallet's latest reading, valued in one currency.

        Archived wallets are excluded, because an archived wallet is one the owner asked us
        to stop reading and its last balance is not part of what they hold today. Its history
        still answers -- that is what `wallet_history` is for, and why archiving is a
        timestamp rather than a delete.

        Three queries regardless of how many wallets there are: the wallets, their latest
        snapshots, and the price rows for the currency.

        **Nothing is converted between currencies.** A EUR valuation reads EUR prices; a
        holding with only a USD price is unpriced rather than translated, which is #9's
        contract and is re-asserted here rather than re-decided.
        """
        wallets = await self._wallets.list_for_user(principal.user_id)
        latest = await self._balances.latest_for_wallets([wallet.id for wallet in wallets])

        quantities = {
            wallet.id: _quantity_of(latest.get(wallet.id))
            for wallet in wallets
            if latest.get(wallet.id) is not None
        }
        portfolio = await self._prices.value_portfolio(
            _holdings_of(wallets, quantities),
            quote_currency=quote_currency,
        )
        price_by_symbol = {entry.asset_symbol: entry.price for entry in portfolio.valued}

        rows = [
            _wallet_balance(wallet, latest.get(wallet.id), price_by_symbol) for wallet in wallets
        ]
        # `Decimal(0)` as the start value, not the `0` that `sum` defaults to: an empty
        # portfolio must come back as a Decimal like every other total.
        total = sum((row.value for row in rows if row.value is not None), Decimal(0))
        observations = [row.observed_at for row in rows if row.observed_at is not None]
        return CurrentBalances(
            quote_currency=quote_currency,
            total=total,
            complete=portfolio.complete,
            as_of=max(observations) if observations else None,
            wallets=tuple(rows),
            unpriced=portfolio.unpriced,
        )

    async def wallet_history(
        self,
        principal: Principal,
        wallet_id: int,
        *,
        since: datetime | None = None,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> WalletHistory:
        """One wallet's readings, oldest first, from `since` onwards.

        **An archived wallet still answers.** Its history is the reason archiving is a
        timestamp and not a delete, and refusing to show it would make retiring an address
        destroy the record of what it held.

        `limit` takes the *first* rows at or after `since`, which makes the pair a forward
        cursor: read a window, take the last `observed_at` you saw, ask again from there.

        Raises:
            WalletNotFoundError: no wallet with that id belongs to the caller. Scoped by
                `user_id`, so somebody else's wallet is indistinguishable from one that does
                not exist -- the only answer that does not confirm the id.
        """
        wallet = await self._wallets.get_for_user(principal.user_id, wallet_id)
        if wallet is None:
            raise WalletNotFoundError
        rows = await self._balances.history(
            wallet_id=wallet_id,
            since=since,
            limit=_clamped(limit, MAX_HISTORY_LIMIT),
        )
        return WalletHistory(
            wallet_id=wallet_id,
            # From the newest row in the page rather than the oldest: if an exponent ever
            # did change, the most recent reading is the one a caller rendering "now" wants.
            decimals=rows[-1].decimals if rows else None,
            snapshots=tuple(
                SnapshotView(
                    observed_at=row.observed_at,
                    confirmed=row.confirmed,
                    pending=row.pending,
                    quantity=from_base_units(row.confirmed, row.decimals),
                    sync_run_id=row.sync_run_id,
                )
                for row in rows
            ),
        )

    async def list_runs(self, *, limit: int = DEFAULT_RUNS_LIMIT) -> tuple[SyncRunSummary, ...]:
        """The most recent sync runs, newest first, each with its per-chain outcomes.

        This is what makes criterion 4 observable without opening the database, and it is
        what #23's sync-health endpoint will eventually read rather than re-derive.
        """
        return tuple(await self._runs.list_runs(limit=_clamped(limit, MAX_RUNS_LIMIT)))


def _clamped(limit: int, ceiling: int) -> int:
    """A page size inside the bounds, for a caller that did not come through the schema."""
    return min(max(limit, 1), ceiling)


def _quantity_of(snapshot: BalanceSnapshot | None) -> Decimal:
    """A snapshot's confirmed balance as a decimal amount, by the domain's one rule.

    Called only for a wallet that has one; the `None` arm exists because `dict.get` is how
    the caller asks, and returning a zero for it would be the ambiguity this module refuses
    -- so it is unreachable by construction rather than by a comment, and mypy needs the arm.
    """
    if snapshot is None:
        return Decimal(0)
    return from_base_units(snapshot.confirmed, snapshot.decimals)


def _holdings_of(wallets: Sequence[Wallet], quantities: dict[int, Decimal]) -> list[Holding]:
    """One holding per asset symbol, with the wallets' quantities summed.

    Per symbol rather than per wallet, because `PortfolioValue.unpriced` has to come back as
    one reason per asset -- "KAS has no price" -- rather than the same reason repeated once
    per wallet holding it.

    Sorted by symbol so that two calls over one registry produce the same `unpriced` order
    and a response is stable between renders.
    """
    totals: dict[str, Decimal] = {}
    for wallet in wallets:
        quantity = quantities.get(wallet.id)
        if quantity is None:
            continue
        symbol = ChainKey(wallet.chain_key).asset_symbol
        totals[symbol] = totals.get(symbol, Decimal(0)) + quantity
    return [Holding(asset_symbol=symbol, quantity=totals[symbol]) for symbol in sorted(totals)]


def _wallet_balance(
    wallet: Wallet,
    snapshot: BalanceSnapshot | None,
    price_by_symbol: dict[str, Price],
) -> WalletBalance:
    """Build one row: the wallet, its latest reading if there is one, and its value.

    The multiplication is `quantity * price.amount`, exactly the expression
    `ValuedHolding.value` uses, so a row here and a valuation elsewhere cannot disagree about
    what one holding is worth.
    """
    symbol = ChainKey(wallet.chain_key).asset_symbol
    price = price_by_symbol.get(symbol)
    quantity = None if snapshot is None else from_base_units(snapshot.confirmed, snapshot.decimals)
    return WalletBalance(
        wallet_id=wallet.id,
        chain_key=wallet.chain_key,
        label=wallet.label,
        asset_symbol=symbol,
        confirmed=None if snapshot is None else snapshot.confirmed,
        pending=None if snapshot is None else snapshot.pending,
        decimals=None if snapshot is None else snapshot.decimals,
        quantity=quantity,
        value=None if quantity is None or price is None else quantity * price.amount,
        price=price,
        observed_at=None if snapshot is None else snapshot.observed_at,
    )


def build_balance_service(
    session: AsyncSession,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> BalanceService:
    """Assemble the read-side service over one database session.

    The repositories are built here rather than injected because there is exactly one
    implementation of each; the clock is injectable and is handed to the price service, so
    that a test can name the instant and watch one price go from fresh to stale.
    """
    return BalanceService(
        wallets=WalletRepository(session),
        balances=BalanceRepository(session),
        runs=SyncRunRepository(session),
        prices=build_price_service(session, clock=clock),
    )
