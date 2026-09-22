"""Tests for the concrete chain providers.

This package needs an `__init__.py` for the same reason `tests/providers/` does, and it is
not decoration. Without one, `mypy --strict` over `tests` meets two `conftest` modules that
resolve to the same module name, reports `Duplicate module named "conftest"` and **stops
checking** -- which would quietly disarm criterion 7 of #6, the static protocol check that
is the only thing standing between a broken provider and a green build.

`EsploraProvider` is assigned to a `ChainProvider`-typed name in
`tests/providers/chains/harness.py` for exactly that reason: the protocol is checked
statically, never with `isinstance`.
"""
