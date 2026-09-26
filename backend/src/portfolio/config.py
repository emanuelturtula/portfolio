"""Application settings, loaded from the environment.

Every value is read from a `PORTFOLIO_`-prefixed environment variable so that the same
image can run in development and in production without a rebuild. Nothing in this module
carries a default that would be unsafe if it survived into production.
"""

import re
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Final, Literal, Self

import httpx
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from portfolio.domain.passwords import (
    OWASP_MINIMUM_MEMORY_COST,
    OWASP_MINIMUM_TIME_COST,
    policy_violation,
)

# The Vite dev server. Harmless in development and wrong everywhere else, which is why
# `prod` refuses to start while it is still the configured value.
DEV_ALLOWED_ORIGIN: Final = "http://localhost:5173"

# The `__Host-` prefix is only valid on a cookie that is `Secure`, has `Path=/` and has no
# `Domain`; a browser silently drops one that arrives without them. The failure mode is a
# login that returns 204 and then does not work, with nothing in any log -- so the name is
# derived from the flag rather than written down twice.
SECURE_SESSION_COOKIE_NAME: Final = "__Host-psid"
INSECURE_SESSION_COOKIE_NAME: Final = "psid"

PROVIDER_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
"""The schemes a provider base URL may use. `https` everywhere except a local index."""


def provider_url_violation(url: str) -> str | None:
    """Why this provider base URL is unusable, or `None` if it is fine. Blank is fine.

    **Measured, not imagined.** Every one of these arrives at `httpx.AsyncClient.get` as an
    exception that a provider cannot translate, which is the one way `httpx` can currently
    reach a caller that must never import it:

    | Configured value | What `client.get` does |
    |---|---|
    | `mempool.space/api` (no scheme) | `builtins.ValueError: unknown url type` |
    | `not a url` | the same bare `ValueError` |
    | `http://` (no host) | the same bare `ValueError` |
    | `htp://host/api` (scheme typo) | `httpx.UnsupportedProtocol`, which *is* a `TransportError` |

    The first three escape `fetch_balances` and `health()` as a `ValueError` from inside
    `urllib`, past an `except httpx.TransportError` that cannot see it -- and past a
    `health()` whose contract is that it never raises. The fourth is worse for being
    quieter: it is caught, and reported as `ProviderUnavailableError` on every sync
    forever, so the owner is told their chain is down while nothing anywhere mentions the
    typo. That is the exact failure `providers/errors.py` names in its 401-behind-an-auth-
    proxy example.

    All four are configuration errors that are wrong from the first request and stay wrong,
    so the right moment to refuse them is startup -- where `_refuse_unsafe_configuration`
    already turns four other unsafe configurations into a container that fails its health
    check and a deployment that rolls back.

    **Parsed with `httpx.URL` on purpose**, rather than with `urllib.parse`: the question is
    not "is this a URL" in the abstract but "will the client this URL is handed to accept
    it", and a validator that answers a different question than the one that matters is how
    a check passes while the thing it guards fails. A space in the host survives both and is
    deliberately allowed through -- it resolves to nothing, and a host that does not resolve
    is honestly indistinguishable from one that is down.

    Userinfo is allowed. `https://user:pass@host/api` is how a self-hoster puts their own
    Esplora behind basic auth, which is a supported deployment rather than a mistake, and
    a provider URL never reaches a log in the first place: the transport logs
    `request_target`, which emits a scheme, a host and an endpoint label and never sees
    userinfo at all. **Not `strip_query`**, which is a separate helper for a future
    exchange provider and is not on this path -- `http.py` warns by name that reaching for
    it to log a chain request meets the letter of the rule and leaks anyway, and crediting
    it here would be that confusion written down as reassurance.

    Returns:
        A short reason, or `None`. **The reason never quotes the URL**, because a provider
        URL may legitimately carry userinfo; the scheme is enough to act on.
    """
    candidate = url.strip()
    if not candidate:
        # Blank is a configuration, not an omission: for the fallback it means "one
        # instance only", and for the primary it means this chain is not read at all.
        return None
    try:
        parsed = httpx.URL(candidate)
    except httpx.InvalidURL as error:
        # `httpx.URL` refuses a handful of inputs outright -- an unclosed IPv6 bracket, a
        # non-printable character. The class name rather than the message, which quotes
        # the offending URL.
        return f"it is not a URL ({type(error).__name__})"
    if parsed.scheme not in PROVIDER_URL_SCHEMES:
        return f"the scheme must be http or https, not {parsed.scheme!r}"
    if not parsed.host:
        return "it names no host"
    return None


