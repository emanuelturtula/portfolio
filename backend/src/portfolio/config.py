"""Application settings, loaded from the environment.

Every value is read from a `PORTFOLIO_`-prefixed environment variable so that the same
image can run in development and in production without a rebuild. Nothing in this module
carries a default that would be unsafe if it survived into production.
"""

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


class Settings(BaseSettings):
    """Runtime configuration for the backend."""

    model_config = SettingsConfigDict(
        env_prefix="PORTFOLIO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
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
    # `providers.prices.base.price_sources` omits it from the tuple, so with no key there
    # is no object holding a blank credential and no code path that could reach the
    # vendor. Criterion 5 asks for "works with and without an API key"; absent is the only
    # spelling of "without" that cannot be defeated by a later caller reaching past the
    # check.
    #
    # Deliberately **not** validated by `_refuse_unsafe_configuration`. There is no shape
    # a CoinGecko key has to have that this application knows, and a length or prefix rule
    # invented here would refuse a valid key the day the vendor changes its format. A
    # wrong key surfaces as a refusal from that one source, which the failover moves past.
    coingecko_api_key: SecretStr | None = None

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

        Refusing to start turns all five into a container that fails its health check,
        which is a failure the deployment pipeline already knows how to roll back.

        **Unconditional, not gated on `prod`.** A URL that cannot be requested is wrong in
        development too, and the case for gating the cost floor -- that the test suite runs
        deliberately below it -- has no counterpart here: every test that builds a
        `Settings` either leaves these at their defaults or passes a real-looking URL.

        ## Never serialise the `ValidationError` these raises produce

        Measured, and it is not what the `SecretStr` on `bootstrap_password` leads anyone
        to expect:

        | Rendering | Carries `PORTFOLIO_BOOTSTRAP_PASSWORD`? |
        |---|---|
        | `str(exc)` | no -- pydantic elides the middle of the input |
        | `exc.errors()` | **yes, in plaintext** |
        | `exc.json()` | **yes, in plaintext** |

        Each error entry carries an `input` dict holding every `PORTFOLIO_*` variable as
        the raw environment string -- which is to say *before* pydantic coerced it into the
        `SecretStr` that would have masked it. The field type protects a value that has
        been parsed; it cannot protect the copy of the input that failed to parse.

        Two things keep that off stdout today, and neither is a rule anybody stated. Only
        `str(exc)` reaches the log when the process refuses to start, and the one caller of
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
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed from the environment exactly once."""
    return Settings()
