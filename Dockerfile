# syntax=docker/dockerfile:1
#
# One image serving both the API and the single-page application.
#
# This image is public: it must never contain a secret. Runtime credentials come from an
# env_file on the host, which is never part of the build context (see .dockerignore).

# ---------------------------------------------------------------------------------------
# Stage 1 - build the single-page application
# ---------------------------------------------------------------------------------------
FROM node:25-alpine AS frontend

WORKDIR /build

# Copy the manifests alone first so a source-only change does not invalidate the dependency
# layer. npm ci is the slowest step in this file on arm64.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------------------
# Stage 2 - resolve the Python dependencies
# ---------------------------------------------------------------------------------------
FROM python:3.12-slim AS backend

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies only. The application source is copied into the runtime stage and run from
# PYTHONPATH rather than installed, so that the built SPA lands at a predictable path
# instead of depending on wheel data-file inclusion rules.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# ---------------------------------------------------------------------------------------
# Stage 3 - runtime
# ---------------------------------------------------------------------------------------
FROM python:3.12-slim

ARG APP_VERSION=0.0.0-dev

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORTFOLIO_VERSION=${APP_VERSION} \
    PORTFOLIO_ENVIRONMENT=prod

# Non-root, with a data directory it owns. The SQLite database is the only permanent record
# of trade history once an exchange's retention window passes, so the volume matters.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown app:app /app/data

WORKDIR /app

COPY --from=backend --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app backend/src /app/src
COPY --from=frontend --chown=app:app /build/dist /app/src/portfolio/web/dist

# Fail the build here rather than discovering at deploy time that the image serves no UI or
# cannot import its own application.
#
# The second check builds the application, which constructs `Settings`, and this image
# sets PORTFOLIO_ENVIRONMENT=prod above -- so every refusal gated on production applies
# here, at build time, with none of the deployment's environment file present. That is
# why the origin is supplied inline. It is a reserved `.invalid` name that can never
# resolve, and it never reaches a running container: the real value comes from the
# host's secrets file at deploy time.
#
# Anything else gated on production has to be supplied here too, or the build fails on a
# validation error rather than a missing route.
# `backend/tests/test_image_configuration.py` reproduces this construction, so that the
# failure lands in the test suite instead of in a multi-architecture image build.
RUN python -c "import pathlib, sys; p = pathlib.Path('/app/src/portfolio/web/dist/index.html'); sys.exit(0) if p.is_file() else sys.exit('The SPA bundle is missing from the image')" \
    && PORTFOLIO_ALLOWED_ORIGIN=https://image-smoke-check.invalid \
       python -c "from portfolio.main import create_app; assert '/api/health' in create_app().openapi()['paths'], 'The health route is missing from the OpenAPI schema'"

USER app

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=6s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import json,urllib.request,sys; d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5)); sys.exit(0 if d.get('status')=='ok' else 1)"]

# A single worker on purpose: the application owns an in-process scheduler, and a second
# worker would run every sync twice against rate-limited public APIs.
CMD ["uvicorn", "portfolio.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
