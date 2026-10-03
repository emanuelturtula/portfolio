"""Spec 029 (#22), criterion 7: a restore performed end to end, on every run of the suite.

Nothing is replaced. A real application runs its real lifespan on a temporary database
**file**; the owner signs in; wallets and manual adjustments are created through the API; a
copy is taken by the running application's own backup service, while its connections are
open; the data is then changed and deleted through the API; the application stops; the
operator's command, `restore-backup`, runs through `portfolio.cli.main`; and a new
application, started on the same file, serves exactly what the API served at the backup.

Then the restore is undone with the same command, restoring the safety copy it printed, and
the API serves the changed data again. That is the claim the documentation makes about a
safety copy, and a claim nobody has exercised is a guess.

**What "exactly" means here.** Three reads are compared as parsed JSON: the wallets with
the archived ones, the manual adjustments, and the positions. The position snapshot is
recomputed at startup, and a recompute over the same events writes nothing new, so it is
part of what must come back unchanged -- all of it but `last_recompute`, which is this
process's memory of its own last recompute (spec 021), not data in the file, and which a
new process starts afresh by design.

The command line runs in a worker thread, because `cli.main` calls `asyncio.run` and this
test's loop is already running. That is also how it runs on the Pi: in a process of its own,
with the application stopped.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Final

from anyio import to_thread
from httpx import ASGITransport, AsyncClient

from portfolio import cli
from portfolio.config import get_settings
from portfolio.main import create_app
from tests.address_vectors import BIP173_TESTNET_P2WPKH, BIP173_TESTNET_P2WSH, KASPA_TESTNET_V0
from tests.adjustments_harness import ADJUSTMENTS_PATH, POSITIONS_PATH, body
from tests.auth.conftest import BASE_URL, JSON_HEADERS, apply_auth_environment, sign_in
from tests.backup_harness import row_counts, sidecars_of, table_contents
from tests.logging_harness import preserved_logging

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    import pytest
    from fastapi import FastAPI

WALLETS: Final = "/api/wallets"
SETTLE_SECONDS: Final = 10

#: A log record on stdout, as the development renderer writes one. The command's own lines
#: are everything else.
LOG_LINE: Final = re.compile(r"^\S+Z \[\w+\s*\] ")

SAFETY_LINE: Final = re.compile(
    r"The database as it was before is in the safety copy "
    r"(portfolio-\d{8}T\d{12}Z\.sqlite3)\."
)


async def settled(app: FastAPI) -> None:
    """Wait for the startup recompute, so a read does not race the snapshot it writes."""
    task: asyncio.Task[Any] = app.state.accounting_startup_task
    await asyncio.wait({task}, timeout=SETTLE_SECONDS)
    assert task.done(), "the startup recompute did not finish"


def running(app: FastAPI) -> AbstractAsyncContextManager[Any]:
    return app.router.lifespan_context(app)


@asynccontextmanager
async def signed_in(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        await sign_in(client)
        yield client


async def ok(response_awaitable: Any, status: int = 200) -> Any:
    response = await response_awaitable
    assert response.status_code == status, response.text
    return None if status == 204 else response.json()


async def served(client: AsyncClient) -> dict[str, Any]:
    """What the owner sees: every wallet, every adjustment, and the stored positions."""
    positions = await ok(client.get(POSITIONS_PATH))
    assert set(positions["last_recompute"]) == {"at", "error", "outcome"}
    del positions["last_recompute"]
    return {
        "wallets": await ok(client.get(WALLETS, params={"include_archived": "true"})),
        "adjustments": await ok(client.get(ADJUSTMENTS_PATH)),
        "positions": positions,
    }


def command(argv: list[str]) -> Callable[[], int]:
    def run() -> int:
        with preserved_logging():
            return cli.main(argv)

    return run


def own_lines(stream: str) -> list[str]:
    """The command's lines, without the log records the same stream carries."""
    return [line for line in stream.splitlines() if line and not LOG_LINE.match(line)]


async def restore_through_the_command_line(
    name: str, capsys: pytest.CaptureFixture[str]
) -> tuple[list[str], str]:
    """Run `restore-backup NAME` as the operator would, and return its lines and stderr."""
    capsys.readouterr()
    exit_code = await to_thread.run_sync(command(["restore-backup", name]))
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    assert captured.err == ""
    return own_lines(captured.out), captured.err


