"""Fixtures shared by the whole test suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from portfolio.main import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

BASE_URL = "http://testserver"


@pytest.fixture
def app() -> FastAPI:
    """A freshly built application, wired exactly as the server wires it."""
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client that talks to the app in-process, without binding a port."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as http_client:
        yield http_client
