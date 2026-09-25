"""Tests for the exchange provider seam (#12): the protocol, the fill, the errors, signing.

This package needs an `__init__.py` for the same reason `tests/providers/chains/` does:
without one, `mypy --strict` over `tests` meets two modules that resolve to the same name
and stops before it has checked anything.
"""
