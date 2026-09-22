"""Tests for the chain provider seam.

This package needs an `__init__.py` and it is not decoration. Without one, `mypy --strict`
over `tests` meets two `conftest` modules that resolve to the same module name, reports
`Duplicate module named "conftest"` and **stops checking**, which would quietly disarm
criterion 7 -- the static check that is the only thing standing between a broken fake and
a green build.
"""
