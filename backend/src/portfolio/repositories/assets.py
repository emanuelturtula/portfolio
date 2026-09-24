"""Reads of the `assets` table. Queries and nothing else.

Small, and it exists rather than being folded into `repositories/prices.py` because
`prices.asset_id` has to be resolved from a symbol and an assets query living inside the
prices repository would be the muddier of the two options -- the next table that needs an
asset id would either import it from there or write a second copy.

**There is no write here.** The rows are seeded by `0002_seed_assets` and the product has
no way to add an asset; a method that could insert one would be a method with no caller and
a claim no test can check.

No scoping by `user_id`, unlike `WalletRepository`: an asset is reference data, the same for
every account, and scoping reference data by owner would be a `WHERE` clause that is always
true and would read as though it were protecting something.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from portfolio.db.models import Asset

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class AssetRepository:
    """Every query this application makes against `assets`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def by_symbol(self) -> dict[str, Asset]:
        """Every asset, keyed by its symbol.

        One query rather than one per symbol. A caller valuing a portfolio needs the id of
        every asset it holds, and four round trips to answer four symbols is four times the
        latency for a table of three rows.

        Ordered by the primary key, which is an `INTEGER`: the ordering is immaterial to a
        mapping, and saying so here is cheaper than leaving a reader to wonder whether an
        unordered query was an oversight. `symbol` is unique, so no key can collide.
        """
        result = await self._session.scalars(select(Asset).order_by(Asset.id))
        return {asset.symbol: asset for asset in result}

    async def get_by_symbol(self, symbol: str) -> Asset | None:
        """One asset by its symbol, or `None`.

        Case-sensitive, and deliberately not `func.lower()`: `assets.symbol` holds the
        canonical upper-case ticker and every caller in this application passes a constant
        from `providers.prices.base`. A lookup that quietly accepted `btc` would make the
        day somebody passes a user-supplied string look like it works.
        """
        found: Asset | None = await self._session.scalar(
            select(Asset).where(Asset.symbol == symbol)
        )
        return found
