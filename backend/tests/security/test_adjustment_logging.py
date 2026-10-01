"""#18, criterion 9 and rule 3: nothing the owner types into an adjustment reaches a log line.

An adjustment carries the owner's holdings -- an asset, a quantity, a unit cost, the date they
were acquired -- and a note in the owner's own words. Spec 023, *Logging*: the changes are
logged by id only, and never the asset, an amount, a date or the note.

This drives a create, a replacement, a refused create and a delete through the **real
application with the production JSON logging**, and searches every byte on stdout and every
standard-library record for each value it entered. What the test itself writes while it sets
up is discarded before each search. The positive companions come first, so an empty capture
cannot pass: each change's own line, with its id, and the recompute it set off, must be there.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from typing import TYPE_CHECKING, Any, Final

from httpx import ASGITransport, AsyncClient

from portfolio.main import create_app
from tests.adjustments_harness import ADJUSTMENTS_PATH, body
from tests.auth.conftest import BASE_URL, JSON_HEADERS, sign_in
from tests.security.conftest import assert_carried_something

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from tests.security.conftest import ProductionLoggingInstaller

#: Built at run time from short cycles, so no literal here looks like anything to a scanner.
NOTE_SENTINEL: Final = "note-" + "Tz6" * 6
REPLACED_NOTE_SENTINEL: Final = "note-" + "Gb3" * 6
REFUSED_NOTE_SENTINEL: Final = "note-" + "Yw1" * 6
ASSET_SENTINEL: Final = "QZ" + "XV" * 3
REPLACED_ASSET_SENTINEL: Final = "KQ" + "JW" * 3
QUANTITY_SENTINEL: Final = "7314.159265"
COST_SENTINEL: Final = "2718.281828"
REPLACED_QUANTITY_SENTINEL: Final = "4669.201609"
DATE_SENTINEL: Final = "2024-02-29"
TIME_SENTINEL: Final = "13:57:11"
SENTINELS: Final = (
    NOTE_SENTINEL,
    REPLACED_NOTE_SENTINEL,
    REFUSED_NOTE_SENTINEL,
    ASSET_SENTINEL,
    REPLACED_ASSET_SENTINEL,
    QUANTITY_SENTINEL,
    COST_SENTINEL,
    REPLACED_QUANTITY_SENTINEL,
    DATE_SENTINEL,
    TIME_SENTINEL,
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


async def test_no_note_amount_asset_or_date_reaches_a_log_line(
    api_environment: Path,
    production_logging: ProductionLoggingInstaller,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the production log level, over a create, a replacement, a refusal and a delete."""
    del api_environment
    monkeypatch.setattr("portfolio.main.exchange_providers", lambda client, **_: {})
    app = create_app()
    production_logging()
    records = EveryRecord()
    logging.getLogger().addHandler(records)
    searched: list[str] = []
    outputs: dict[str, str] = {}

    def keep(name: str) -> None:
        output = capsys.readouterr().out
        outputs[name] = output
        searched.append(output)
        searched.extend(records.rendered)
        records.rendered.clear()

    def discard() -> None:
        capsys.readouterr()
        records.rendered.clear()

    entered: dict[str, Any] = body(
        asset=ASSET_SENTINEL,
        quantity=QUANTITY_SENTINEL,
        unit_cost=COST_SENTINEL,
        occurred_at=f"{DATE_SENTINEL}T{TIME_SENTINEL}Z",
        note=NOTE_SENTINEL,
    )
    replacement: dict[str, Any] = body(
        asset=REPLACED_ASSET_SENTINEL,
        quantity=REPLACED_QUANTITY_SENTINEL,
        unit_cost=None,
        occurred_at=f"{DATE_SENTINEL}T{TIME_SENTINEL}+00:00",
        note=REPLACED_NOTE_SENTINEL,
    )
    refused: dict[str, Any] = body(
        asset=ASSET_SENTINEL.lower(), quantity=QUANTITY_SENTINEL, note=REFUSED_NOTE_SENTINEL
    )

    try:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client,
        ):
            task: asyncio.Task[Any] = app.state.accounting_startup_task
            await asyncio.wait_for(until(task.done), timeout=5)
            await sign_in(client)
            discard()

            created = await client.post(ADJUSTMENTS_PATH, json=entered, headers=JSON_HEADERS)
            keep("create")
            identifier = created.json()["id"]
            replaced = await client.put(
                f"{ADJUSTMENTS_PATH}/{identifier}", json=replacement, headers=JSON_HEADERS
            )
            keep("replace")
            refusal = await client.post(ADJUSTMENTS_PATH, json=refused, headers=JSON_HEADERS)
            keep("refuse")
            deleted = await client.delete(f"{ADJUSTMENTS_PATH}/{identifier}", headers=JSON_HEADERS)
            keep("delete")
    finally:
        logging.getLogger().removeHandler(records)

    # The positive companions: each change happened, was logged by id, and recomputed.
    assert (created.status_code, replaced.status_code) == (201, 200), created.text
    assert (refusal.status_code, deleted.status_code) == (422, 204)
    for name, event in (
        ("create", "adjustment_created"),
        ("replace", "adjustment_updated"),
        ("delete", "adjustment_deleted"),
    ):
        assert_carried_something(outputs[name], marker=event)
        assert f'"adjustment_id": {identifier}' in outputs[name] or (
            f'"adjustment_id":{identifier}' in outputs[name]
        ), outputs[name]
        assert_carried_something(outputs[name], marker="accounting_recompute_finished")
        assert '"adjustment"' in outputs[name], "the recompute names its reason"
    assert "accounting_recompute" not in outputs["refuse"], "a refusal recomputes nothing"
    # The control: the values really were stored and served, so their absence means something.
    assert NOTE_SENTINEL in created.text
    assert REPLACED_ASSET_SENTINEL in replaced.text
    # The claim.
    assert leaks("\n".join(searched)) == []
