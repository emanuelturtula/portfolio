"""The suite does not talk to anybody, and this is what makes that a check rather than a habit.

#10 is the change that made this necessary. Before it, nothing in this application initiated
a request: the providers existed and had no caller, so a test could not accidentally reach a
vendor. Now the lifespan starts a scheduler whose first tick happens *at startup* -- because
a database created a moment ago has no finished run to suppress it -- and every suite that
enters the real lifespan with a wallet registered would send that wallet's address to
`mempool.space` and `api.kaspa.org`, and its price pairs to Kraken.

Two things would be wrong at once, and the second is worse than the first:

* the suite would depend on somebody else's uptime, so a red build would sometimes mean a
  vendor was having an afternoon;
* every CI run would put the testnet addresses this public repository ships into a third
  party's request log, at whatever rate the pipeline happens to run -- against indexes that
  document a ban as the consequence of asking too often.

A suite that quietly depends on the internet passes on a laptop and fails in CI, and by then
the thing it is failing about is not the change under review. So the guard is mechanical.

## Two guards, and neither is enough on its own

`test_the_shared_environment_leaves_no_schedule_running` reads the *configuration* the shared
fixtures produce. It is specific and it names the switch, so a failure says exactly what to
fix -- and it is blind to a suite that builds its own environment.

`test_entering_the_lifespan_opens_no_socket` blocks the two calls every outbound connection
in this process has to make and then runs a real startup. It covers what the first cannot see
and it says much less about why, which is why both are here.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, Any, Final

import pytest

from portfolio.config import Settings, get_settings
from portfolio.main import create_app
from tests.auth.conftest import apply_auth_environment

if TYPE_CHECKING:
    from pathlib import Path

#: The settings that decide whether a lifespan reaches a vendor. Both default to `true`,
#: which is right in production and is exactly why a test environment has to say otherwise.
SCHEDULE_SWITCHES: Final = ("balance_sync_enabled", "price_refresh_enabled")


class ReachedTheNetworkError(AssertionError):
    """Raised in place of a connection, so a failure names the host rather than timing out."""


@pytest.fixture
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any outbound connection an immediate, readable failure.

    `getaddrinfo` and `socket.connect` are the two chokepoints: every client in this process
    -- `httpx`, `anyio`, anything a dependency reaches for -- resolves a name and then
    connects, and neither can be skipped. Blocking them rather than mocking `httpx` is
    deliberate: a guard written against one library stops covering the day somebody adds
    another.

    Nothing legitimate in this suite passes through either. SQLite is a file, and the ASGI
    transport hands requests to the application in process without binding a port.
    """

    def refuse(*arguments: Any, **keywords: Any) -> Any:
        del keywords
        target = arguments[0] if arguments else "an unknown host"
        message = (
            f"the test suite tried to reach {target!r}. Nothing here may talk to a vendor: "
            "see this module's docstring"
        )
        raise ReachedTheNetworkError(message)

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def test_the_shared_environment_leaves_no_schedule_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every suite built on `apply_auth_environment` has both schedules switched off.

    Asserted on a `Settings` built from the environment that helper arranges, rather than on
    the text of the helper, because what matters is the value the application reads. The
    production defaults are asserted alongside it so the test says what it is protecting
    against: these are `True` where nobody has said otherwise, and that is correct.
    """
    try:
        apply_auth_environment(monkeypatch, tmp_path)
        settings = Settings()
    finally:
        get_settings.cache_clear()

    for switch in SCHEDULE_SWITCHES:
        assert getattr(settings, switch) is False, switch


def test_the_production_default_is_the_opposite_and_that_is_why_this_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control. If both schedules were off by default the test above would prove nothing.

    A deployment that configures nothing has to sync, or the product does not work; the test
    environment is the exception and it has to be a written one.
    """
    for switch in SCHEDULE_SWITCHES:
        monkeypatch.delenv(f"PORTFOLIO_{switch.upper()}", raising=False)

    settings = Settings()

    for switch in SCHEDULE_SWITCHES:
        assert getattr(settings, switch) is True, switch


async def test_entering_the_lifespan_opens_no_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_sockets: None,
) -> None:
    """A real startup and shutdown, with every outbound connection blocked.

    This is the one that covers a suite which builds its own environment and forgets the
    switch, and a future startup step that calls a vendor for a reason nobody anticipated.
    It runs the whole lifespan -- migrations, the bootstrap, the sweep, the client, both
    schedulers -- and asserts that not one of them resolved a hostname.

    A wallet is deliberately **not** registered: with none, a startup sync has nothing to
    ask about and would pass this even with the schedule on. The stronger claim is the one
    in `tests/db/test_lifespan.py`, which registers a wallet and stubs the registry; this is
    the floor under every other suite rather than a test of the sync.
    """
    del no_sockets  # Ordering only: the fixture is the whole point of the test.
    apply_auth_environment(monkeypatch, tmp_path)
    try:
        app = create_app()
        async with app.router.lifespan_context(app):
            assert app.state.http_client.is_closed is False
    finally:
        get_settings.cache_clear()


async def test_the_socket_guard_can_actually_fail(no_sockets: None) -> None:
    """The falsification control: a guard that blocked nothing would pass the test above.

    Driven against the resolver rather than a real connection, so this test does not need a
    hostname -- and does not become the one place in the suite that names one, which rule 3
    forbids.
    """
    del no_sockets

    with pytest.raises(ReachedTheNetworkError):
        socket.getaddrinfo("a-host-that-is-never-resolved.invalid", 443)
