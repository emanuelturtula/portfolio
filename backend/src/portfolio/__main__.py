"""Entry point for `python -m portfolio`.

Kept to one line of behaviour so that the commands themselves stay importable and
testable without a subprocess: `main` returns an exit code, and only this module turns
one into a process exit.
"""

from __future__ import annotations

import sys

from portfolio.cli import main

if __name__ == "__main__":
    sys.exit(main())
