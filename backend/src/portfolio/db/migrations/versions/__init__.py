"""Revision scripts.

This package has an `__init__.py` so that import-linter can see inside it. grimp prunes a
directory that sits inside an already-found package but has no `__init__.py`, and never
collects its modules -- which meant a data migration could `import portfolio.services`,
invert the layering contract, and still be reported as "3 kept, 0 broken".

The files are named `v<revision>.py` because a revision id starting with a digit is not a
valid Python identifier, and a package cannot contain a module that cannot be named. The
`revision` strings inside the files are unchanged: those are what `alembic_version` stores,
and renaming one would strand every database already stamped with it. Alembic finds
revisions by scanning this directory and reading each module's `revision` attribute, so the
file name carries no meaning, and its own filename filter already excludes `__init__.py`
from revision discovery.
"""
