"""The logging fixtures put back what they found, and the application never logs frame locals.

`tests/logging_harness.py` carries the account of the defect. The short version: three
fixtures tore down with `structlog.reset_defaults()`, which installs structlog's defaults
rather than restoring the configuration in effect before the test, and structlog's default
renderer walks every local variable of every frame in a traceback. The first security test
to run silently switched the whole session over to it, and the suite hung the first time a
test under #10 logged an exception with SQLAlchemy objects in scope.

A restore nobody checks is how that shipped, so it is checked here twice over:

* **each fixture's real teardown is driven** and the state afterwards is compared with the
  state before -- not the helper, the fixtures, so one written the old way fails here;
* **the property that actually mattered is asserted directly**: an exception logged through
  the application's own pipeline, in either environment, never has its frame locals
  rendered. That is a claim about `portfolio.logging` rather than about the tests, and it is
  the half that would catch `format_exc_info` being dropped from the chain in `src/`.

Both come with the control that makes them mean something: the hazardous configuration is
installed on purpose, inside `preserved_logging`, and shown to fail each check.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import pytest
import structlog

from portfolio.config import Settings
from portfolio.domain.passwords import OWASP_MINIMUM_MEMORY_COST, OWASP_MINIMUM_TIME_COST
from portfolio.logging import configure_logging
from tests import test_logging_redaction
from tests.db import conftest as db_conftest
from tests.logging_harness import logging_state, preserved_logging
from tests.security import conftest as security_conftest
from tests.security.conftest import PRODUCTION_ORIGIN

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

TESTS_ROOT: Final = Path(__file__).resolve().parent

#: Every fixture in the suite that reconfigures logging for a test and must undo it. Named
#: here rather than discovered, so that a new one is a line in this list and the test above
#: it fails until somebody adds it -- `test_no_test_module_resets_structlog_to_its_defaults`
#: is what catches one that is added without being listed.
LOGGING_FIXTURES: Final[dict[str, Callable[..., Iterator[None]]]] = {
    "tests/security/conftest.py::restored_logging": security_conftest.restored_logging,
    "tests/db/conftest.py::restored_logging": db_conftest.restored_logging,
    "tests/test_logging_redaction.py::restore_logging": test_logging_redaction.restore_logging,
}

#: The one module allowed to call `structlog.reset_defaults()`, because it does so on
#: purpose, inside `preserved_logging`, to show that the hazard these checks guard against is
#: real.
RESET_DEFAULTS_ALLOWED: Final = frozenset({"test_logging_harness.py"})


def production_settings() -> Settings:
    """The settings the Raspberry Pi logs with, clear of the production cost floor."""
    return Settings(
        environment="prod",
        allowed_origin=PRODUCTION_ORIGIN,
        argon2_memory_cost=OWASP_MINIMUM_MEMORY_COST,
        argon2_time_cost=OWASP_MINIMUM_TIME_COST,
    )


def development_settings() -> Settings:
    """The settings a developer's machine logs with, and the renderer that hid the hazard."""
    return Settings(environment="dev", allowed_origin=PRODUCTION_ORIGIN)


def structlog_defaults() -> dict[str, object]:
    """What `structlog.reset_defaults()` installs, captured without leaving it installed."""
    with preserved_logging():
        structlog.reset_defaults()
        return logging_state()


@pytest.fixture(autouse=True)
def application_logging() -> Iterator[None]:
    """Start every test here from the application's own pipeline, and leave no trace after.

    **This fixture is the difference between these tests meaning something and not.** The
    first version of this module took its baseline from whatever the previous test left
    behind, and run against the broken teardowns it failed on the first fixture and then
    *passed* on the other two: the first teardown had installed structlog's defaults, so the
    next test's "before" already was the defaults, and "restored to the defaults" compared
    equal. A restore test whose baseline a broken restore can corrupt is a test that goes
    green precisely when the defect is worst.

    So the baseline is installed here, it is the configuration `create_app()` installs, and
    `test_the_baseline_is_the_application_pipeline_not_structlogs_defaults` asserts it is not
    the thing the old teardowns left behind. The whole module runs inside
    `preserved_logging`, so a fixture that fails to restore cannot corrupt the rest of the
    session from here either.
    """
    with preserved_logging():
        configure_logging(development_settings())
        yield


def run_fixture(fixture: Callable[..., Iterator[None]], body: Callable[[], None]) -> None:
    """Drive a fixture's own generator through setup, `body`, and teardown.

    `inspect.unwrap` follows the `__wrapped__` attribute pytest sets on a fixture definition,
    which is the standard-library convention for reaching the function a decorator wrapped.
    What runs is therefore the fixture's real code, including its `finally`, rather than a
    re-statement of what the fixture is supposed to do.
    """
    generator = inspect.unwrap(fixture)()
    next(generator)
    try:
        body()
    finally:
        with pytest.raises(StopIteration):
            next(generator)


# --------------------------------------------------------------------------------------
# The fixtures restore, and are proven to
# --------------------------------------------------------------------------------------


def test_the_baseline_is_the_application_pipeline_not_structlogs_defaults() -> None:
    """The control on the autouse fixture, so the restore tests cannot pass by coincidence.

    If the baseline were structlog's defaults, a teardown that installed the defaults would
    "restore" it perfectly, and every test below would pass against exactly the defect they
    exist to catch -- which is what happened to the first version of this module.
    """
    assert logging_state() != structlog_defaults()