async def test_a_backup_restored_through_the_command_line_brings_back_what_the_api_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = apply_auth_environment(monkeypatch, tmp_path)
    try:
        # 1. A real application on a file, data through the API, and a copy taken by the
        #    running application while its own connections are open.
        first = create_app()
        async with running(first):
            await settled(first)
            async with signed_in(first) as client:
                cold = await ok(
                    client.post(
                        WALLETS,
                        json={
                            "chain_key": "bitcoin",
                            "address": BIP173_TESTNET_P2WPKH,
                            "label": "Cold storage",
                        },
                        headers=JSON_HEADERS,
                    ),
                    201,
                )
                hot = await ok(
                    client.post(
                        WALLETS,
                        json={"chain_key": "kaspa", "address": KASPA_TESTNET_V0, "label": "Kaspa"},
                        headers=JSON_HEADERS,
                    ),
                    201,
                )
                opening = await ok(
                    client.post(
                        ADJUSTMENTS_PATH,
                        json=body(quantity="0.5", unit_cost="30000"),
                        headers=JSON_HEADERS,
                    ),
                    201,
                )
                kaspa = await ok(
                    client.post(
                        ADJUSTMENTS_PATH,
                        json=body(asset="KAS", quantity="1000", unit_cost="0.1", note="Mined"),
                        headers=JSON_HEADERS,
                    ),
                    201,
                )
                at_the_backup = await served(client)
                taken = await first.state.backup_service.take()

                # 2. Changed and deleted after the copy, through the API.
                await ok(
                    client.patch(
                        f"{WALLETS}/{cold['id']}",
                        json={"label": "Renamed after"},
                        headers=JSON_HEADERS,
                    )
                )
                await ok(client.delete(f"{WALLETS}/{hot['id']}", headers=JSON_HEADERS), 204)
                await ok(
                    client.post(
                        WALLETS,
                        json={"chain_key": "bitcoin", "address": BIP173_TESTNET_P2WSH},
                        headers=JSON_HEADERS,
                    ),
                    201,
                )
                await ok(
                    client.delete(f"{ADJUSTMENTS_PATH}/{opening['id']}", headers=JSON_HEADERS), 204
                )
                await ok(
                    client.put(
                        f"{ADJUSTMENTS_PATH}/{kaspa['id']}",
                        json=body(asset="KAS", quantity="2500", unit_cost="0.2", note="Changed"),
                        headers=JSON_HEADERS,
                    )
                )
                after_the_changes = await served(client)
        assert after_the_changes != at_the_backup
        for part in ("wallets", "adjustments", "positions"):
            assert after_the_changes[part] != at_the_backup[part], part

        # 3. The application has stopped, and its engine closed the database cleanly: nothing
        #    is beside it that would make the restore refuse.
        assert sidecars_of(database) == []
        backups = database.parent / "backups"
        before_restore = table_contents(database)

        lines, _ = await restore_through_the_command_line(taken.name, capsys)

        safety = re.fullmatch(SAFETY_LINE, lines[1])
        assert safety is not None, lines
        counts = row_counts(backups / taken.name)
        assert lines == [
            f"Restored {taken.name}.",
            lines[1],
            "Rows per table after the restore:",
            *[f"  {table}: {rows}" for table, rows in counts.items()],
        ]
        assert table_contents(backups / safety.group(1)) == before_restore
        assert sidecars_of(database) == []

        # 4. A new application on the same file serves exactly what was served at the backup.
        second = create_app()
        async with running(second):
            await settled(second)
            async with signed_in(second) as client:
                assert await served(client) == at_the_backup

        # 5. Undone with the same command: the safety copy brings the changes back.
        lines, _ = await restore_through_the_command_line(safety.group(1), capsys)
        assert lines[0] == f"Restored {safety.group(1)}."
        third = create_app()
        async with running(third):
            await settled(third)
            async with signed_in(third) as client:
                assert await served(client) == after_the_changes
    finally:
        get_settings.cache_clear()
