"""The one type in the exchange seam that holds secrets, and why it cannot render them.

Credentials come from environment variables into `SecretStr` (rule 3), are handed to the
provider that signs with them, and go nowhere else: not into a column, not into a response,
not into a log. #13 and #14 add the `PORTFOLIO_*` settings each venue's credentials are
read from; this module only fixes the shape they arrive in.

**The API key is treated as a secret too.** It is not what signs a request, but rule 3
names API keys, and a key is half of what an attacker needs and the half that identifies
the account. Every field is a `SecretStr`.

**Masking is by construction rather than by care.** `SecretStr` already renders as
`**********`, so even the dataclass default `__repr__` would not leak. The fixed
`__repr__` and `__str__` below are there anyway, for two reasons: they do not depend on
what a later pydantic release decides a `SecretStr` repr looks like, and they say plainly
in a log line which fields exist -- `passphrase=None` when there is none, because whether a
venue needs a passphrase is configuration, not a secret.
"""

from __future__ import annotations

from dataclasses import dataclass

# A real import, not a `TYPE_CHECKING` one: `__post_init__` checks against it at run time.
from pydantic import SecretStr

__all__ = ["Credentials"]

_REDACTED = "<redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class Credentials:
    """An exchange API key, its secret and, for the venues that use one, a passphrase.

    Frozen and slotted: nothing replaces a field after the checks below have run, and
    there is no `__dict__` for a generic serialiser to walk.

    Refused at construction, each naming the field and never the value:

    * a plain `str` in any field (`TypeError`) -- a string that was never wrapped is a
      string something has already had the chance to log;
    * a blank value (`ValueError`) -- an empty or whitespace secret is a missing setting,
      and it should fail here rather than as an auth error from the venue, where it looks
      exactly like a revoked key.
    """

    api_key: SecretStr
    api_secret: SecretStr
    passphrase: SecretStr | None = None

    def __post_init__(self) -> None:
        """Refuse an unwrapped or blank secret, naming the field and never its value.

        Raises:
            TypeError: a field is not a `SecretStr`.
            ValueError: a field is empty or whitespace.
        """
        _require_secret(self.api_key, field="api_key")
        _require_secret(self.api_secret, field="api_secret")
        if self.passphrase is not None:
            _require_secret(self.passphrase, field="passphrase")

    def __repr__(self) -> str:
        """A fixed rendering that names the fields and shows none of their values."""
        passphrase = "None" if self.passphrase is None else _REDACTED
        return f"Credentials(api_key={_REDACTED}, api_secret={_REDACTED}, passphrase={passphrase})"

    def __str__(self) -> str:
        """The same fixed rendering as `__repr__`."""
        return self.__repr__()


def _require_secret(value: object, *, field: str) -> None:
    """Refuse a field that is not a non-blank `SecretStr`.

    Takes `object` so the type check is not statically dead: the dataclass annotation is a
    promise `mypy` keeps for our code and nobody keeps for a value assembled from the
    environment at run time.
    """
    if not isinstance(value, SecretStr):
        message = f"Credentials.{field} must be a SecretStr, got {type(value).__name__}"
        raise TypeError(message)
    if not value.get_secret_value().strip():
        message = f"Credentials.{field} is blank"
        raise ValueError(message)
