"""The image must be able to build its own application at build time.

The Dockerfile sets `PORTFOLIO_ENVIRONMENT=prod` on the image and then, as the last build
step, imports `create_app()` to prove the image can serve the API it ships. Those two facts
together mean **every refusal gated on production is evaluated during `docker build`**, with
none of the deployment's environment file present.

That is not obvious, and it has already bitten once: adding the `PORTFOLIO_ALLOWED_ORIGIN`
refusal turned a green test suite into a failed multi-architecture image build, because the
whole backend suite runs at `environment="dev"` and nothing reproduced the production
construction. The build is the slowest and least legible place to find that out.

So this reproduces it here. The Dockerfile is parsed for the `PORTFOLIO_*` values the image
actually carries -- the `ENV` block plus anything supplied inline on the smoke-check `RUN` --
and `Settings` is constructed from exactly those. A new production refusal that the
Dockerfile does not satisfy fails this test in milliseconds instead of failing CI after an
arm64 build.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final

import pytest

from portfolio.config import Settings

DOCKERFILE: Final = Path(__file__).resolve().parents[2] / "Dockerfile"

# `PORTFOLIO_NAME=value`, wherever it appears: an `ENV` line, or an inline assignment
# prefixing a command in a `RUN`. Both end up in the environment the build step sees.
_ASSIGNMENT: Final = re.compile(r"\b(PORTFOLIO_[A-Z0-9_]+)=(\S+)")

# `PORTFOLIO_VERSION=${APP_VERSION}` is substituted from a build argument, so its literal
# text is not a value. Nothing gated on production depends on it.
_BUILD_ARGUMENT: Final = "${"


def image_environment() -> dict[str, str]:
    """Every `PORTFOLIO_*` value the image carries when the smoke check runs."""
    found: dict[str, str] = {}
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue  # A comment naming a variable is not a value.
        for name, value in _ASSIGNMENT.findall(line):
            if _BUILD_ARGUMENT not in value:
                found[name] = value.rstrip("\\").strip()
    return found


@pytest.fixture
def build_environment(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The process environment as the image's final build step sees it, and nothing else.

    Every ambient `PORTFOLIO_*` is cleared first: a developer with one exported, or a `.env`
    left in the backend directory, would otherwise supply a value the image does not have
    and hide exactly the failure this test exists to catch.
    """
    for name in [key for key in os.environ if key.startswith("PORTFOLIO_")]:
        monkeypatch.delenv(name, raising=False)

    environment = image_environment()
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return environment


def test_the_dockerfile_builds_the_application_in_production_mode(
    build_environment: dict[str, str],
) -> None:
    """The regression: `Settings()` must construct with only what the image provides.

    `_env_file=None` because the deployment's values arrive through compose's `env_file` at
    run time, never at build time, and reading a developer's local `.env` here would make
    the test pass for a reason the build does not share.
    """
    assert build_environment.get("PORTFOLIO_ENVIRONMENT") == "prod", (
        "the image no longer runs as production, so this test is checking nothing"
    )

    settings = Settings(_env_file=None)

    assert settings.environment == "prod"


def test_the_smoke_check_supplies_an_origin(build_environment: dict[str, str]) -> None:
    """Named explicitly, because it is the value whose absence broke the build.

    Asserted as *some* non-default origin rather than a literal one: the point is that the
    build supplies one, not which placeholder it chose.
    """
    origin = build_environment.get("PORTFOLIO_ALLOWED_ORIGIN")

    assert origin is not None
    assert origin != Settings.model_fields["allowed_origin"].default


def test_the_build_origin_never_reaches_a_running_container() -> None:
    """The placeholder must be unresolvable, so a misconfigured deployment cannot use it.

    `.invalid` is reserved by RFC 2606 and can never be registered. If this value were ever
    a real host, an image deployed without its environment file would accept writes from it
    instead of refusing them.
    """
    origin = image_environment()["PORTFOLIO_ALLOWED_ORIGIN"]

    assert origin.endswith(".invalid")
