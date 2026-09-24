"""Several interchangeable instances of one vendor's API, and the failover loop over them.

A chain provider that reads a public index wants a second index behind the first, and the
rule for moving between them is not obvious enough to be re-derived correctly. #7 wrote it
inside `chains/bitcoin.py` and review corrected it there; #8 is the second provider, so
this is where the rule stops being a copy.

## Why the extraction happened here rather than with the first provider

With one provider the shared part was a guess; with two it is observable. What is genuinely
common turns out to be exactly the part review already corrected once -- ordered endpoints,
failover on *every* failure to answer, stickiness within a single call, one `_Failure`
record rather than two variables that can disagree, and classification by the **last**
failure. Extracting it means that correction cannot be un-made by a provider that copied
the shape it had before.

What stays with the provider, because it is a judgement about a vendor rather than about
requesting:

* which settings hold the base URLs, and what the positions are called;
* the endpoint labels and the paths;
* every parser, and therefore what a 200's body means;
* `health()`, whose probe parses a vendor-specific document and whose `detail` strings are
  written where the no-URL, no-body, no-address rule is visible.

`docs/providers.md` promises that the third chain is a file rather than a refactor. That is
only true if the second chain does this.

## The rule, in one place

**Every failure to answer moves to the next endpoint.** A transport error, a 5xx, a 429, a
403, a 401, a 404, a 3xx -- all of them. Only a 200 whose body cannot be parsed stops the
call, and that happens in the provider, because this module's job ends at "an endpoint
answered".

The rule it replaced stopped on any 4xx, arguing that a second instance runs the same
software against the same chain and would refuse identically. That is true of a refusal
scoped to the **request** -- a 400 on a malformed address, which a provider that validates
offline cannot even produce -- and false of every refusal scoped to the **instance**, which
is the realistic set: a ban that is spelled 403, a self-hosted index behind an auth proxy
returning 401, a base URL missing its path prefix returning 404 forever. Those are exactly
the cases a second endpoint exists for, and the old rule made them the cases where the
second endpoint was never asked.

**The misconfiguration is not lost by failing over, it is relocated.** A provider's
`health()` probes each endpoint in turn and is what tells an operator that one of them is
broken. Failover keeps balances arriving; health is where the fact surfaces.

**Failover is sticky within a single call and resets between calls.** `read` returns the
index that answered, and the caller passes it back as `start` for the next address or
chunk. Re-asking an endpoint that just refused, twenty times in one sync, is how a soft
throttle becomes the ban one vendor warns about; but an endpoint throttled five minutes ago
is the one we would rather be using now.

**On exhaustion the error is chosen by the last failure**, not by the worst or the first,
and the cause is that same failure's. Both halves matter. A 429 followed by a 5xx is a
broken vendor, and telling an operator to lengthen an interval sends them after the wrong
thing; and a `ProviderRateLimitedError` chained to a `ConnectError` from the *other*
endpoint sends whoever reads the traceback after the wrong host.

## Nothing here logs, and nothing here names a host

Not one call. The shared transport logs `"{scheme}://{host}/{label}"` and nothing else,
which is the only log contract in this package that is enforced rather than remembered. No
message raised from here contains a URL, a body or an address: every vendor whose API this
reaches puts the address in the path, and `str(error)` on an `httpx` exception can carry the
request's URL, so failures are described by their exception's class name instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

import httpx

from portfolio.providers.errors import (
    ProviderError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from portfolio.providers.http import ENDPOINT_EXTENSION, IDEMPOTENT_EXTENSION

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "FALLBACK",
    "PRIMARY",
    "Endpoint",
    "EndpointSet",
    "configured_endpoints",
]

PRIMARY: Final = "primary"
FALLBACK: Final = "fallback"
"""What an endpoint is called in a `ProviderHealth.detail`.

A position rather than a URL, because `detail` is rendered in an operations view and
reaches a log, and a URL there would name the deployment. "the fallback answered" is the
whole of what an operator needs and the whole of what they are told.
"""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One configured instance of a vendor's API: where it is, and what to call it.

    `base_url` has already had its trailing slashes removed, so joining is concatenation
    and cannot produce a double slash -- which some reverse proxies answer with a 404 and
    some with a redirect, and the shared client does not follow redirects.
    """

    position: str
    base_url: str

    def url(self, path: str) -> str:
        """The absolute URL for `path`, which always begins with a slash."""
        return f"{self.base_url}{path}"


