"""Fixtures for the price-source suite.

A provider suite that needs a database is unusual and is worth justifying rather than
explaining away. Criterion 6 is "failover is exercised by a test", and the half of it that
matters operationally is not that a second source answers -- it is that the **row** ends up
saying which source answered. A `prices.source` column reading `kraken` when Coinbase
supplied the number is a record nobody can audit, and no assertion made against a
`PriceQuote` in memory can see it.

So `test_failover.py` drives the whole path: scripted vendors, the shipped source order,
the refresh service, the repository, and then the row. That is #8's closing lesson applied
before the fact -- two halves proven and the join deletable with the suite green.

The database decisions themselves live in `tests/sqlite_harness.py`, shared with
`tests/services/`, so that "a file, not `:memory:`; the migrations, not `create_all`" is
stated once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.sqlite_harness import migrated_session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def price_session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    """A session over a freshly migrated, file-backed database, with `assets` seeded."""
    async with migrated_session(tmp_path, name="prices.db") as opened:
        yield opened
