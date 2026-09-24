"""The HTTP client the application gets in every suite that is not about the HTTP client.

## Why the suite needs its own

#10's lifespan builds the shared `httpx.AsyncClient` on every startup, which in production is
once per process and costs nothing worth measuring. In this suite it is once per test that
enters the lifespan -- several hundred of them -- and it is not cheap: `httpx` builds an SSL
context and loads certifi's CA bundle when it constructs its transport. Measured on the
development machine, ten iterations each:

| Built by | Cost |
|---|---|
| `build_http_client()` | 117 ms |
| `build_http_client(transport=httpx.MockTransport(...))` | 0.01 ms |

That one line took `tests/auth` from 3 s to 12 s with the test bodies unchanged: the time
was all in fixture setup. So suites that only need the application *running* get a client
built over a `MockTransport`, and pay nothing.

## Why it refuses rather than answers

The handler raises on every request, naming the host. A client that answered with an
empty `200` would be faster to write and would turn a stray vendor call into a plausible
zero balance; one that refuses turns it into a failed test with the host in the message.
That makes this a second no-network layer under `tests/test_no_network.py`: the schedule
switches in `tests/auth/conftest.py` stop the application *deciding* to call a vendor, and
this stops the call *arriving* anywhere even if something decides to anyway.

The message names the host and nothing else. A chain provider puts the address in the
path -- Esplora's is `/address/{address}` -- and an assertion message is a string that ends up
in CI output, so the rule every other message in this application follows applies here too.

## What it is not

The builder is still `build_http_client`, with the retrying transport, the rate limiter
and the timeouts all in place: only the innermost transport is swapped. A suite that stubs
a provider above the client never notices, and one that reached through it would meet the
same wiring production has, up to the point where a socket would have been opened.

It is **not** applied where the real client is the subject. `tests/test_no_network.py`
exists to prove that the real lifespan with the real client opens no socket, and the
lifespan tests that check the client is built and closed need the one production builds.
Those use `the_real_http_client`, which also records that the real builder actually ran --
so they cannot quietly pass on the offline one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import httpx

from portfolio.providers.http import build_http_client

if TYPE_CHECKING:
    import pytest

#: The real builder, captured from the module that defines it rather than from
#: `portfolio.main`, whose attribute is exactly what the helpers below replace.
REAL_BUILD_HTTP_CLIENT: Final = build_http_client

#: Where the lifespan looks the builder up. `main` imports the name, so patching the
#: definition in `providers.http` would change nothing the lifespan calls.
LIFESPAN_BUILDER: Final = "portfolio.main.build_http_client"


class ReachedAVendorError(AssertionError):
    """A request left the application in a suite that promised it never would."""


def refuse_every_request(request: httpx.Request) -> httpx.Response:
    """The transport's handler: every request is a failed test, naming only the host."""
    message = (
        f"a request to {request.url.host!r} reached the offline HTTP client. Nothing in this "
        "suite may talk to a vendor; stub the provider or the price source above the client."
    )
    raise ReachedAVendorError(message)


def offline_http_client() -> httpx.AsyncClient:
    """`build_http_client` exactly as production calls it, over a transport that refuses."""
    return REAL_BUILD_HTTP_CLIENT(transport=httpx.MockTransport(refuse_every_request))


def use_an_offline_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the lifespan build `offline_http_client` for the rest of this test."""
    monkeypatch.setattr(LIFESPAN_BUILDER, offline_http_client)


def the_real_http_client(monkeypatch: pytest.MonkeyPatch) -> list[httpx.AsyncClient]:
    """Give the lifespan the real builder back, and record every client it builds with it.

    For the tests whose subject is the real client. The list is the proof: a test asserts
    it has one entry and that the entry is the client on `app.state`, so an ordering
    mistake that left the offline builder in place fails there instead of passing on a
    client that was never the real one.
    """
    built: list[httpx.AsyncClient] = []

    def build() -> httpx.AsyncClient:
        client = REAL_BUILD_HTTP_CLIENT()
        built.append(client)
        return client

    monkeypatch.setattr(LIFESPAN_BUILDER, build)
    return built
