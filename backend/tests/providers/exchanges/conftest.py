"""The production log pipeline, for the two exchange tests that read what a log wrote.

`tests/security/conftest.py` owns the original of these fixtures, and a conftest fixture is
visible only inside its own package, so the exchange suite needs its own. They are written
in terms of the same pieces -- `preserved_logging` for the teardown and `PRODUCTION_ORIGIN`
for the settings -- and `tests/test_logging_harness.py` drives `restored_logging` here
through its real teardown exactly as it does the other three.

The one decision that must not drift: **the pipeline is installed by the test, never in
fixture setup.** `configure_logging` goes through `logging.basicConfig`, which binds whatever
object `sys.stdout` names at the moment it is called. pytest swaps that object between the
setup and call phases and throws the setup buffer away, so a pipeline installed during
setup writes where `capsys.readouterr()` never looks, and every "the secret is absent"
assertion then passes against an empty string. Handing back a callable the test must invoke
is what makes that ordering impossible to get wrong.

Why stdout and not `structlog.testing.capture_logs`: `capture_logs` replaces the processor
chain, so `format_exc_info` never runs and anything carried in an exception is invisible to
the assertion. That is how a real leak passed a green gate on #5.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from portfolio.config import Settings
from portfolio.domain.passwords import OWASP_MINIMUM_MEMORY_COST, OWASP_MINIMUM_TIME_COST
from portfolio.logging import configure_logging
from tests.logging_harness import preserved_logging
from tests.security.conftest import PRODUCTION_ORIGIN

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

type LoggingInstaller = Callable[[], None]


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """Put back the logging configuration that was in effect -- not structlog's defaults.

    `tests/logging_harness.py` explains why the difference once hung the suite.
    """
    with preserved_logging():
        yield


@pytest.fixture
def production_logging(restored_logging: None) -> LoggingInstaller:
    """Hand back an installer for the JSON pipeline the Raspberry Pi runs. Do not install here."""
    del restored_logging  # The fixture's value is its teardown.

    def install() -> None:
        configure_logging(
            Settings(
                environment="prod",
                allowed_origin=PRODUCTION_ORIGIN,
                argon2_memory_cost=OWASP_MINIMUM_MEMORY_COST,
                argon2_time_cost=OWASP_MINIMUM_TIME_COST,
            )
        )

    return install
