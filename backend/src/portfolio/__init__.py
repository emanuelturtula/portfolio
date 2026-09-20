"""Backend package for the crypto investment portfolio tracker."""

import os

__all__ = ["__version__"]

# CI bakes the git-tag version into the image through this environment variable; a
# checkout that was never built reports the development fallback instead.
__version__: str = os.getenv("PORTFOLIO_VERSION", "0.0.0-dev")
