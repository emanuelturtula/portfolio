"""`python -m portfolio` is the documented way to run a command, so it is tested as one.

Executed through `runpy` rather than imported, and `portfolio.__main__` is deliberately
not imported at module level here: importing it first makes `runpy` warn that the module
was already in `sys.modules`, and the point of this test is the path an operator actually
takes on the Raspberry Pi.
"""

from __future__ import annotations

import runpy
import sys

import pytest


def test_running_the_module_exits_with_the_command_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--help` rather than a real command.

    This asserts the wiring from `python -m portfolio` to `cli.main`; a command that
    touched the database would be testing something the other files already test. The
    absent `--password` flag is asserted here too, because the help text is where an
    operator would go looking for one.
    """
    monkeypatch.setattr(sys, "argv", ["portfolio", "--help"])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("portfolio", run_name="__main__")

    assert exit_info.value.code == 0
    written = capsys.readouterr().out
    assert "create-user" in written
    assert "hash-benchmark" in written
    assert "--password" not in written