@dataclass(frozen=True, slots=True)
class _Failure:
    """Why one endpoint did not answer, and what to raise if it turns out to be the last.

    One record rather than the two loose variables this replaced. Those could disagree:
    the transport arm set a cause and the status arm did not, so a primary that refused
    the connection followed by a fallback answering 429 raised
    `ProviderRateLimitedError(...) from ConnectError(...)` -- an error about the fallback
    chained to a cause from the primary, which sends whoever reads the traceback after the
    wrong host. Carrying the class, the message and the cause together makes that
    impossible to express rather than merely unlikely.

    `cause` is `None` for a status-based failure, because there is no exception to chain:
    the endpoint answered, it simply answered with a refusal. `status` is the mirror image
    -- set for a status-based failure and `None` for a transport one -- and it is carried
    onto the raised exception so that a caller can tell *which* refusal it caught without
    reading the message. Kaspa's batch read is the caller that needs it: only a status
    which can mean "the batch was too large" may be answered with advice about the batch
    size, and a 403 from a CDN block must not be.
    """

    error: type[ProviderError]
    message: str
    cause: BaseException | None = None
    status: int | None = None


def configured_endpoints(candidates: Iterable[tuple[str, str]]) -> tuple[Endpoint, ...]:
    """The endpoints to try, in order, dropping blanks and dropping a repeat of the first.

    A blank URL means "not configured", which for a fallback is what a self-hoster running
    a single index sets. All blank yields no endpoints at all: a read then raises
    `ProviderUnavailableError` and `health` reports unhealthy, which is what an operator
    who has configured no index should be told rather than a zero balance.

    **Two URLs being the same is not a fallback, and treating it as one is actively
    harmful.** A self-hoster who points both variables at their own index -- which is a
    thing people do, because two variables look like they both want filling -- would
    otherwise get a "fallback" that is the same host: a 429 costs `max_attempts` requests,
    and then the failover spends `max_attempts` more on the host that has just asked us to
    stop. The one vendor whose limit is enforced by a ban is the one most likely to be
    asked twice this way.

    Compared after trimming and after trailing slashes are removed, so `https://x.test/api`
    and `https://x.test/api/` are recognised as one endpoint. Order is preserved and the
    *first* spelling wins, so an operator who configures the same index twice gets one
    endpoint called `primary` rather than one called `fallback`.

    Args:
        candidates: `(position, url)` pairs in the order they should be tried.

    Returns:
        One `Endpoint` per distinct, non-blank URL, in the order given.
    """
    endpoints: list[Endpoint] = []
    seen: set[str] = set()
    for position, url in candidates:
        base_url = url.strip().rstrip("/")
        if not base_url or base_url in seen:
            continue
        seen.add(base_url)
        endpoints.append(Endpoint(position=position, base_url=base_url))
    return tuple(endpoints)


