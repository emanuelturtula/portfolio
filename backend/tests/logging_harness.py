"""Put the logging configuration back exactly as it was found, which `reset_defaults` does not.

Three fixtures in this suite reconfigure logging on purpose -- two tests render a real
production log line to prove an address or a key is absent from it, and one reads
`alembic.ini`, whose `fileConfig` section installs handlers of its own. Each of them used to
tear down with `structlog.reset_defaults()`, and the name is the trap: it does not restore
the configuration in effect before the test, it installs **structlog's** defaults.

Those defaults are not neutral. Measured on structlog 26.1.0, the default chain ends in a
`ConsoleRenderer` whose exception formatter is `RichTracebackFormatter(show_locals=True)`,
and it has no `format_exc_info` in front of it. The application's own chain -- installed by
`configure_logging`, which importing `portfolio.main` runs -- has the same renderer class
but puts `format_exc_info` ahead of it, so an exception reaches the renderer as a plain
string and nothing ever walks a frame's locals.

So the first test to use one of those fixtures silently swapped the application's logging
for one that pretty-prints every local variable of every frame, for the rest of the session.
Nothing noticed until #10, the first code under test that calls `_logger.exception()` with
SQLAlchemy objects in scope: `rich.pretty.traverse` walked into a column type's
`__getattr__` and never came back, and the suite hung on whichever balance-sync test ran
after a security test. On Windows, interrupting it produced an access violation.

That is also a leak, not only a hang. A renderer that prints frame locals prints whatever a
frame was holding -- an address, a bound SQL parameter, a credential on its way into a
header -- which is precisely what `tests/security/` exists to keep out of a log.

## What is restored

Everything `portfolio.logging.configure_logging` touches, and nothing it does not:

* structlog's global configuration, all five keys, from `structlog.get_config()`;
* the root logger's handlers and level, which `logging.basicConfig(force=True)` replaces;
* the level of each logger in `URL_LOGGING_LIBRARIES`, which `silence_vendor_url_logging`
  raises. Every call raises them to the same floor today, so this half restores nothing in
  practice -- it is here so that it keeps restoring the day that stops being true.

`tests/test_logging_harness.py` drives each fixture's real teardown and compares the state
afterwards with the state before, so a fourth fixture written the old way, or this one
edited back to `reset_defaults()`, fails there rather than as a hang somewhere else.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import structlog

from portfolio.logging import URL_LOGGING_LIBRARIES

if TYPE_CHECKING:
    from collections.abc import Iterator


def logging_state() -> dict[str, Any]:
    """Everything `configure_logging` can change, in a form two snapshots can be compared in.

    The processor list is copied rather than referenced. `structlog.configure` replaces the
    list rather than mutating it today, so a reference would work -- until a processor is
    ever appended in place, at which point a snapshot taken by reference would change along
    with the thing it was meant to remember.
    """
    config = structlog.get_config()
    root = logging.getLogger()
    return {
        "structlog": {**config, "processors": list(config["processors"])},
        "root_handlers": list(root.handlers),
        "root_level": root.level,
        "vendor_levels": {
            library: logging.getLogger(library).level for library in URL_LOGGING_LIBRARIES
        },
    }


@contextmanager
def preserved_logging() -> Iterator[None]:
    """Run a block that reconfigures logging, then put back exactly what was there before.

    Restored in a `finally`, so a test that fails half way through its own reconfiguration
    still leaves the session as it found it -- which is when a leak is most likely to go
    unnoticed, because the failure everyone looks at is the first one.
    """
    before = logging_state()
    try:
        yield
    finally:
        structlog.configure(**before["structlog"])
        root = logging.getLogger()
        root.handlers[:] = before["root_handlers"]
        root.setLevel(before["root_level"])
        for library, level in before["vendor_levels"].items():
            logging.getLogger(library).setLevel(level)
