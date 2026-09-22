"""The machinery for reading what a log actually put on stdout.

These five names were written for `test_address_logging.py` and are now needed by
`test_provider_url_logging.py` as well, so they live here rather than in either module. A
second copy of the `production_logging` fixture would be a second copy of the reason it is
a callable instead of a fixture body -- and that reason is the one thing about this file
that must not drift, because forgetting it turns every "the address is absent" assertion
into a check against an empty string.

**Why stdout and not `structlog.testing.capture_logs`.** `capture_logs` swaps the whole
processor chain out for a `LogCapture`, so `format_exc_info` never runs and an address
carried inside an exception's text is invisible to the assertion. That is not a
hypothetical: it is how a real production leak passed a green gate on #5. A verifier that
replaces the pipeline can only report on the pipeline it installed. The bytes on stdout are
the artifact that actually gets copied, tailed and pasted into an issue, so that is what
these tests read.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Final

import pytest
import structlog

from portfolio.config import Settings
from portfolio.domain.passwords import (
    OWASP_MINIMUM_MEMORY_COST,
    OWASP_MINIMUM_TIME_COST,
)
from portfolio.logging import configure_logging

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

# A PEP 695 alias, so the fixture's type is one name in every signature that takes it.
# `Callable[..., None]` spelled out at each use drifts the moment the parameter list
# changes, which it just did when `log_level` was added.
type ProductionLoggingInstaller = Callable[..., None]

#: A fictional origin, never a real hostname (rule 3). `Settings` refuses to build with
#: `environment="prod"` while `allowed_origin` is still the development default.
PRODUCTION_ORIGIN: Final = "https://portfolio.example"

#: How many leading or trailing characters of an address count as a disclosure. A
#: twenty-character run is as good as the whole string to whoever reads the log: it is
#: unique on chain and it is enough to search an explorer with.
DISCLOSING_RUN: Final = 20


def rendered(entries: Sequence[Mapping[str, Any]]) -> str:
    """Every captured record as one string, so a search cannot miss a nested value."""
    return json.dumps(entries, default=repr)


def forbidden_forms(*addresses: str) -> list[str]:
    """Each address and the spellings of it a log could plausibly carry instead."""
    forms: list[str] = []
    for address in addresses:
        forms.extend((address, address.lower(), address.upper()))
        forms.extend((address[:DISCLOSING_RUN], address[-DISCLOSING_RUN:]))
    return forms


def assert_absent(written: str, *addresses: str) -> None:
    """Fail naming the address **and the line it reached**, not merely that it matched.

    A bare `assert form not in written` says a leak happened. Saying which spelling
    appeared and quoting the log line says *how*, which is the difference between an hour
    of bisecting and reading the answer off the failure.
    """
    for form in forbidden_forms(*addresses):
        if form in written:
            line = next((one for one in written.splitlines() if form in one), "<no line>")
            message = f"an address reached the log as {form[:24]!r}... on this line: {line[:400]}"
            raise AssertionError(message)


def assert_carried_something(written: str, *, marker: str) -> None:
    """The companion every absence assertion needs, or silence passes it.

    An assertion that a string is *not* in the output is satisfied by output that does not
    exist. Twice now a logging test in this repository has passed against an empty capture:
    once because the pipeline was installed before pytest swapped `sys.stdout`, and once
    because the pipeline under test was never the one that ran. So every test that asserts
    an absence also asserts that the log it is reading is the log it meant to read, by
    naming something that must be in it.
    """
    assert written.strip(), "nothing was written to stdout, so an absence proves nothing"
    assert marker in written, (
        f"stdout carried no line containing {marker!r}; "
        f"the log under test never ran. What was captured: {written[:400]!r}"
    )


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """Undo the global logging configuration these tests install."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    try:
        yield
    finally:
        structlog.reset_defaults()
        root.handlers[:] = handlers
        root.setLevel(level)


@pytest.fixture
def production_logging(restored_logging: None) -> ProductionLoggingInstaller:
    """Hand back an installer the test calls; **do not install here**.

    `configure_logging` goes through `logging.basicConfig`, which binds whatever object
    `sys.stdout` names at the moment it is called. pytest swaps that object between the
    setup and call phases and throws the setup phase's buffer away, so a pipeline
    installed in fixture setup writes into a buffer `capsys.readouterr()` never returns --
    and every assertion of the form "the address is not in the output" then passes against
    an empty string. That is the vacuous-pass failure this module has now hit twice, so
    the ordering requirement is expressed as a callable the test has to invoke rather than
    as a convention somebody has to remember.

    `environment="prod"` because the JSON renderer is what the Raspberry Pi emits and
    rendering is exactly what is under test. The Argon2 parameters are passed explicitly
    to clear the production floor: the suite's environment sets them deliberately cheap,
    and `Settings` is right to refuse those values in production.

    `log_level` is a parameter rather than a constant because a provider's success log is
    at debug, and a test of what a successful request writes has to be able to turn debug
    on. The default stays at the production default, so no existing caller changes
    meaning.
    """
    del restored_logging  # The fixture's value is its teardown.

    def install(log_level: str = "INFO") -> None:
        configure_logging(
            Settings(
                environment="prod",
                allowed_origin=PRODUCTION_ORIGIN,
                log_level=log_level,
                argon2_memory_cost=OWASP_MINIMUM_MEMORY_COST,
                argon2_time_cost=OWASP_MINIMUM_TIME_COST,
            )
        )

    return install
