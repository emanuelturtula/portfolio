"""#93 and rule 3: the transactions view logs neither id, nor a payload, nor an amount.

`GET /api/exchanges/fills` serves the owner's trades: the order id crosses the API because the
owner needs it to find a trade at the venue, and the trade id never does. Spec 024:
**neither id is ever logged**, and `raw_payload` is never read on this path at all.

This drives the endpoint through the **real application with the production JSON logging** --
every page, every filter, a refused range, an unknown venue, and an anonymous request -- and
searches every byte on stdout and every standard-library record for each sentinel. What the
test itself writes to plant its rows is discarded before the search. The positive companion
comes first, so an empty capture cannot pass: the anonymous request's refusal must be there,
naming the path.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from typing import TYPE_CHECKING, Any, Final

from httpx import ASGITransport, AsyncClient

from portfolio.main import create_app
from tests.auth.conftest import BASE_URL, sign_in
from tests.fill_view_harness import (
    FILLS_PATH,
    PAYLOAD_SENTINEL,
    TRADE_ID_PREFIX,
    minute,
    plant_history,
    the_book,
    user_id_of,
)
from tests.security.conftest import assert_carried_something

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from tests.security.conftest import ProductionLoggingInstaller

#: The book's order ids, its one distinctive amount, and what must never be read at all.
ORDER_ID_SENTINELS: Final = ("ord-1001", "ord-1002", "ord-1003", "ord-2001", "ord-2002")
AMOUNT_SENTINEL: Final = "3000.123456789012345678"
SENTINELS: Final = (TRADE_ID_PREFIX, PAYLOAD_SENTINEL, AMOUNT_SENTINEL, *ORDER_ID_SENTINELS)


class EveryRecord(logging.Handler):
    """A root handler of this test's own: every standard-library record, rendered in full."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.rendered: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        parts = [record.name, record.getMessage(), repr(record.args), repr(record.__dict__)]
        if record.exc_info:
            parts.append("".join(traceback.format_exception(*record.exc_info)))
        self.rendered.append(" ".join(parts))


async def until(condition: Callable[[], bool]) -> None:
    while not condition():  # noqa: ASYNC110
        await asyncio.sleep(0.01)


def leaks(searched: str) -> list[str]:
    """Each sentinel found, with the line it was found on."""
    found = []
    for sentinel in SENTINELS:
        if sentinel in searched:
            line = next(one for one in searched.splitlines() if sentinel in one)
            found.append(f"{sentinel}: {line[:300]}")
    return found


async def test_no_id_payload_or_amount_of_a_fill_reaches_a_log_line(
    api_environment: Path,
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the production log level, over every request shape the endpoint answers."""
    del api_environment
    monkeypatch.setattr("portfolio.main.exchange_providers", lambda client, **_: {})
    app = create_app()
    production_logging()
    records = EveryRecord()
    logging.getLogger().addHandler(records)
    requests: list[dict[str, Any]] = [
        {},
        {"limit": 1, "offset": 3},
        {"exchange": "bingx"},
        {"exchange": ["bitget", "bingx"], "from": minute(5).isoformat()},
        {"from": minute(10).isoformat(), "to": minute(20).isoformat()},
        {"from": "2024-07-19T13:57:11"},
        {"from": minute(20).isoformat(), "to": minute(10).isoformat()},
        {"exchange": "kraken"},
        {"offset": 2**63 - 1},
    ]

    try:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client,
        ):
            task: asyncio.Task[Any] = app.state.accounting_startup_task
            await asyncio.wait_for(until(task.done), timeout=5)
            factory = app.state.db_sessionmaker
            await plant_history(factory, await user_id_of(factory), the_book())
            await sign_in(client)
            capsys.readouterr()
            records.rendered.clear()

            answered = [
                (await client.get(FILLS_PATH, params=params)).status_code for params in requests
            ]
            served = await client.get(FILLS_PATH)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url=BASE_URL
            ) as anonymous:
                refused = await anonymous.get(FILLS_PATH, params={"exchange": "bitget"})
            written = capsys.readouterr().out
            searched = "\n".join([written, *records.rendered])
    finally:
        logging.getLogger().removeHandler(records)

    # The controls: every request was answered as expected, and the order ids and the amount
    # really were served, so their absence from the log means something.
    assert answered == [200, 200, 200, 200, 200, 422, 422, 422, 200]
    assert served.status_code == 200
    assert all(order_id in served.text for order_id in ORDER_ID_SENTINELS)
    assert AMOUNT_SENTINEL in served.text
    assert refused.status_code == 401
    # The positive companion: the capture is the log that ran.
    assert_carried_something(written, marker="request_refused")
    assert FILLS_PATH in written, "the refusal names the path"
    # The claim.
    assert leaks(searched) == []
