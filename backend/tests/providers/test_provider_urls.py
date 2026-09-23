"""The two Esplora base URLs are refused at startup, and why that is not tidiness.

This file exists because of a defect found after #7's implementation was already green,
and the defect is worth stating precisely, because the first two descriptions of it -- in
a test report and then in a provider docstring -- were both wrong about the mechanism.

`EsploraProvider.health()` promised it never raises. `_read` catches `httpx.TransportError`
so that no `httpx` exception reaches a service. Both claims were false for a typo'd base
URL, and **not** because of `httpx.InvalidURL`, which is what the residual originally
named. Measured against httpx 0.28.1, and pinned below in
`test_each_refused_url_is_one_that_really_does_escape_the_provider`:

    client.get("mempool.space/api" + path)  -> builtins.ValueError: unknown url type
    client.get("http://" + path)            -> builtins.ValueError: unknown url type
    client.get("not a url" + path)          -> builtins.ValueError: unknown url type

A bare `ValueError` out of `urllib`, which no `except httpx.*` can catch by type. So
`health()` raised, and `fetch_balances` raised something a service that must never import
`httpx` could not have been told to expect.

The fourth case is quieter and worse, and it is the one that motivates refusing a *scheme*
rather than only an unparseable string. `htp://host/api` parses perfectly well, so it
reaches the real transport as `httpx.UnsupportedProtocol` -- which **is** an
`httpx.TransportError`, so `_read` catches it and reports `ProviderUnavailableError` on
every sync, forever, while nothing anywhere mentions the typo. That is exactly the
"a self-hosted Esplora behind an auth proxy returns 401 and the owner is told their chain
is down forever" failure `providers/errors.py` was written to prevent, arriving through a
one-character mistake in a scheme.

The fix is upstream, in `config.py`, so that no running application holds such a URL and
the provider needs no branch for it. That is the right shape -- a branch no test can reach
is worse than the gap it was meant to close -- but it moves the whole burden of proof
here: **these tests are now the only thing standing between a typo'd URL and a container
that starts.**
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import httpx
import pytest
from pydantic import ValidationError

from portfolio.config import PROVIDER_URL_SCHEMES, Settings, provider_url_violation
from portfolio.providers.chains import bitcoin
from tests.address_vectors import BIP173_TESTNET_P2WPKH

#: The two environment variables the validator runs over. Both, because a validator that
#: checked only the primary would pass every test that only ever passed a bad primary --
#: and the fallback is the one an operator edits second and re-reads less carefully.
PRIMARY_VARIABLE: Final = "PORTFOLIO_BITCOIN_ESPLORA_URL"
FALLBACK_VARIABLE: Final = "PORTFOLIO_BITCOIN_ESPLORA_FALLBACK_URL"

#: Synthetic, and obviously so. A provider URL may legitimately carry basic-auth
#: userinfo -- that is how a self-hoster puts their own Esplora behind a proxy -- so the
#: refusal message must never quote the URL. This is the string that proves it does not,
#: and it is shaped so that any eight-character substring of it is unique.
SENTINEL_USERINFO: Final = "NOT-A-REAL-PASSWORD-DO-NOT-LOG-c0ffee"

#: Every URL the validator must refuse, with the arm each one reaches. Named rather than
#: anonymous, because a parametrised list of strings that all happen to hit one branch is
#: a sweep that looks thorough and tests one thing.
REFUSED: Final[tuple[tuple[str, str], ...]] = (
    ("http://[::1", "an unclosed IPv6 bracket: httpx.InvalidURL"),
    ("http://a\nb.test", "a non-printable character: httpx.InvalidURL"),
    ("mempool.space/api", "no scheme at all, which is the realistic typo"),
    ("htp://host/api", "a one-character scheme typo that otherwise parses"),
    ("ftp://host/api", "a scheme that is not http"),
    ("file:///etc/passwd", "a scheme that would read the local filesystem"),
    ("http://", "a scheme and no host"),
    ("not a url", "prose"),
)

#: Every URL the validator must accept. The awkward ones are the point: a validator that
#: refused these would be one an operator works around by disabling it.
ACCEPTED: Final[tuple[tuple[str, str], ...]] = (
    ("https://host.example/api", "the ordinary case"),
    ("http://host.example/api", "plain http, for a self-hosted index on a LAN"),
    ("https://host.example", "no path"),
    ("https://host.example/", "a trailing slash"),
    ("https://host.example:3002/api", "an explicit port, which a self-hosted index uses"),
    (f"https://user:{SENTINEL_USERINFO}@host.example/api", "basic auth, a supported setup"),
    ("https://127.0.0.1:3002/api", "a loopback address"),
    ("", "blank, which is how an operator says 'one instance only'"),
    ("   ", "blank with whitespace, because a .env file collects trailing spaces"),
)


def settings_with(**overrides: str) -> Settings:
    """A `Settings` built directly, so nothing process-wide moves.

    `get_settings` is cached for the life of the process, so a test that set
    `PORTFOLIO_*` in the environment would be sharing state with every other test in the
    run. The validator under test is a model validator, so constructing the model is
    exactly what runs it.
    """
    return Settings(**overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# The pure validator
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("url", "why"), [pytest.param(url, why, id=why) for url, why in REFUSED])
def test_a_url_the_client_cannot_use_is_refused(url: str, why: str) -> None:
    """Each refused shape, with the arm it reaches named in the parameter id.

    The scheme rows carry the weight. An unparseable string is caught by any validator at
    all; `htp://host/api` is not, and it is the one that produces a chain reported as down
    forever with no mention of the cause.
    """
    del why  # In the parameter id, where a failure can read it.

    reason = provider_url_violation(url)

    assert reason is not None, f"{url!r} was accepted, and the client cannot use it"
    assert reason.strip()


@pytest.mark.parametrize(("url", "why"), [pytest.param(url, why, id=why) for url, why in ACCEPTED])
def test_a_usable_url_is_not_refused(url: str, why: str) -> None:
    """The control, and it is not a formality.

    A validator that refused everything would satisfy every refusal test above and turn
    the fix into an application that cannot start. The blank rows are a real configuration
    -- an empty fallback is how a self-hoster says "one instance only", and
    `tests/providers/chains/test_bitcoin.py` drives that path -- and the userinfo row is a
    deployment this project supports rather than a mistake.
    """
    del why  # In the parameter id.

    assert provider_url_violation(url) is None


def test_the_refusal_never_quotes_the_url() -> None:
    """The reason names the scheme and nothing else, so a password cannot reach a log.

    `https://user:password@host/api` is a supported configuration, and this message is
    rendered into a startup failure -- which is written to stdout, captured by the
    container runtime, and pasted into an issue by whoever is working out why the deploy
    rolled back. A message built the helpful way, quoting the URL that failed, would put
    the password in all three places.

    Driven over every refused URL, including the ones carrying the sentinel, so the
    property holds on each arm rather than on the one somebody thought of.
    """
    for url, _why in REFUSED:
        with_credentials = url.replace("//", f"//user:{SENTINEL_USERINFO}@", 1)
        for candidate in (url, with_credentials):
            reason = provider_url_violation(candidate)

            assert reason is not None
            assert SENTINEL_USERINFO not in reason
            assert "user:" not in reason
            assert candidate not in reason


def test_the_scheme_allowlist_is_pinned_and_is_not_empty() -> None:
    """Pinned as a literal, the same shape as `PUBLIC_API_PATHS` and `ENDPOINT_LABELS`.

    `PROVIDER_URL_SCHEMES == PROVIDER_URL_SCHEMES` is true of any set at all, including
    the empty one -- which would refuse every URL and every configuration, and including a
    widened one carrying `file`, which would let a base URL read the local filesystem.
    """
    assert sorted(PROVIDER_URL_SCHEMES) == ["http", "https"]
    assert PROVIDER_URL_SCHEMES, "an empty allowlist refuses every configuration"
    assert isinstance(PROVIDER_URL_SCHEMES, frozenset)


# --------------------------------------------------------------------------------------
# Through `Settings`, which is what actually refuses to start
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("url", "why"), [pytest.param(url, why, id=why) for url, why in REFUSED])
def test_a_typod_primary_url_refuses_to_start_and_names_its_variable(url: str, why: str) -> None:
    """The failure an operator actually meets, and the message they act on.

    Naming the variable is the whole value of the message: `config.py` holds a dozen
    settings and the one that is wrong is not otherwise discoverable from a validation
    error. Asserted as the variable name rather than as "it raised", because a refusal
    nobody can act on gets worked around rather than fixed.
    """
    del why  # In the parameter id.

    with pytest.raises(ValidationError, match=PRIMARY_VARIABLE):
        settings_with(bitcoin_esplora_url=url)


@pytest.mark.parametrize(("url", "why"), [pytest.param(url, why, id=why) for url, why in REFUSED])
def test_a_typod_fallback_url_refuses_to_start_and_names_its_own_variable(
    url: str, why: str
) -> None:
    """The second variable, driven separately, because the loop is where this can go wrong.

    A validator applied to `self.bitcoin_esplora_url` twice -- a copy-paste in the loop
    body, the easiest mistake in the file to make and the hardest to see -- passes every
    test above and leaves the fallback unchecked. The fallback is also the variable an
    operator edits second and re-reads less carefully.

    The assertion is that the message names the **fallback** variable specifically, which
    is what a duplicated loop body would get wrong while still raising.
    """
    del why  # In the parameter id.

    with pytest.raises(ValidationError, match=FALLBACK_VARIABLE):
        settings_with(bitcoin_esplora_fallback_url=url)


@pytest.mark.parametrize(
    ("variable", "field"),
    [
        pytest.param(PRIMARY_VARIABLE, "bitcoin_esplora_url", id="the primary"),
        pytest.param(FALLBACK_VARIABLE, "bitcoin_esplora_fallback_url", id="the fallback"),
    ],
)
def test_the_startup_failure_names_the_variable_and_still_quotes_no_url(
    variable: str, field: str
) -> None:
    """The message an operator actually sees, which is not the one the validator returns.

    **This test exists because a mutation survived without it.** `provider_url_violation`
    returning a URL-free reason says nothing about what `_refuse_unsafe_configuration`
    does with that reason: interpolating the offending URL into the final message is the
    obvious way to make a validation error more helpful, it passes every assertion about
    the pure function, and it is the one that matters -- a `ValidationError` is what
    pydantic renders to stdout when the container refuses to start, which the runtime
    captures and somebody pastes into an issue while working out why the deploy rolled
    back.

    So the assertion is on the rendered exception rather than on the return value, and the
    URL under test carries userinfo, because a provider URL legitimately may.

    `str(caught.value)` and not `caught.value.errors()`: the rendered form is what reaches
    a terminal, and a message that was safe in one and not the other would still be a
    disclosure.
    """
    leaky = f"htp://user:{SENTINEL_USERINFO}@host.example/api"

    with pytest.raises(ValidationError) as caught:
        settings_with(**{field: leaky})

    rendered = str(caught.value)

    assert variable in rendered, "an operator cannot act on a message that names no setting"
    assert SENTINEL_USERINFO not in rendered
    assert leaky not in rendered
    # Our own message contributes the reason and nothing else: the scheme, quoted, and no
    # part of the URL it came from.
    assert "scheme must be http or https" in rendered
    assert "'htp'" in rendered


def test_pydantics_structured_errors_carry_the_whole_environment_and_ours_does_not() -> None:
    """**A disclosure that is not ours, pinned as current behaviour so closing it shows up.**

    Found by a mutation sweep over this file's own subject, and it is wider than #7.
    `str(ValidationError)` -- the form that reaches stdout when the container refuses to
    start -- elides the middle of the input it echoes, so no secret survives it. That is
    the artifact, and the test above asserts on it.

    `ValidationError.errors()` does **not** elide. Its `input` key carries the entire dict
    the model was built from, which for `get_settings()` is every `PORTFOLIO_*` variable
    in the environment -- measured here, including `PORTFOLIO_BOOTSTRAP_PASSWORD` in
    plaintext, because the raw environment string is in that dict *before* pydantic
    coerces it into the `SecretStr` that would have masked it.

    So the `SecretStr` on the field is not the protection anyone assumes it is at this
    boundary, and one `logger.exception` or one `.errors()` in a future startup handler
    turns a typo'd setting into every credential on the host reaching a log.

    **Two independent barriers stand between that and today, and both were verified.**
    `api/errors.py` is registered for `RequestValidationError` only, so a settings error
    never reaches it -- and even if it did, `handle_validation_error` projects each entry
    down to `loc`, `msg` and `type`, dropping `input` before anything is rendered. So the
    realistic hazard is not an existing call site but a *new* one: a debug dump, a
    `logger.exception`, or precisely the helpful startup handler somebody would write to
    improve this message. That is why this is a pin rather than a failure.

    **Asserting the unsafe outcome deliberately**, the same shape that
    `tests/providers/test_url_scrubbing.py` used for the truncated-label residual before
    #7 closed it. If something starts redacting this, this test goes red and that is the
    notification the residual closed -- not a regression. It is reported to the tech lead
    as its own decision, because the fix is a startup exception handler and belongs to
    nothing in this issue.
    """
    leaky = f"htp://user:{SENTINEL_USERINFO}@host.example/api"

    with pytest.raises(ValidationError) as caught:
        settings_with(bitcoin_esplora_url=leaky)

    rendered = str(caught.value)
    structured = str(caught.value.errors())
    serialised = caught.value.json()

    # Ours: clean. Theirs: not. Both asserted, so the boundary between them is visible.
    assert SENTINEL_USERINFO not in rendered
    for form, name in ((structured, "errors()"), (serialised, "json()")):
        assert SENTINEL_USERINFO in form, (
            f"pydantic no longer echoes the full input in {name}; if that is now "
            "redacted, the residual this test pins has closed and the warning above "
            "can go -- along with this assertion"
        )
    # `json()` matters on its own: it is what a structured logger reaches for, and it is
    # one `logger.error(exc.json())` away from being the whole environment on stdout.
    # And the reason our own message contributes is clean regardless of the wrapper.
    reason = provider_url_violation(leaky)
    assert reason is not None
    assert SENTINEL_USERINFO not in reason


def test_a_bad_primary_is_not_reported_as_a_bad_fallback() -> None:
    """The two messages are distinguishable, which is the point of naming them at all.

    Without this, both tests above would pass against a loop that reported every failure
    under one name -- and an operator would be sent to the wrong line.
    """
    with pytest.raises(ValidationError) as caught:
        settings_with(bitcoin_esplora_url="mempool.space/api")

    rendered = str(caught.value)
    assert PRIMARY_VARIABLE in rendered
    assert FALLBACK_VARIABLE not in rendered


def test_the_shipped_defaults_pass_their_own_validator() -> None:
    """The embarrassing case: a validator that refuses the values it ships with.

    `Settings()` with no arguments is what a fresh deployment builds, so if the defaults
    did not satisfy the check the application could not start at all -- on a Raspberry Pi,
    after a deploy, with the rollback already running.
    """
    shipped = Settings()

    assert provider_url_violation(shipped.bitcoin_esplora_url) is None
    assert provider_url_violation(shipped.bitcoin_esplora_fallback_url) is None


def test_both_urls_may_be_blank_together() -> None:
    """Every chain unread is a configuration, not a contradiction.

    `EsploraProvider.health()` answers `"no endpoint configured"` for exactly this, and
    `tests/providers/chains/test_bitcoin.py::test_health_says_so_when_no_endpoint_is_configured`
    drives it -- which it could not do if `Settings` refused to build.
    """
    settings = settings_with(bitcoin_esplora_url="", bitcoin_esplora_fallback_url="")

    assert settings.bitcoin_esplora_url == ""
    assert settings.bitcoin_esplora_fallback_url == ""


# --------------------------------------------------------------------------------------
# The refusal is necessary, measured from outside rather than argued
# --------------------------------------------------------------------------------------


async def test_each_refused_url_is_one_that_really_does_escape_the_provider() -> None:
    """Why each entry in `REFUSED` is there, demonstrated against `httpx` itself.

    A refusal list is a set of opinions until something shows what happens without it.
    This drives every refused URL through an `httpx.AsyncClient` the way the provider
    would and asserts that the outcome is one of the two failures the fix exists for:

    * an exception that is **not** an `httpx.TransportError`, so `_read`'s `except` clause
      could not have caught it by type and it escapes into a service that must never
      import `httpx`; or
    * an `httpx.UnsupportedProtocol`, which **is** a `TransportError` and is therefore
      caught and reported as `ProviderUnavailableError` on every sync forever, with
      nothing anywhere naming the typo.

    Nothing here touches the network. The parse failures happen while the request URL is
    built and `UnsupportedProtocol` is raised during transport selection, both before any
    socket is opened -- which is also why the second arm can use a real client at all.

    If a future `httpx` turns these into something `_read` handles cleanly, this test goes
    red and says the refusal list is now guarding against a failure that no longer exists
    -- rather than leaving it to protect against nothing with every other test green.
    """

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - unreached
        return httpx.Response(200, content="1")

    path = f"/address/{BIP173_TESTNET_P2WPKH}"

    async def outcome(url: str) -> BaseException | None:
        """Whatever `client.get` did with this URL, caught rather than asserted on.

        The mock transport first, because the parse failures happen while the request is
        being built and never reach a transport at all. A scheme that *parses* gets past
        the mock -- which answers whatever it is handed -- so it is tried again against a
        real client, where transport selection is what refuses it. Neither opens a socket.
        """
        for transport in (httpx.MockTransport(handler), None):
            client = httpx.AsyncClient(transport=transport) if transport else httpx.AsyncClient()
            async with client:
                try:
                    await client.get(f"{url}{path}")
                # Broad on purpose: the exception's *type* is the subject, and naming a
                # narrower one here would presume the answer this test exists to measure.
                except Exception as error:
                    return error
        return None

    outcomes = {url: await outcome(url) for url, _why in REFUSED}

    # Asserted out here, outside every `except`, so a refused URL that raised nothing at
    # all is a failure rather than a silently skipped row.
    for (url, why), error in zip(REFUSED, outcomes.values(), strict=True):
        assert error is not None, (
            f"{url!r} ({why}) was accepted by httpx without incident, so the refusal "
            "list is wider than the failure it guards"
        )
        escapes_untyped = not isinstance(error, httpx.HTTPError)
        mis_reported_as_unavailable = isinstance(error, httpx.UnsupportedProtocol)
        assert escapes_untyped or mis_reported_as_unavailable, (
            f"{url!r} raised {type(error).__name__}, which `_read` already catches and "
            "reports correctly -- so it does not need refusing at startup"
        )

    # Both failure modes are represented, so the list covers the bug that was found *and*
    # the one that was found while fixing it, rather than eight rows of the same arm.
    kinds = {type(error).__name__ for error in outcomes.values()}
    assert "ValueError" in kinds
    assert "UnsupportedProtocol" in kinds
    assert "InvalidURL" in kinds


async def test_an_accepted_url_does_not_escape_the_provider() -> None:
    """The control for the test above, and the half that makes it a boundary.

    Every URL the validator *accepts* has to be one the client can actually build a
    request from -- including the awkward ones: userinfo, an explicit port, no path. A
    validator that accepted a URL `httpx` then refused would have moved the failure rather
    than removed it, and `health()`'s "never raises" would still be untrue.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="1")

    path = f"/address/{BIP173_TESTNET_P2WPKH}"

    for url, why in ACCEPTED:
        if not url.strip():
            continue  # A blank base URL is never used to build a request; see `_instances`.
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await client.get(f"{url}{path}")

        assert response.status_code == 200, why