class EndpointSet:
    """An ordered list of endpoints, the shared client, and the failover loop over both.

    Holds the client rather than taking one per call: an instance belongs to a provider,
    which is built per `registry.create()` with the client it was handed, and a set whose
    client could change between two addresses of one call would produce a result read
    through two different connection pools with nothing saying so.

    `vendor` is the name that appears in an exhaustion message -- "Every Esplora instance
    was tried" -- and it is a **software or protocol name, never a host**. It is rendered
    into a `ProviderError`, which reaches a log and a traceback.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        endpoints: Sequence[Endpoint],
        *,
        vendor: str,
    ) -> None:
        self._client = client
        self._endpoints = tuple(endpoints)
        self._vendor = vendor

    @classmethod
    def configured(
        cls,
        client: httpx.AsyncClient,
        candidates: Iterable[tuple[str, str]],
        *,
        vendor: str,
    ) -> EndpointSet:
        """Build a set straight from `(position, url)` pairs out of the settings.

        The spelling a provider's `__init__` uses. `configured_endpoints` stays a separate
        pure function so its blank-and-duplicate rules can be driven without a client.
        """
        return cls(client, configured_endpoints(candidates), vendor=vendor)

    @property
    def endpoints(self) -> tuple[Endpoint, ...]:
        """Every configured endpoint, in order, for a `health()` that probes each in turn.

        A provider's health check is the thing that surfaces a misconfiguration failover
        would otherwise hide, and it needs the positions to say which one answered. The
        tuple is immutable, so exposing it cannot let a caller reorder the failover.

        There is deliberately no `__len__` on this class. A caller that wants a count has
        `len(endpoints)` on the tuple, and a convenience method nothing calls is a line no
        test covers and a claim no reader can check -- the rule this package already
        applies to an `except` clause that cannot fire.
        """
        return self._endpoints

    async def read(
        self,
        path: str,
        label: str,
        start: int = 0,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[str, int]:
        """`GET path` from the first endpoint that answers, starting at `start`.

        Returns the body and the index of the endpoint that produced it, which the caller
        carries into its next read as `start`. That is the whole of the sticky-failover
        mechanism: an endpoint that failed is never asked again within one call, and
        nothing has to remember to skip it.

        **`headers` exists for one caller and carries one kind of value: a credential.**
        The keyed price source authenticates with a request header, and the alternative
        spellings are both worse. A key in the query string is the case `http.py` warns
        about by name -- `strip_query` keeps it out of *this* application's log and does
        nothing about the vendor's, any intermediary's, or a traceback that renders the
        URL. Setting it on the shared `httpx.AsyncClient` instead would send one vendor's
        credential to every host every provider talks to.

        Per request and per call site, therefore, so the credential travels no further
        than the one request that needs it. **Nothing here logs a header**, and nothing
        may start to: the transport logs `request_target`, which renders a scheme, a host
        and an endpoint label and never sees a header at all. That is the property this
        argument depends on, and it is enforced in `http.py` rather than remembered here.

        Args:
            path: the path to read, beginning with a slash.
            label: the endpoint label, a member of `ENDPOINT_LABELS`.
            start: the index to begin at, which is sticky failover's whole state.
            headers: request headers for this one call, or `None` for the client's own.

        Raises:
            ProviderRateLimitedError: every endpoint was tried and the last said 429.
            ProviderResponseError: every endpoint was tried and the last refused with some
                other non-200.
            ProviderUnavailableError: every endpoint was tried and the last did not answer
                or failed with a 5xx -- and the same when none is configured.
        """
        return await self._failover(
            path, label, start, payload=None, idempotent=False, headers=headers
        )

    async def post(
        self,
        path: str,
        label: str,
        start: int = 0,
        *,
        json: Mapping[str, object],
        idempotent: bool,
    ) -> tuple[str, int]:
        """`POST path` with a JSON body, from the first endpoint that answers.

        For a vendor whose batch read is expressed as a `POST` -- Kaspa's
        `POST /addresses/balances` is the one this exists for. Identical failover to
        `read`; the difference is the method and two things that follow from it.

        **`idempotent` is required and has no default, and that is the whole point of this
        signature.** The argument this seam is built on is that retrying a `POST` is unsafe
        by default: `RetryPolicy.retry_methods` is not widened to include `POST`, because
        the policy lives on the process-wide transport and widening it would make an
        exchange request that places an order retryable, where a retry after a transport
        error can double a trade.

        A shared helper that set `IDEMPOTENT_EXTENSION: True` for every caller would
        reintroduce exactly that, one layer up and more quietly -- and **`EndpointSet` is
        precisely what an exchange provider with a primary and a fallback will reach for.**
        At that moment the failover loop would double-submit. It is harmless while Kaspa is
        the only caller, which is the reason to fix it now rather than after the second one
        arrives. A default of `False` would have been no better: it would put the decision
        back in this file, silently, where the call site cannot see it.

        So the answer is given once per call site, in a word a reviewer can read, and it is
        impossible to omit.

        **The body is passed as `json=`, which makes it bytes rather than a stream**, and
        that is not a stylistic choice. `httpx` consumes a request stream on the first
        attempt, so a retried streamed body replays as empty: the server would answer about
        no addresses at all, and the sync would report zeros rather than an error.

        Args:
            path: the path to post to, beginning with a slash.
            label: the endpoint label, a member of `ENDPOINT_LABELS`.
            start: the index to begin at, which is sticky failover's whole state.
            json: the request body. Named for the `httpx` keyword it becomes, because that
                keyword is the whole point: it is what turns the body into bytes rather
                than a stream, and a caller reading this signature should see which one it
                is getting.
            idempotent: whether repeating this exact request is safe. `True` only for a
                request that changes nothing at the vendor -- a read expressed as a `POST`.
                Never `True` for anything that places, cancels or transfers.

        Returns:
            The body and the index of the endpoint that produced it.

        Raises:
            ProviderRateLimitedError: every endpoint was tried and the last said 429.
            ProviderResponseError: every endpoint was tried and the last refused with some
                other non-200.
            ProviderUnavailableError: every endpoint was tried and the last did not answer
                or failed with a 5xx -- and the same when none is configured.
        """
        return await self._failover(
            path, label, start, payload=json, idempotent=idempotent, headers=None
        )

    async def _failover(
        self,
        path: str,
        label: str,
        start: int,
        *,
        payload: Mapping[str, object] | None,
        idempotent: bool,
        headers: Mapping[str, str] | None,
    ) -> tuple[str, int]:
        """The loop both public methods are: try each endpoint in turn, classify the last.

        One body rather than two, because two copies of this loop is the thing the module
        exists to prevent -- a rule corrected once in review is a rule that must have
        exactly one implementation.

        `idempotent` is passed through rather than decided here. `read` says `False` and
        means it: a `GET` is already retryable by `RetryPolicy.retry_methods`, so the
        extension would add nothing and saying it anyway would make the one deliberate
        opt-in look like boilerplate.
        """
        failure = self._nothing_configured()
        for index in range(start, len(self._endpoints)):
            endpoint = self._endpoints[index]
            try:
                response = await self._request(
                    endpoint, path, label, payload, idempotent=idempotent, headers=headers
                )
            except httpx.TransportError as error:
                failure = _Failure(
                    error=ProviderUnavailableError,
                    # The class name, never `str(error)`: `httpx` puts the request's URL
                    # into some of its messages, and the URL names the deployment.
                    message=(
                        f"Every {self._vendor} instance was tried; the last one did not "
                        f"answer ({type(error).__name__})."
                    ),
                    cause=error,
                )
                continue

            if response.status_code == HTTPStatus.OK:
                return response.text, index
            failure = self._failure_for(response.status_code)

        raise failure.error(failure.message, status=failure.status) from failure.cause

    async def _request(
        self,
        endpoint: Endpoint,
        path: str,
        label: str,
        payload: Mapping[str, object] | None,
        *,
        idempotent: bool,
        headers: Mapping[str, str] | None,
    ) -> httpx.Response:
        """One request to one endpoint, labelled, and idempotent only if the caller said so.

        The extension is set only when `idempotent` is true, rather than always with a
        boolean value. A request that carries `IDEMPOTENT_EXTENSION: False` and one that
        carries no such key are the same request to the transport -- `is True` is the test
        -- and the absent key is the honest spelling of "this was not opted in".

        `headers` is passed straight to `httpx` and is never inspected, never copied onto
        the client and never logged. It may hold a credential; see `read`.
        """
        url = endpoint.url(path)
        # Materialised once and passed to whichever verb runs. `httpx` treats `None` as
        # "the client's own headers", which is what every caller but the keyed price source
        # wants. Applied to **both** branches rather than only to the `GET` one: `post` is
        # the only caller that passes `headers=None` today, and a parameter silently
        # dropped on one path is a credential silently dropped on the day somebody adds a
        # keyed `POST`.
        request_headers = dict(headers) if headers is not None else None
        if payload is None:
            return await self._client.get(
                url,
                headers=request_headers,
                extensions={ENDPOINT_EXTENSION: label},
            )
        extensions: dict[str, object] = {ENDPOINT_EXTENSION: label}
        if idempotent:
            extensions[IDEMPOTENT_EXTENSION] = True
        return await self._client.post(
            url, json=payload, headers=request_headers, extensions=extensions
        )

    def _nothing_configured(self) -> _Failure:
        """What a read fails with when every base URL is blank.

        Unavailable rather than a refusal: nothing was asked, so nothing refused. It is the
        starting value of the failover loop's `failure`, which means the no-endpoint case
        takes the ordinary exhaustion path instead of needing a branch of its own.
        """
        return _Failure(
            error=ProviderUnavailableError,
            message=f"No {self._vendor} instance is configured.",
        )

    def _failure_for(self, status: int) -> _Failure:
        """Classify a non-200 for the moment it turns out to be the last thing we heard.

        The three-way split `providers/errors.py` exists to keep apart, decided on the
        status alone -- which is all a vendor reliably documents, and for the two current
        ones is all they document at all:

        * **429 -> `ProviderRateLimitedError`.** It survived the transport's retries, so
          our interval is too short for this vendor: a configuration change, not patience.
        * **5xx -> `ProviderUnavailableError`.** The vendor is broken rather than us, and
          the previous reading is still the best information available.
        * **anything else -> `ProviderResponseError`.** It understood and refused.
          Retrying changes nothing, and a person has to look at it.

        Note what this does **not** decide: whether to try the next endpoint. Every one of
        these fails over; this only says what the last one meant.

        The message names the vendor and the status and nothing else. A status is something
        an operator can act on; a URL or a body is the deployment or the owner's holdings.
        """
        if status == HTTPStatus.TOO_MANY_REQUESTS:
            return _Failure(
                error=ProviderRateLimitedError,
                message=(
                    f"Every {self._vendor} instance was tried; the last one is throttling us."
                ),
                status=status,
            )
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            return _Failure(
                error=ProviderUnavailableError,
                message=(
                    f"Every {self._vendor} instance was tried; the last one failed with "
                    f"HTTP {status}."
                ),
                status=status,
            )
        return _Failure(
            error=ProviderResponseError,
            message=(
                f"Every {self._vendor} instance was tried; the last one refused the request "
                f"with HTTP {status}."
            ),
            status=status,
        )