def exchange_credentials_violation(
    variables: tuple[tuple[str, SecretStr | None], ...],
) -> str | None:
    """Why one venue's credential variables cannot be used, or `None` if they can.

    `variables` is the venue's `(environment variable, value)` pairs. Two rules, checked in
    this order:

    1. **No value is blank.** An empty or whitespace credential is a variable somebody set
       and got wrong, and `Credentials` would refuse it anyway -- on the first sync, where it
       looks like any other failure, instead of at startup, where it is a rollback.
    2. **All or none.** `None` for every variable means the venue is not configured and is
       not built. Some set and some not is a credential that cannot sign, and the reason
       names every variable that is missing.

    **The reason names variables and never a value**, and never a length or a prefix either:
    a partial credential in a log line is still part of a credential.
    """
    for name, value in variables:
        if value is not None and not value.get_secret_value().strip():
            return f"{name} is set but blank. Set it to the credential, or unset the variable."
    missing = [name for name, value in variables if value is None]
    if missing and len(missing) < len(variables):
        verb = "is" if len(missing) == 1 else "are"
        return (
            f"{' and '.join(missing)} {verb} not set while the other credential variables of "
            "the same venue are. Set all of them, or none."
        )
    return None


HEADER_SAFE_TEXT: Final = re.compile(r"\A[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?\Z")
"""Text an HTTP header can carry as it is: printable ASCII, no whitespace at either end.

An interior space is allowed -- it is a legal header value, and a user-chosen passphrase may
hold one. A control character, a line break, a leading or trailing space or tab, and any
character outside ASCII are not.

**The reason is a leak, measured on #13 with httpx 0.28.1.** A header value h11 refuses
raises `httpx.LocalProtocolError("Illegal header value b'...'")`, and the message is the
whole value. That is a `TransportError`, and an exchange provider chains its unavailable
error `from` a transport error, so the credential would reach any log that renders the
traceback. A non-ASCII character fails earlier and differently, as a bare
`UnicodeEncodeError` out of `client.get` -- outside every exchange error class. A trailing
space pasted into `secrets.env` is the realistic way to get either.
"""


def is_header_safe(value: str) -> bool:
    """Whether `value` can travel in an HTTP header unchanged. See `HEADER_SAFE_TEXT`."""
    return HEADER_SAFE_TEXT.match(value) is not None


def credential_header_violation(
    variables: tuple[tuple[str, SecretStr | None], ...],
) -> str | None:
    """Why a credential sent in a header cannot be sent, or `None` if every one can.

    `variables` is the `(environment variable, value)` pairs of the credentials a venue
    sends as header values -- for Bitget the API key and the passphrase, and **not** the
    secret, which only ever enters an HMAC and may hold anything. An unset variable passes.

    **The reason names the variable and the rule, and never the value**, nor which
    character or where: the position of a stray character is part of the credential too.
    """
    for name, value in variables:
        if value is not None and not is_header_safe(value.get_secret_value()):
            return (
                f"{name} holds a character an HTTP header cannot carry: whitespace at either "
                "end, a line break or another control character, or a character outside "
                "printable ASCII. Look for a stray space or line break where it was pasted."
            )
    return None