def test_the_escape_assertion_would_notice_a_url_that_is_fine() -> None:
    """The helper's own control: a well-formed URL must not look like an escape.

    Without this, `test_each_refused_url_is_one_that_really_does_escape_the_provider`
    could be satisfied by a loop that recorded an entry unconditionally.
    """
    assert provider_url_violation("https://host.example/api") is None
    assert provider_url_violation("htp://host.example/api") is not None


# --------------------------------------------------------------------------------------
# The claim the provider now makes on the strength of all this
# --------------------------------------------------------------------------------------


def test_the_provider_catches_no_configuration_error_of_its_own() -> None:
    """`bitcoin.py` deliberately has no `except` for an unparseable URL, and says why.

    The fix is upstream, so the provider needs no branch -- and a branch no test can reach
    is worse than the gap it was meant to close, because it reads as a handled case
    forever. This asserts the reasoning is written where the next person will look for it,
    since a decision nobody wrote down is indistinguishable from a thing somebody forgot.

    It also pins the *correction*: the provider's docstring used to name
    `httpx.InvalidURL` as the residual, and that was wrong about the class. A future
    reader reaching for `except httpx.InvalidURL` here would be re-adding a handler for an
    exception that never arrives, and would still not catch the `ValueError` that does.
    """
    source = Path(str(bitcoin.__file__)).read_text(encoding="utf-8")

    assert "except httpx.InvalidURL" not in source, (
        "the provider is catching a configuration error the config layer now refuses, "
        "which is a branch no test can reach"
    )
    assert "ValueError" in source, (
        "the docstring no longer names the exception that actually escapes, so the "
        "correction that motivated the config-layer fix has been lost"
    )
