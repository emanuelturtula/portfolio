"""Application settings, loaded from the environment.

Every value is read from a `PORTFOLIO_`-prefixed environment variable so that the same
image can run in development and in production without a rebuild. Nothing in this module
carries a default that would be unsafe if it survived into production.
"""

from functools import lru_cache
from typing import Final, Literal, Self

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

        Refusing to start turns all four into a container that fails its health check,
        which is a failure the deployment pipeline already knows how to roll back.

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
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed from the environment exactly once."""
    return Settings()