@pytest.mark.parametrize("name", sorted(LOGGING_FIXTURES))
def test_every_logging_fixture_puts_back_the_configuration_it_found(name: str) -> None:
    """Setup, a production reconfiguration, teardown -- and the state is what it was.

    The comparison covers structlog's whole configuration and the root logger's handlers and
    level, because a fixture that restored one and not the other leaks just as surely: the
    renderer decides what an exception looks like, and the root handler decides which stream
    it goes to.

    The assertion in the middle is the control. If `configure_logging` inside the fixture
    changed nothing, "the state afterwards equals the state before" would be true of a
    fixture that restored nothing at all.
    """
    before = logging_state()

    def reconfigure() -> None:
        configure_logging(production_settings())
        assert logging_state() != before, "the reconfiguration has to change something"

    run_fixture(LOGGING_FIXTURES[name], reconfigure)

    assert logging_state() == before, f"{name} did not put the logging configuration back"


def test_the_harness_restores_even_when_the_block_raises() -> None:
    """A test that fails half way through its own reconfiguration still leaves no trace.

    That is the case a leak is most likely to go unnoticed in: everybody reads the first
    failure, and nobody reads the forty unrelated ones it caused further down the session.
    """
    before = logging_state()

    def reconfigure_and_fail() -> None:
        with preserved_logging():
            configure_logging(production_settings())
            message = "a test that failed after reconfiguring logging"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError):
        reconfigure_and_fail()

    assert logging_state() == before


def test_the_comparison_can_see_a_configuration_that_was_not_restored() -> None:
    """The falsification control for everything above, and the defect as it actually was.

    `structlog.reset_defaults()` -- what the three fixtures used to call -- produces a state
    that `logging_state()` reports as different. Without this, a `logging_state` that
    compared something too coarse to notice would make every restore test pass.
    """
    before = logging_state()

    after = structlog_defaults()

    assert after != before
    assert after["structlog"] != before["structlog"]
    assert logging_state() == before, "and capturing the defaults left nothing behind"


# --------------------------------------------------------------------------------------
# The property that actually mattered: no frame locals, ever
# --------------------------------------------------------------------------------------


class Canary:
    """A local variable that records whether anything rendered it.

    Rich's locals renderer calls `repr()` on every local it walks. A `repr` that records the
    call is therefore a direct observation of the thing that must not happen, rather than an
    inference from output formatting that a renderer is free to change.
    """

    def __init__(self) -> None:
        self.rendered = False

    def __repr__(self) -> str:
        self.rendered = True
        return "<canary>"


def log_an_exception_with_a_canary_in_scope() -> Canary:
    """Raise, catch, and log with `.exception()`, holding a canary as a local of the frame."""
    canary = Canary()
    try:
        message = "a failure logged with a local variable in scope"
        raise ValueError(message)
    except ValueError:
        structlog.get_logger("tests.logging_harness").exception("canary_exception")
    return canary


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_the_application_pipeline_never_renders_frame_locals(
    environment: Literal["dev", "prod"],
) -> None:
    """An exception logged by the application carries a traceback and never the locals.

    Both environments, and `dev` is the one that matters: its `ConsoleRenderer` has
    `RichTracebackFormatter(show_locals=True)` as its exception formatter, exactly as
    structlog's default does. The only reason the application never walks a frame's locals
    is that `format_exc_info` sits ahead of the renderer and turns the exception into a
    string first. Removing that one processor from `configure_logging` would reproduce the
    hang in production code, and would put whatever a frame was holding -- an address, a
    bound parameter -- into the log. This is the test that fails if it goes.
    """
    settings = production_settings() if environment == "prod" else development_settings()

    with preserved_logging():
        configure_logging(settings)
        canary = log_an_exception_with_a_canary_in_scope()

    assert canary.rendered is False


def test_structlogs_own_defaults_do_render_frame_locals() -> None:
    """The control: the canary can detect the hazard, because the hazard is real.

    Under `structlog.reset_defaults()` -- what the old teardowns installed -- the same call
    renders the frame's locals and the canary is touched. Without this, the test above would
    pass for a canary whose `repr` nothing could ever reach.
    """
    with preserved_logging():
        structlog.reset_defaults()
        canary = log_an_exception_with_a_canary_in_scope()

    assert canary.rendered is True


# --------------------------------------------------------------------------------------
# And nobody writes a fourth fixture the old way
# --------------------------------------------------------------------------------------


def reset_defaults_calls(path: Path) -> list[int]:
    """The line of every `structlog.reset_defaults()` call in a file, however it is spelled."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute | ast.Name)
        and (node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id)
        == "reset_defaults"
    ]


def test_no_test_module_resets_structlog_to_its_defaults() -> None:
    """`reset_defaults()` is not a restore, and a new fixture must not reach for it as one.

    The name reads like the right teardown, which is exactly how three fixtures came to use
    it. `LOGGING_FIXTURES` covers the fixtures that exist; this covers the one written next
    month by somebody who has not read `tests/logging_harness.py`.
    """
    offences = [
        f"{path.relative_to(TESTS_ROOT)}:{line}"
        for path in sorted(TESTS_ROOT.rglob("*.py"))
        if path.name not in RESET_DEFAULTS_ALLOWED
        for line in reset_defaults_calls(path)
    ]

    assert offences == [], "use tests.logging_harness.preserved_logging instead"


def test_the_reset_defaults_scan_can_actually_fail(tmp_path: Path) -> None:
    """A scan that found nothing would pass the test above while checking nothing."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import structlog\n"
        "from structlog import reset_defaults\n"
        "structlog.reset_defaults()\n"
        "reset_defaults()\n",
        encoding="utf-8",
    )

    assert reset_defaults_calls(planted) == [3, 4]
    assert reset_defaults_calls(Path(__file__)) != [], "this module's own control is found"
