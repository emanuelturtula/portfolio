"""Tests for the operator commands, `create-user` and `hash-benchmark` (#3).

A package rather than a loose directory: mypy runs in strict mode over `tests`, and two
`conftest.py` modules outside packages collide on the module name and silently stop the
check.
"""
