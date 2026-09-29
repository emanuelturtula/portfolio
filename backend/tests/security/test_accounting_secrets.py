"""#19 and rule 3: a fill's payload and its trade id reach no log line and no response.

A recompute reads the owner's fills. Spec 021 keeps two things of theirs out of everything it
produces:

* **`raw_payload`** is never loaded at all -- the repository selects its columns explicitly
  (`tests/services/test_accounting.py` inspects the compiled statement and every statement
  sent);
* **trade ids** stay in the tables: the warnings the endpoint serves carry no `external_id`,
  and an `UnconvertibleFillError` carries the account and the trade id as attributes, never
  in its message -- because the message is what the trigger's failure path would log.

This drives both through the **real application with the production JSON logging**, a
successful recompute and a failing one, and searches every byte on stdout, every
standard-library record, and every response body for the sentinels. What the test itself
writes to plant its rows is discarded before each search: it is the recompute and the
endpoint that are under test. The positive companions come first, so an empty capture
cannot pass: the recompute's own log lines must be there.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from portfolio.domain.exchanges import FillSide
from portfolio.main import create_app, run_accounting_recompute
from portfolio.services.accounting import RecomputeOutcome, RecomputeReason
from tests.accounting_harness import (
    plant_account,
    plant_fills,
    plant_unconvertible_fill,
)
from tests.auth.conftest import BASE_URL, sign_in
from tests.exchange_sync_harness import make_fill
from tests.security.conftest import assert_carried_something

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from tests.security.conftest import ProductionLoggingInstaller

#: Built at run time from short cycles, so no literal here looks like anything to a scanner.
PAYLOAD_SENTINEL: Final = "pl-" + "J6w" * 6
LOT_TRADE_SENTINEL: Final = "tid-" + "Mb4" * 5
WARNED_TRADE_SENTINEL: Final = "tid-" + "Hq2" * 5
BAD_TRADE_SENTINEL: Final = "tid-" + "Vx9" * 5
SENTINELS: Final = (
    PAYLOAD_SENTINEL,
    LOT_TRADE_SENTINEL,
    WARNED_TRADE_SENTINEL,
    BAD_TRADE_SENTINEL,
)


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


async def test_a_fills_payload_and_trade_ids_reach_no_log_line_and_no_response(
    api_environment: Path,
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the production log level, over a written recompute and a failed one."""
    del api_environment
    monkeypatch.setattr("portfolio.main.exchange_providers", lambda client, **_: {})
    now = datetime.now(UTC).replace(microsecond=0)
    # A buy, whose trade id becomes a lot's `external_id`, and a sale of more than it bought,
    # so the snapshot has a warning to serve for that very fill. Both carry the payload.
    payload = f'{{"note":"{PAYLOAD_SENTINEL}"}}'
    bought = replace(
        make_fill(1, now - timedelta(hours=2), raw_payload=payload),
        external_trade_id=LOT_TRADE_SENTINEL,
    )
    oversold = replace(
        make_fill(
            2, now - timedelta(hours=1), side=FillSide.SELL, quantity="2", raw_payload=payload
        ),
        external_trade_id=WARNED_TRADE_SENTINEL,
    )
    app = create_app()
    production_logging()
    records = EveryRecord()
    logging.getLogger().addHandler(records)
    searched: list[str] = []
    bodies: list[str] = []

    def keep_and_reset() -> str:
        output = capsys.readouterr().out
        searched.append(output)
        searched.extend(records.rendered)
        records.rendered.clear()
        return output

    def discard() -> None:
        capsys.readouterr()
        records.rendered.clear()

    try:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client,
        ):
            task: asyncio.Task[Any] = app.state.accounting_startup_task
            await asyncio.wait_for(until(task.done), timeout=5)
            await sign_in(client)
            async with app.state.db_sessionmaker() as session:
                user_id = int(
                    await session.scalar(text("SELECT id FROM users WHERE username = 'owner'"))
                )
                account = await plant_account(session, user_id)
                await plant_fills(session, account, [bought, oversold])
            discard()

            written = await run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC)
            bodies.append((await client.get("/api/accounting/positions")).text)
            first = keep_and_reset()

            async with app.state.db_sessionmaker() as session:
                await plant_unconvertible_fill(
                    session, account, trade_id=BAD_TRADE_SENTINEL, shape="rebate_exceeds_given"
                )
            discard()

            failed = await run_accounting_recompute(app, RecomputeReason.EXCHANGE_SYNC)
            bodies.append((await client.get("/api/accounting/positions")).text)
            second = keep_and_reset()

            async with app.state.db_sessionmaker() as session:
                stored_lot_ids = list(
                    await session.scalars(text("SELECT external_id FROM accounting_lots"))
                )
                stored_payloads = list(
                    await session.scalars(text("SELECT raw_payload FROM exchange_fills"))
                )
    finally:
        logging.getLogger().removeHandler(records)

    # The positive companions: both recomputes ran, logged, and were served.
    assert written.outcome is RecomputeOutcome.WRITTEN
    assert (failed.outcome, failed.error) == (RecomputeOutcome.FAILED, "UnconvertibleFillError")
    assert_carried_something(first, marker="accounting_recompute_finished")
    assert_carried_something(second, marker="accounting_recompute_failed")
    assert "UnconvertibleFillError" in second
    assert '"negative_inventory"' in bodies[0], "the oversold fill's warning was served"
    assert '"failed"' in bodies[1]
    assert any(PAYLOAD_SENTINEL in stored for stored in stored_payloads), "payload stored"
    assert stored_lot_ids == [LOT_TRADE_SENTINEL], "the lot really holds a trade id"
    # The claim.
    assert leaks("\n".join([*searched, *bodies])) == []