class Settings(BaseSettings):
    """Runtime configuration for the backend."""

    model_config = SettingsConfigDict(
        env_prefix="PORTFOLIO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        # Keep every environment value out of a validation error's `str()` and `repr()`,
        # which is what reaches the log when the container refuses to start. Pydantic elides
        # the *middle* of the echoed input and keeps both ends. Measured on #13: with the
        # Bitget key and secret set and the passphrase missing, `str(exc)` carried the key's
        # first five characters and the secret's last twenty; a passphrase with a trailing
        # space showed its own tail, which for a short passphrase is most of it. This drops
        # `input_value` and `input_type` from both renderings. It does **not** change
        # `errors()` or `json()`, which still carry the whole input -- see the docstring of
        # `_refuse_unsafe_configuration`.
        hide_input_in_errors=True,
    )

    environment: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    # The async driver is explicit in the URL: the engine, the session factory and
    # Alembic's environment are all async, and a bare `sqlite://` URL would build a
    # synchronous engine that fails the moment it is awaited. Production already sets
    # `sqlite+aiosqlite:////app/data/portfolio.db`, so only the default was out of step.
    database_url: str = "sqlite+aiosqlite:///./data/portfolio.db"
    allowed_origin: str = DEV_ALLOWED_ORIGIN
    session_cookie_secure: bool = True

    # Argon2id cost, tuned on the deployment hardware rather than copied from a cloud
    # instance. `memory_cost` is in KiB, so 147456 is 144 MiB.
    #
    # The previous default of 65536 was OWASP's minimum rounded up and an estimate for a
    # Cortex-A76. Measured on the Raspberry Pi 5 with `hash-benchmark`, it came in at
    # 113.1 ms -- less than half the ~250 ms target, because the hardware is faster than
    # the estimate assumed. Argon2's cost is close to linear in `memory_cost * time_cost`,
    # so the memory was raised by the missing factor, and the result was then measured on
    # the deployed configuration rather than trusted: 271 ms, against a 250 ms target.
    #
    # Memory rather than passes: memory hardness is what makes parallel attack on a GPU
    # expensive, while an extra pass costs the defender and the attacker alike. Raising
    # this does not invalidate a stored password -- the parameters are encoded in each
    # hash, so an old one still verifies and is re-hashed on the next successful login.
    #
    # These stay settings, not constants, because the number that matters is the one
    # measured on the host. `docs/operations.md` holds the procedure and the log.
    argon2_time_cost: int = 3
    argon2_memory_cost: int = 147456
    argon2_parallelism: int = 4

    # Two expiries, both enforced: the idle window slides with activity, the absolute one
    # never moves. Whole days, because every value this product will ever want is one.
    session_idle_days: int = 7
    session_absolute_days: int = 30

    # The account the first start creates, when `bootstrap_password` is set and no account
    # exists yet. An existing account is never touched, so leaving the variable in an
    # environment file does not reset the password on every deploy.
    bootstrap_username: str = "owner"

    # Credentials are never plain `str`. `SecretStr` keeps the value out of reprs,
    # tracebacks and model dumps, which is what stops an exchange key from reaching the
    # logs by accident; `logging.py` is the second line of defence, not the first.
    bootstrap_password: SecretStr | None = None

    # The two Esplora instances the Bitcoin provider reads, primary first, and the network
    # they serve. Public defaults so the product works out of the box; an operator running
    # their own index points both at it and nothing else changes.
    #
    # Two scalars rather than one `list[str]`, deliberately: pydantic-settings parses a
    # list out of the environment as JSON, which is not a syntax anybody types correctly
    # into a `.env` file at three in the morning. A blank fallback means "one instance
    # only" and is a self-hoster setting one URL and clearing the other.
    bitcoin_esplora_url: str = "https://mempool.space/api"
    bitcoin_esplora_fallback_url: str = "https://blockstream.info/api"

    # An Esplora instance serves exactly one network, and neither vendor documents what it
    # answers for an address from another one -- checked on 2026-09-22. So the provider
    # refuses a wrong-network address offline instead of trusting an undocumented 400.
    # The failure this prevents is the expensive one: a balance read against the wrong
    # chain is a number rather than an error, and nothing downstream can tell it from a
    # right one.
    #
    # A `Literal` rather than the `BitcoinNetwork` enum itself, so that a typo in the
    # environment is a startup failure that names the three acceptable values. `providers`
    # converts it to the domain enum, which is the layer allowed to know both.
    bitcoin_network: Literal["mainnet", "testnet", "regtest"] = "mainnet"

    # The kaspa-rest-server instances the Kaspa provider reads, primary first. Same two
    # scalars and the same blank-means-one-instance rule as the Esplora pair above.
    #
    # **The fallback ships blank, and that is a fact about the ecosystem rather than an
    # omission.** Bitcoin has two independent public Esplora operators, which is what makes
    # one a usable fallback for the other. There is one well-known public kaspa-rest-server
    # and no second operator to name, so a default fallback would either be a second URL at
    # the same host -- which `configured_endpoints` drops as a repeat, correctly, because a
    # fallback onto the host that just refused us is worse than none -- or a hostname
    # invented here. A self-hoster running their own index fills it in.
    kaspa_api_url: str = "https://api.kaspa.org"
    kaspa_api_fallback_url: str = ""

    # A kaspa-rest-server instance serves exactly one network, and this one is not a guess:
    # measured against the public instance on 2026-09-23, its own path validation is
    # `^kaspa:[a-z0-9]{61,63}$` with the prefix as a literal, and a `kaspatest:` address is
    # answered 422 quoting that rule. So the provider refuses a wrong-network address
    # offline, before a URL is built out of it.
    #
    # The same measurement found that the vendor checks prefix, charset and length and
    # **not the checksum**: a mistyped mainnet address matching that regex is accepted and
    # answered with a balance of 0. Our offline validation is strictly stronger, which is
    # now a measurement rather than a preference.
    #
    # A `Literal` rather than the `KaspaNetwork` enum itself, so that a typo in the
    # environment is a startup failure naming the three acceptable values.
    kaspa_network: Literal["mainnet", "testnet", "devnet"] = "mainnet"

    # The one credential the price providers can take, and the only one of the four price
    # sources that needs any. `SecretStr` for the reason `bootstrap_password` is one: the
    # value stays out of reprs, tracebacks and model dumps. It is never persisted, never
    # returned by an endpoint and never logged -- it travels as a request header on the
    # one call that uses it and nowhere else.
    #
    # **`None` means the keyed source is not built at all, rather than built and skipped.**
    # `providers.prices.registry.price_sources` omits it from the tuple, so with no key there
    # is no object holding a blank credential and no code path that could reach the
    # vendor. Criterion 5 asks for "works with and without an API key"; absent is the only
    # spelling of "without" that cannot be defeated by a later caller reaching past the
    # check.
    #
    # Deliberately **not** validated for *shape* by `_refuse_unsafe_configuration`. There
    # is no form a CoinGecko key has to take that this application knows, and a length or
    # prefix rule invented here would refuse a valid key the day the vendor changes its
    # format. A wrong key surfaces as a refusal from that one source, which the failover
    # moves past.
    # **A blank value is a configured key, not an absent one**, and that is a decision
    # rather than an oversight. `PORTFOLIO_COINGECKO_API_KEY=` yields `SecretStr("")`,
    # which is not `None`, so the source is built and the vendor answers 401 -- and the
    # shared transport logs that at error level, naming the host and the endpoint label.
    # Normalising the blank to `None` instead would drop the source silently, and an
    # operator who typed the variable and got three sources would have nothing at all to
    # look at.
    #
    # Note what makes the cost of the noisy option small: this source is a *last-resort
    # fallback*, so `fetch_prices` never reaches it on a healthy day. The 401 appears only
    # on the days the operator is already looking at a log.
    #
    # It is also the one shape rule this application would be making about a vendor's
    # credential format, which is exactly what the paragraph above refuses to do.
    coingecko_api_key: SecretStr | None = None

    # The Bitget API key, its secret and its passphrase: the credentials the spot fills import
    # signs with. **Read-only**, created by the owner on the venue, and `docs/operations.md`
    # says how. `SecretStr` for the reason `bootstrap_password` is one, and the API key and the
    # passphrase are secrets too -- rule 3 names API keys, and the three together are what
    # reads the owner's trading history. Never persisted, never returned by an endpoint, never
    # logged: they travel in request headers on the one call that uses them.
    #
    # **All three or none.** `None` for all three means the venue is not configured, and
    # `providers.exchanges.registry.exchange_providers` then does not build it -- absent, not
    # built and skipped, the rule the CoinGecko key set. Some set and some not is refused at
    # startup, naming the missing variables.
    #
    # **A blank value is refused at startup, unlike the CoinGecko key.** A blank CoinGecko key
    # reaches its vendor and comes back as a 401 the transport logs, which is the diagnosable
    # outcome that setting chose. A blank Bitget credential never reaches the venue:
    # `Credentials` refuses it at construction, so the failure would surface on the first sync
    # instead of at the start. Refusing it here is the same fact, reported where the
    # deployment pipeline rolls back.
    #
    # **The key and the passphrase must also be text a header can carry** (`HEADER_SAFE_TEXT`):
    # printable ASCII with no whitespace at either end. Both are sent as header values, and a
    # value h11 refuses comes back as a transport error whose message is the value itself.
    # The secret is exempt; it only ever enters an HMAC.
    bitget_api_key: SecretStr | None = None
    bitget_api_secret: SecretStr | None = None
    bitget_api_passphrase: SecretStr | None = None

    # The balance scheduler. Three settings, and each answers a question an operator
    # actually has.
    #
    # `enabled` is an off switch that is not a code edit: an operator debugging a vendor --
    # or waiting out a public index's bad afternoon -- needs a way to stop the loop without
    # rebuilding an image. **It does not disable `POST /api/balances/sync`**, deliberately:
    # the manual trigger is the tool they are debugging *with*, and taking it away with the
    # same switch would be the opposite of the intent.
    #
    # `interval_minutes` is the issue's default of fifteen. Whole minutes, because every
    # interval this product will ever want is one and because `float` is banned in the layer
    # that consumes it.
    #
    # `shutdown_grace_seconds` is how long the lifespan waits for a run in flight before
    # cancelling it and letting the sweep record it interrupted. Ten seconds is short of a
    # slow sync and long enough for an ordinary one; the cost of being wrong in either
    # direction is a row marked `interrupted` rather than lost data, because the run writes
    # its snapshots per chain as it goes.
    balance_sync_enabled: bool = True
    balance_sync_interval_minutes: int = 15
    balance_sync_shutdown_grace_seconds: int = 10

    # The price refresh, on its own timer and its own switch. **Separate from the balance
    # pair rather than folded into it**, because the two answer to different vendors on
    # different schedules: balances come from two chain indexes that ban you for asking too
    # often, prices from four market-data APIs where the primary answers every configured
    # pair in a single call. One switch for both would mean an operator waiting out a chain
    # outage also stopped valuing the balances they already had.
    #
    # Sixty minutes because `services.prices.STALE_AFTER` is one hour: a price that has
    # missed exactly one refresh is the first one worth flagging, and an interval longer than
    # the staleness threshold would mark every price stale in the minutes before each run.
    # The two numbers are a pair, and changing one without the other is the mistake this
    # comment exists to prevent.
    price_refresh_enabled: bool = True
    price_refresh_interval_minutes: int = 60

    # The exchange fill sync (#15), on its own timer, switch and grace period, for the reason
    # the price refresh has its own: it answers to different vendors, and they are the ones
    # that sign in with the owner's key.
    #
    # `history_start` is the earliest date the owner wants fills from, at 00:00 UTC. Unset
    # means "all of it" -- 2009-01-03 -- and in either case the venue's retention clamps it
    # further, which `GET /api/exchanges` reports as `requested_since` against
    # `effective_since`. A date after today's (UTC) is refused at startup: it would plan
    # nothing, and the owner would read that as a sync that works and finds no trades.
    # Moving it earlier later is supported -- the next run plans the older range, while
    # retention still allows it.
    #
    # `enabled` switches the timer off and nothing else: `POST /api/exchanges/sync` still
    # works, as `POST /api/balances/sync` does with its switch off. The timer is also not
    # built when no venue is configured, so an install without exchange credentials writes no
    # empty run every fifteen minutes.
    #
    # `shutdown_grace_seconds` is the balance sync's ten, and the cost of it running out is
    # the same: every page is committed as it is read, so a cancelled run loses at most the
    # page in flight, and the sweep marks the run `interrupted`.
    exchange_history_start: date | None = None
    exchange_sync_enabled: bool = True
    exchange_sync_interval_minutes: int = 15
    exchange_sync_shutdown_grace_seconds: int = 10

    @property
    def session_cookie_name(self) -> str:
        """`__Host-psid`, degrading to `psid` on the one configuration that cannot use it."""
        if self.session_cookie_secure:
            return SECURE_SESSION_COOKIE_NAME
        return INSECURE_SESSION_COOKIE_NAME

    @model_validator(mode="after")
    def _refuse_unsafe_configuration(self) -> Self:
        """Fail at construction, so an unsafe configuration never becomes a running server.

        Each of these is something a deployment gets wrong silently otherwise:

        * a bootstrap password that is blank or one of the well-known defaults creates a
          real account with a password an attacker already has;
        * `prod` without a `Secure` cookie means the session cookie loses the `__Host-`
          prefix, and with it the guarantee that no other host on the domain set it;
        * `prod` still carrying the development origin rejects every write with a 403
          while the health check stays green -- "login works, nothing else does", a
          symptom that does not name its cause;
        * `prod` below the OWASP cost floor hashes passwords fast enough to be worth
          cracking, and says nothing about it at all. The realistic way to arrive there is
          not malice: `memory_cost` is in KiB, so an operator tuning after a
          `hash-benchmark` run and reading the number as MiB sets 64 and drops the cost by
          a factor of a thousand.
        * a provider base URL with no scheme, no host or a mistyped scheme cannot be
          requested, and reaches a caller either as a bare `ValueError` out of `urllib` --
          past the `except httpx.TransportError` that is supposed to be where `httpx` stops
          -- or, for the scheme typo, as "the chain is unavailable" on every sync forever
          while nothing mentions the typo. `provider_url_violation` says which.
        * a balance sync or price refresh interval of zero or less is a loop with no sleep
          in it, pointed at public APIs that document a ban as the consequence of asking too
          often. The per-host rate limiter would pace the requests, so the symptom is not a
          burst -- it is a process that never stops making them, quietly, for as long as it
          is up.
        * a partial or blank set of Bitget credentials cannot sign a request, and would be
          discovered on the first exchange sync rather than here. `exchange_credentials_violation`
          says which variable, and never what it holds. Nor can a key or passphrase holding a
          character no HTTP header can carry -- and that one would also write the value into
          a transport error's message. `credential_header_violation` says which.
        * an exchange sync interval below one is the same loop without a sleep, pointed at a
          venue that signs in with the owner's key; and an exchange history start after
          today's UTC date plans nothing, which the owner would read as a working sync that
          found no trades.

        Refusing to start turns every one of these into a container that fails its health
        check, which is a failure the deployment pipeline already knows how to roll back.

        **Unconditional, not gated on `prod`.** A URL that cannot be requested is wrong in
        development too, and the case for gating the cost floor -- that the test suite runs
        deliberately below it -- has no counterpart here: every test that builds a
        `Settings` either leaves these at their defaults or passes a real-looking URL.

        ## Never serialise the `ValidationError` these raises produce

        Measured, and it is not what the `SecretStr` on `bootstrap_password` leads anyone
        to expect:

        | Rendering | Carries a `PORTFOLIO_*` value? |
        |---|---|
        | `str(exc)`, `repr(exc)` | no, since #13 -- `hide_input_in_errors` drops the input |
        | `exc.errors()` | **yes, in plaintext** |
        | `exc.json()` | **yes, in plaintext** |

        **Before #13 the first row was wrong, and it said "no" anyway.** Pydantic elides the
        *middle* of the echoed input and keeps both ends, so `str(exc)` carried the start of
        the first variable in the dict and the end of the last. Measured on #13: with the
        Bitget key and secret set and the passphrase missing, the message held the key's
        first five characters and the secret's last twenty. `hide_input_in_errors=True` on
        `model_config` now removes `input_value` and `input_type` from `str()` and `repr()`
        entirely. It does not touch the other two rows.

        Each error entry carries an `input` dict holding every `PORTFOLIO_*` variable as
        the raw environment string -- which is to say *before* pydantic coerced it into the
        `SecretStr` that would have masked it. The field type protects a value that has
        been parsed; it cannot protect the copy of the input that failed to parse.

        Two things keep that off stdout today, and neither is a rule anybody stated. Only
        `str(exc)` reaches the log when the process refuses to start -- and since #13 it
        carries no input at all -- and the one caller of
        `.errors()` in this application -- `api/errors.py` -- is registered for a
        `RequestValidationError` from a request body and projects each entry down to
        `loc`, `msg` and `type`, dropping `input` before anything is rendered. So the
        hazard is a future `logger.exception`, a debug dump, or a startup handler written
        to be helpful.

        `tests/providers/test_provider_urls.py` pins the unsafe outcome deliberately, so
        that anything which starts redacting it announces itself rather than looking like a
        regression. Do not turn that assertion around; if this is to be fixed it is fixed
        at the startup boundary, which is a decision with a caller behind it.

        The cost floor is the one check gated on `prod` for a reason beyond symmetry: the
        test suite runs the real application at `memory_cost=64` so that it can hash
        several hundred times in a few seconds, and a floor that applied in `dev` would
        make that impossible rather than merely slow.
        """
        if self.bootstrap_password is not None:
            reason = policy_violation(self.bootstrap_password.get_secret_value())
            if reason is not None:
                message = f"PORTFOLIO_BOOTSTRAP_PASSWORD is not acceptable: {reason}"
                raise ValueError(message)
        if self.environment == "prod" and not self.session_cookie_secure:
            message = (
                "PORTFOLIO_SESSION_COOKIE_SECURE cannot be false in production: the "
                "session cookie would lose its __Host- prefix and travel over plain HTTP."
            )
            raise ValueError(message)
        if self.environment == "prod" and self.allowed_origin == DEV_ALLOWED_ORIGIN:
            message = (
                "PORTFOLIO_ALLOWED_ORIGIN must be set to the deployed origin in "
                "production; it is still the development default."
            )
            raise ValueError(message)
        if self.environment == "prod" and self.argon2_memory_cost < OWASP_MINIMUM_MEMORY_COST:
            message = (
                f"PORTFOLIO_ARGON2_MEMORY_COST is {self.argon2_memory_cost}, below the "
                f"OWASP minimum of {OWASP_MINIMUM_MEMORY_COST}. The unit is KiB, not MiB: "
                f"{OWASP_MINIMUM_MEMORY_COST} KiB is 19 MiB."
            )
            raise ValueError(message)
        if self.environment == "prod" and self.argon2_time_cost < OWASP_MINIMUM_TIME_COST:
            message = (
                f"PORTFOLIO_ARGON2_TIME_COST is {self.argon2_time_cost}, below the OWASP "
                f"minimum of {OWASP_MINIMUM_TIME_COST}."
            )
            raise ValueError(message)
        for variable, minutes, switch in (
            (
                "PORTFOLIO_BALANCE_SYNC_INTERVAL_MINUTES",
                self.balance_sync_interval_minutes,
                "PORTFOLIO_BALANCE_SYNC_ENABLED",
            ),
            (
                "PORTFOLIO_PRICE_REFRESH_INTERVAL_MINUTES",
                self.price_refresh_interval_minutes,
                "PORTFOLIO_PRICE_REFRESH_ENABLED",
            ),
            (
                "PORTFOLIO_EXCHANGE_SYNC_INTERVAL_MINUTES",
                self.exchange_sync_interval_minutes,
                "PORTFOLIO_EXCHANGE_SYNC_ENABLED",
            ),
        ):
            if minutes < 1:
                message = (
                    f"{variable} must be at least 1, got {minutes}. Zero or less is a loop "
                    "with no sleep in it against a public API that documents a ban as the "
                    f"consequence. To stop that timer, set {switch}=false."
                )
                raise ValueError(message)
        for name, url in (
            ("PORTFOLIO_BITCOIN_ESPLORA_URL", self.bitcoin_esplora_url),
            ("PORTFOLIO_BITCOIN_ESPLORA_FALLBACK_URL", self.bitcoin_esplora_fallback_url),
            ("PORTFOLIO_KASPA_API_URL", self.kaspa_api_url),
            ("PORTFOLIO_KASPA_API_FALLBACK_URL", self.kaspa_api_fallback_url),
        ):
            reason = provider_url_violation(url)
            if reason is not None:
                message = f"{name} is not usable: {reason}"
                raise ValueError(message)
        reason = exchange_credentials_violation(
            (
                ("PORTFOLIO_BITGET_API_KEY", self.bitget_api_key),
                ("PORTFOLIO_BITGET_API_SECRET", self.bitget_api_secret),
                ("PORTFOLIO_BITGET_API_PASSPHRASE", self.bitget_api_passphrase),
            )
        )
        if reason is not None:
            raise ValueError(reason)
        reason = credential_header_violation(
            (
                ("PORTFOLIO_BITGET_API_KEY", self.bitget_api_key),
                ("PORTFOLIO_BITGET_API_PASSPHRASE", self.bitget_api_passphrase),
            )
        )
        if reason is not None:
            raise ValueError(reason)
        if (
            self.exchange_history_start is not None
            and self.exchange_history_start > datetime.now(UTC).date()
        ):
            # The value is not quoted, by the rule every refusal here follows: name the
            # variable and the rule. The owner has the value in the file they just edited.
            message = (
                "PORTFOLIO_EXCHANGE_HISTORY_START is after today's date in UTC. A history "
                "start in the future would plan nothing to import; set a date on or before "
                "today, or unset it to import everything the venue still keeps."
            )
            raise ValueError(message)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed from the environment exactly once."""
    return Settings()
