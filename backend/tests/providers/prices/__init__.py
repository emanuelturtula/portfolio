"""Tests for the price sources.

This file is not empty by accident. Without it `mypy --strict` over `tests` meets two
`conftest` modules with the same module name and stops with "Duplicate module named
'conftest'" -- which does not fail the type check loudly so much as end it early, leaving
whatever it had not reached unchecked.
"""
