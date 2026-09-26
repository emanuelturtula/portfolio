"""The suite does not talk to anybody, and this is what makes that a check rather than a habit.

#10 is the change that made this necessary. Before it, nothing in this application initiated
a request: the providers existed and had no caller, so a test could not accidentally reach a
vendor. Now the lifespan starts a scheduler whose first tick happens *at startup* -- because
a database created a moment ago has no finished run to suppress it -- and every suite that
enters the real lifespan with a wallet registered would send that wallet's address to
the two public chain indexes the providers default to, and its price pairs to the
key-free price vendor.

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

import asyncio
import socket
from typing import TYPE_CHECKING, Any, Final

import pytest
from structlog.testing import capture_logs

from portfolio.config import Settings, get_settings
from portfolio.main import create_app
from tests.address_vectors import BIP173_TESTNET_P2WPKH
from tests.auth.conftest import apply_auth_environment
from tests.offline_http import (
    ReachedAVendorError,
    take_offline_attempts,
    the_real_http_client,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from fastapi import FastAPI

#: The settings that decide whether a lifespan reaches a vendor. All three default to `true`,
#: which is right in production and is exactly why a test environment has to say otherwise.
#: The exchange timer also needs a configured venue, and a developer's `.env` can supply one.
SCHEDULE_SWITCHES: Final = (
    "balance_sync_enabled",
    "price_refresh_enabled",
    "exchange_sync_enabled",
)


class ReachedTheNetworkError(AssertionError):
    """Raised in place of a connection, so a failure names the host rather than timing out."""


#: The addresses a test is allowed to reach. `asyncio`'s own event loop builds a loopback
#: socket pair for its self-pipe on Windows, and `httpx`'s ASGI transport never leaves the
#: process at all -- so a guard that refused *every* connection would refuse the machinery
#: running the test rather than the vendor call it is aimed at.
LOOPBACK: Final = frozenset({"127.0.0.1", "::1", "localhost", "", None})


def _host_of(address: object) -> object:
    """The host out of whatever shape a socket call was handed it in.

    `asyncio` hands `getaddrinfo` the host as IDNA-encoded **bytes**, so a real vendor call
    arrives as `b'...'`; decoded here so the record reads as a hostname rather than as the
    repr of one.
    """
    host = address[0] if isinstance(address, tuple) and address else address
    if isinstance(host, bytes):
        return host.decode("ascii", errors="replace")
    return host


class SocketAttempts:
    """Every host the guard refused during one test, recorded as well as raised.

    Raising alone was measured not to be enough: `ReachedTheNetworkError` is an
    `AssertionError`, which is an `Exception`, and `IntervalScheduler._tick` catches
    `Exception` on purpose. With a timer switched on and the real client, the guard fired,
    `scheduler_tick_failed` was logged, and the test passed. The record is what the fixture's
    teardown checks, so a refusal something swallowed still fails the test that caused it.
    Hosts only: a path is where a chain provider puts an address.
    """

    def __init__(self) -> None:
        self.hosts: list[str] = []

    def take(self) -> list[str]:
        """The hosts recorded so far, forgotten, for a test that expected them."""
        taken = list(self.hosts)
        self.hosts.clear()
        return taken


@pytest.fixture
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> Iterator[SocketAttempts]:
    """Make any connection to anything but the loopback an immediate, readable failure.

    `getaddrinfo` and `connect` are the two chokepoints: every client in this process --
    `httpx`, `anyio`, anything a dependency reaches for -- resolves a name and then connects,
    and neither can be skipped. Blocking those rather than mocking `httpx` is deliberate: a
    guard written against one library stops covering the day somebody adds another.

    The loopback is allowed because refusing it would break the event loop rather than the
    test's subject, and nothing reachable on it is a vendor. A name that has to be resolved
    is by definition not the loopback, so the resolver check is the one doing the work.
    """

    attempts = SocketAttempts()

    def refuse(what: str, target: object) -> None:
        attempts.hosts.append(str(_host_of(target)))
        message = (
            f"the test suite tried to {what} {target!r}. Nothing here may talk to a vendor: "
            "see this module's docstring"
        )
        raise ReachedTheNetworkError(message)

    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect

    def guarded_getaddrinfo(host: Any, *arguments: Any, **keywords: Any) -> Any:
        if _host_of(host) not in LOOPBACK:
            refuse("resolve", host)
        return real_getaddrinfo(host, *arguments, **keywords)

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if _host_of(address) not in LOOPBACK:
            refuse("connect to", address)
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    yield attempts
    leftover = attempts.take()
    assert leftover == [], (
        f"the socket guard refused {leftover!r} during this test and something caught it"
    )


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
    no_sockets: SocketAttempts,
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

    **What makes it able to fail.** Both timers are off in this environment, so on its own
    this could only ever pass; two things change that. The guard's teardown fails the test
    if any connection was refused, even one something caught -- which
    `test_a_vendor_call_a_timer_swallows_still_fails_the_test` proves is real. And the two
    timers are asserted to be absent rather than merely idle, which is what fails if either
    one starts ignoring its switch: a timer that exists but has not ticked yet would
    otherwise leave nothing for the guard to see before the lifespan exits.
    """
    apply_auth_environment(monkeypatch, tmp_path)
    # The shared environment installs an offline client that could never open a socket,
    # which would make this test pass by construction. The real builder is put back, and
    # `built` proves it ran: the claim is about the client production builds.
    built = the_real_http_client(monkeypatch)
    try:
        app = create_app()
        async with app.router.lifespan_context(app):
            assert built == [app.state.http_client], "the real client, built once"
            assert app.state.http_client.is_closed is False
            assert app.state.balance_scheduler is None, "the balance timer ignored its switch"
            assert app.state.price_scheduler is None, "the price timer ignored its switch"
            assert app.state.exchange_scheduler is None, "the exchange timer ignored its switch"
    finally:
        get_settings.cache_clear()

    assert no_sockets.hosts == [], "no connection was attempted, caught or not"


