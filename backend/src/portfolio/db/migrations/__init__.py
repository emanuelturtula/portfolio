"""Alembic migration environment, shipped inside the package.

It lives under `src/portfolio/` rather than at `backend/alembic/` because the Docker image
copies `backend/src` and nothing else: a migration directory outside `src/` would simply
not exist in production, and the container would come up against an empty database file.
"""
