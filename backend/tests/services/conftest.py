"""Fixtures for the service layer's database-backed tests.

Until #9 every test in this package was pure -- `test_wallets_service.py` builds an
unattached mapped instance and never opens a connection, because the wallet policy that
needs a database is driven end to end through the router instead. The price service has no
router to be driven through (#10 and #11 own that), and what it decides it decides against
rows, so it needs a real one.

The decisions behind that database -- a file rather than `:memory:`, the migrations rather
than `create_all`, the application's own engine -- live in `tests/sqlite_harness.py`, in
one place, because `tests/providers/prices/` needs the same database for criterion 6 and
two copies of those three decisions would drift.
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
async def service_session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    """A session over a freshly migrated, file-backed database."""
    async with migrated_session(tmp_path) as opened:
        yield opened
