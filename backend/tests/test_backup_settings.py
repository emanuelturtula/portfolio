"""Spec 029 (#22): the five backup settings, their defaults, and the values refused at startup.

Each refusal turns into a container that fails its health check, which the deployment
already knows how to roll back, rather than a server that copies the database in a loop or
deletes the copy it has just taken. The messages are pinned whole, because the operator
reads them in the container log with nothing else to go on, and because each one names the
variable to change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from portfolio.config import MINIMUM_BACKUP_INTERVAL_MINUTES, Settings
from portfolio.services.backup import build_backup_service

BACKUP_VARIABLES: Final = (
    "PORTFOLIO_BACKUP_ENABLED",
    "PORTFOLIO_BACKUP_INTERVAL_MINUTES",
    "PORTFOLIO_BACKUP_DIR",
    "PORTFOLIO_BACKUP_KEEP_DAILY",
    "PORTFOLIO_BACKUP_KEEP_WEEKLY",
)


@pytest.fixture(autouse=True)
def without_an_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build `Settings` from its declared defaults, whatever the developer's shell holds."""
    for name in BACKUP_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_the_defaults_are_the_specs() -> None:
    """Written out, not read from the class, which would be the class agreeing with itself."""
    settings = Settings()

    assert settings.backup_enabled is True
    assert settings.backup_interval_minutes == 1440
    assert settings.backup_dir == Path("./data/backups")
    assert settings.backup_keep_daily == 7
    assert settings.backup_keep_weekly == 4


def test_each_setting_is_read_from_its_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PORTFOLIO_BACKUP_ENABLED", "false")
    monkeypatch.setenv("PORTFOLIO_BACKUP_INTERVAL_MINUTES", "90")
    monkeypatch.setenv("PORTFOLIO_BACKUP_DIR", str(tmp_path / "copies"))
    monkeypatch.setenv("PORTFOLIO_BACKUP_KEEP_DAILY", "3")
    monkeypatch.setenv("PORTFOLIO_BACKUP_KEEP_WEEKLY", "0")

    settings = Settings()

    assert settings.backup_enabled is False
    assert settings.backup_interval_minutes == 90
    assert settings.backup_dir == tmp_path / "copies"
    assert settings.backup_keep_daily == 3
    assert settings.backup_keep_weekly == 0


def test_the_interval_floor_is_the_hour_ruling_r14_names() -> None:
    """Written out: the constant is what the message and the validator both read."""
    assert MINIMUM_BACKUP_INTERVAL_MINUTES == 60


@pytest.mark.parametrize("interval", [59, 15, 1, 0, -1, -1440])
def test_an_interval_below_an_hour_is_refused_naming_the_count_and_the_switch(
    interval: int,
) -> None:
    """R14: rotation keeps every copy of the recent dates, so the copies kept multiply as the
    interval shrinks, on the database's own disk. 59 is the boundary, and 0 or less is still
    refused, as it was under the old floor of one minute."""
    with pytest.raises(ValidationError) as caught:
        Settings(backup_interval_minutes=interval)

    assert (
        f"PORTFOLIO_BACKUP_INTERVAL_MINUTES must be at least 60, got {interval}. Rotation keeps "
        "every copy of the most recent days, so a shorter interval multiplies the copies kept "
        "-- about PORTFOLIO_BACKUP_KEEP_DAILY x 1440 / interval of them -- and they share the "
        "database's disk, where a full disk stops the application's writes. To stop that "
        "timer, set PORTFOLIO_BACKUP_ENABLED=false."
    ) in str(caught.value)


@pytest.mark.parametrize("interval", [60, 61, 1440])
def test_an_interval_of_an_hour_or_more_is_accepted(interval: int) -> None:
    assert Settings(backup_interval_minutes=interval).backup_interval_minutes == interval


@pytest.mark.parametrize("keep", [0, -1])
def test_keeping_fewer_than_one_day_is_refused(keep: int) -> None:
    """It would delete the copy just taken."""
    with pytest.raises(ValidationError) as caught:
        Settings(backup_keep_daily=keep)

    assert (
        f"PORTFOLIO_BACKUP_KEEP_DAILY must be at least 1, got {keep}: rotation always keeps "
        "the newest copy."
    ) in str(caught.value)


def test_keeping_one_day_is_accepted() -> None:
    assert Settings(backup_keep_daily=1).backup_keep_daily == 1


@pytest.mark.parametrize("keep", [-1, -4])
def test_a_negative_number_of_weeks_is_refused(keep: int) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(backup_keep_weekly=keep)

    assert f"PORTFOLIO_BACKUP_KEEP_WEEKLY must be at least 0, got {keep}." in str(caught.value)


def test_keeping_no_weeks_is_accepted() -> None:
    assert Settings(backup_keep_weekly=0).backup_keep_weekly == 0


def test_a_disabled_timer_still_validates_its_interval() -> None:
    """Off is `PORTFOLIO_BACKUP_ENABLED=false`, not an interval of zero; both are checked."""
    with pytest.raises(ValidationError, match="PORTFOLIO_BACKUP_INTERVAL_MINUTES"):
        Settings(backup_enabled=False, backup_interval_minutes=0)


def test_the_service_is_built_from_these_settings(tmp_path: Path) -> None:
    """The directory is made absolute against the working directory when the service is built."""
    service = build_backup_service(Settings(backup_dir=Path("relative") / "copies"))

    assert service.directory.is_absolute()
    assert service.directory == Path.cwd() / "relative" / "copies"
    assert build_backup_service(Settings(backup_dir=tmp_path)).directory == tmp_path


def test_a_home_relative_directory_is_expanded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    service = build_backup_service(Settings(backup_dir=Path("~") / "copies"))

    assert service.directory == tmp_path / "copies"