async def until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until `condition` holds. Bounded by the caller's `wait_for`.

    A real sleep rather than a bare checkpoint, unlike its namesake in `test_lifespan.py`:
    what is being waited for here happens in the resolver's worker thread, and a loop that
    only ever checkpoints would spin without giving that thread's result a chance to land.
    """
    while not condition():  # noqa: ASYNC110
        await asyncio.sleep(0.01)


async def test_a_vendor_call_a_timer_swallows_still_fails_the_test(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_sockets: SocketAttempts,
) -> None:
    """The control the test above depends on: a swallowed vendor call is still seen.

    The price timer is switched on and the real client is used, so the startup refresh
    really does try to resolve a vendor. The guard refuses it, the refusal propagates up to
    `IntervalScheduler._tick`, and `_tick` catches it and logs `scheduler_tick_failed` --
    correctly: a failed tick must not end the schedule. The lifespan then exits cleanly.

    That is exactly the sequence that let the old version of the guard pass while a vendor
    was being called. What this asserts is that the record saw it anyway, so the teardown
    would have failed the test. The record is taken here, because in this test it is
    expected; the hosts are deliberately not asserted by name, so that no vendor hostname has
    to be written into the repository to prove the point.
    """
    apply_auth_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("PORTFOLIO_PRICE_REFRESH_ENABLED", "true")
    get_settings.cache_clear()
    the_real_http_client(monkeypatch)
    try:
        app = create_app()
        with capture_logs() as captured:
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(
                    until(
                        lambda: any(entry["event"] == "scheduler_tick_failed" for entry in captured)
                    ),
                    timeout=10,
                )
    finally:
        get_settings.cache_clear()

    assert no_sockets.take() != [], "the guard saw the vendor call the timer swallowed"
    failed_ticks = [entry for entry in captured if entry["event"] == "scheduler_tick_failed"]
    assert failed_ticks[0]["scheduler"] == "price-refresh", "and it was the tick that hid it"


async def test_the_shared_fixtures_give_the_application_a_client_that_refuses(
    api_app: FastAPI,
) -> None:
    """The second layer, on the application every API suite actually uses.

    `api_app` is built on `apply_auth_environment`, so this is the client `tests/api/`,
    `tests/auth/` and `tests/security/` run against. A request through it fails at once and
    names only the host -- never the path, which is where a chain provider puts an address.
    """
    with pytest.raises(ReachedAVendorError) as caught:
        await api_app.state.http_client.get(
            f"https://an-index.invalid/address/{BIP173_TESTNET_P2WPKH}"
        )

    assert "an-index.invalid" in str(caught.value)
    assert BIP173_TESTNET_P2WPKH not in str(caught.value)
    assert take_offline_attempts() == ["an-index.invalid"], "recorded once, host only"


async def test_the_socket_guard_can_actually_fail(no_sockets: SocketAttempts) -> None:
    """The falsification control: a guard that blocked nothing would pass the test above.

    Driven against the resolver and against a connection to a documentation address, so both
    halves are shown to fire. `.invalid` is reserved by RFC 2606 and `192.0.2.1` by RFC 5737,
    so neither is a real host and rule 3 is untouched -- and the loopback is checked to still
    work, because a guard that refused everything would break the event loop rather than the
    thing it is aimed at.

    Both refusals are recorded, by host and nothing else, and the loopback lookup is not.
    """
    with pytest.raises(ReachedTheNetworkError):
        socket.getaddrinfo("a-host-that-is-never-resolved.invalid", 443)

    with pytest.raises(ReachedTheNetworkError), socket.socket() as opened:
        opened.connect(("192.0.2.1", 443))

    # The loopback is deliberately still reachable, and it has to be: this test runs inside
    # an event loop that built itself a socket pair over it.
    assert socket.getaddrinfo("127.0.0.1", 0)
    assert no_sockets.take() == ["a-host-that-is-never-resolved.invalid", "192.0.2.1"]
