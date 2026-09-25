"""Which exchange venues exist in this process, and why an unconfigured one is not there at all.

One function, the shape of `providers/prices/registry.py`: the protocol and the vocabulary
live in `base`, and the table of what is actually wired lives here, in one short file a
reader looking for "what does this application call" can find.

**A venue without credentials is absent, not built.** The rule the CoinGecko key set: a
provider that exists and checks for its credentials at call time is one line away from a
caller that reaches past the check, and "the object does not exist" is the only version of
that guarantee a later caller cannot defeat. There is no object holding a missing key and no
code path that could sign a request without one.

Nothing is auto-discovered, for the reason `providers/registry.py` gives about
`pkgutil.walk_packages`: a venue that was written but never wired should be visible as a
missing line here rather than as fills that quietly never arrive. BingX arrives with #14.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from portfolio.config import get_settings
from portfolio.domain.exchanges import ExchangeKey
from portfolio.providers.exchanges.bitget import BitgetProvider, bitget_credentials

if TYPE_CHECKING:
    from collections.abc import Mapping

    import httpx

    from portfolio.config import Settings
    from portfolio.providers.exchanges.base import ExchangeProvider

__all__ = ["exchange_providers"]


def exchange_providers(
    client: httpx.AsyncClient,
    *,
    settings: Settings | None = None,
) -> Mapping[ExchangeKey, ExchangeProvider]:
    """Every exchange venue this process has credentials for, keyed by venue.

    **Bitget is present only when its credentials are configured**, and the test is
    `bitget_credentials(...) is None` -- presence, never truthiness. `Settings` has already
    refused a partial set and a blank value at startup, so the only two cases here are "all
    three set" and "none set".

    Building the table makes no request: a provider's constructor binds to the client and
    keeps its credentials, and nothing more.

    Args:
        client: the shared `httpx.AsyncClient`, whose transport carries the retry policy and
            the per-host rate limiter. Every provider is bound to this one client, for the
            reason `build_http_client` gives.
        settings: the configuration to read. `None` reads the process-wide settings; a test
            passes its own rather than monkeypatching the environment.

    Returns:
        A read-only mapping. Empty when no venue is configured.
    """
    resolved = settings if settings is not None else get_settings()
    providers: dict[ExchangeKey, ExchangeProvider] = {}
    credentials = bitget_credentials(resolved)
    if credentials is not None:
        providers[ExchangeKey.BITGET] = BitgetProvider(client, credentials)
    return MappingProxyType(providers)
