"""Tests for single-user password login with server-side sessions (#3).

A package rather than a loose directory, and not only for tidiness: mypy runs in strict
mode over `tests` as well as `src`, and two `conftest.py` files that are not inside
packages collide on the module name `conftest` -- at which point mypy stops checking
rather than complaining.
"""
