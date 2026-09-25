"""The settings #10 adds, and the one configuration that must never become a running server.

`tests/providers/test_provider_urls.py` covers the four provider URLs `_refuse_unsafe_configuration`
already guarded and `tests/auth/test_startup.py` covers the password and cookie rules. What
is here is the interval, which is the first setting whose wrong value is not merely unsafe
but actively hostile to somebody else: a loop with no sleep in it against a public index
that documents a ban as the consequence.

Refusing at construction is what turns that into a container which fails its health check
and a deployment the pipeline already knows how to roll back. The alternative -- clamping a
zero up to one and carrying on -- is worse than it sounds: an operator who typed `0` meaning
"off" would get a sync every minute forever and nothing anywhere would mention it.
"""

from __future__ import annotations

from typing import Final

import pytest
from pydantic import ValidationError

from portfolio.config import Settings

#: The issue's own default, and the one `docs/operations.md` documents.
DEFAULT_BALANCE_INTERVAL_MINUTES: Final = 15

#: One hour, matching `services/prices.py::STALE_AFTER`. The two are a pair: a price that
#: has missed exactly one refresh is the first one worth flagging stale, and a refresh
#: interval longer than the staleness window would mark every price stale between ticks.
DEFAULT_PRICE_INTERVAL_MINUTES: Final = 60

#: Long enough for an ordinary sync to finish and short enough that a stuck one does not
#: hold a deployment open. Being wrong in either direction costs a row marked `interrupted`
#: rather than data, because a run writes its snapshots per chain as it goes.
DEFAULT_SHUTDOWN_GRACE_SECONDS: Final = 10


@pytest.fixture(autouse=True)
def without_an_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build `Settings` from its declared defaults, whatever the developer's shell holds.

    `Settings` reads `PORTFOLIO_`-prefixed variables *and* a `.env` file, so a test asserting
    on a default is otherwise asserting on whatever the machine running it happens to have
    exported -- which passes on a laptop with nothing set and fails on the one where somebody
    was debugging a vendor last week.
    """
    for name in (
        "PORTFOLIO_BALANCE_SYNC_ENABLED",
        "PORTFOLIO_BALANCE_SYNC_INTERVAL_MINUTES",
        "PORTFOLIO_BALANCE_SYNC_SHUTDOWN_GRACE_SECONDS",
        "PORTFOLIO_PRICE_REFRESH_ENABLED",
        "PORTFOLIO_PRICE_REFRESH_INTERVAL_MINUTES",
    ):
        monkeypatch.delenv(name, raising=False)


def test_the_shipped_defaults_are_the_ones_the_issue_and_the_spec_name() -> None:
    """The defaults, pinned, because they are what a deployment that configures nothing gets.

    Written out rather than read from the class, which would be the class agreeing with
    itself. `docs/operations.md` quotes these numbers, and a default changed without that
    document changing is a runbook that describes a different product.
    """
    settings = Settings()

    assert settings.balance_sync_enabled is True
    assert settings.balance_sync_interval_minutes == DEFAULT_BALANCE_INTERVAL_MINUTES
    assert settings.balance_sync_shutdown_grace_seconds == DEFAULT_SHUTDOWN_GRACE_SECONDS


@pytest.mark.parametrize("interval", [0, -1, -15])
def test_a_zero_interval_is_refused_at_construction(interval: int) -> None:
    """A loop with no sleep against a public index, refused before the server exists.

    Zero is the value an operator reaches for when they mean "off", which is why the message
    has to name the switch that actually does that. Negative values are here because
    `int(...)` accepts them and a `timedelta` built from one produces a sleep that returns
    immediately -- the same loop, arrived at by a different typo.
    """
    with pytest.raises(ValidationError) as caught:
        Settings(balance_sync_interval_minutes=interval)

    message = str(caught.value)
    assert "PORTFOLIO_BALANCE_SYNC_INTERVAL_MINUTES" in message
    assert "PORTFOLIO_BALANCE_SYNC_ENABLED" in message, (
        "the refusal has to name the switch that actually turns the schedule off"
    )


def test_an_interval_of_one_minute_is_accepted() -> None:
    """The boundary from the other side, so the guard is a floor rather than a ban.

    One minute is aggressive and it is a decision an operator is allowed to make -- a
    self-hoster pointing both providers at their own index on the same machine has no vendor
    to annoy. The rule is about zero, not about speed.
    """
    assert Settings(balance_sync_interval_minutes=1).balance_sync_interval_minutes == 1


@pytest.mark.parametrize("grace", [0, 1, 60])
def test_a_shutdown_grace_of_zero_is_a_configuration_rather_than_a_mistake(grace: int) -> None:
    """Zero means "do not wait", which is a real answer on a machine being shut down hard.

    Distinct from the interval, deliberately: a zero interval is a busy loop and a zero grace
    is simply a cancelled run, which the sweep records as `interrupted` and the next startup
    handles. Refusing it would be a rule with no failure behind it.
    """
    assert (
        Settings(balance_sync_shutdown_grace_seconds=grace).balance_sync_shutdown_grace_seconds
        == grace
    )


# --------------------------------------------------------------------------------------
# The price refresh, added to #10's scope after the spec was first committed
# --------------------------------------------------------------------------------------


def test_the_price_refresh_interval_defaults_to_the_staleness_window() -> None:
    """Sixty minutes, matching `STALE_AFTER`, and the match is the point.

    `services/prices.py` documents its one-hour threshold as "matching the refresh interval
    #10 will schedule". This is that interval. A refresh slower than the window marks every
    price stale between ticks, which trains whoever reads the dashboard to ignore the flag;
    a refresh much faster spends requests on a number that has not moved.
    """
    settings = Settings()

    assert settings.price_refresh_interval_minutes == DEFAULT_PRICE_INTERVAL_MINUTES
    assert settings.price_refresh_enabled is True


@pytest.mark.parametrize("interval", [0, -1])
def test_a_zero_price_refresh_interval_is_refused_too(interval: int) -> None:
    """The same guard, because it is the same busy loop against a different set of vendors.

    Four price sources rather than two indexes, one of them keyed, and Kraken is the
    key-free primary every deployment uses. A loop with no sleep here is the same ban with
    somebody else's name on it.
    """
    with pytest.raises(ValidationError) as caught:
        Settings(price_refresh_interval_minutes=interval)

    assert "PORTFOLIO_PRICE_REFRESH_INTERVAL_MINUTES" in str(caught.value)


def test_the_two_schedules_are_configured_apart() -> None:
    """One switch each, so an operator debugging a vendor can stop the right half.

    Folding both under one flag would mean that turning off a chain index nobody can reach
    also stops the price cache filling -- and the dashboard would then report every holding
    unpriced for a reason that has nothing to do with prices.
    """
    balances_only = Settings(balance_sync_enabled=True, price_refresh_enabled=False)
    prices_only = Settings(balance_sync_enabled=False, price_refresh_enabled=True)

    assert (balances_only.balance_sync_enabled, balances_only.price_refresh_enabled) == (
        True,
        False,
    )
    assert (prices_only.balance_sync_enabled, prices_only.price_refresh_enabled) == (False, True)


def test_the_interval_refusals_do_not_quote_the_bootstrap_password() -> None:
    """`ValidationError.errors()` carries every `PORTFOLIO_*` variable as its raw input.

    Measured and recorded in `_refuse_unsafe_configuration`'s docstring: `str(exc)` elides
    the input, and `.errors()` and `.json()` do not. That is a live hazard rather than a
    historical one -- #10 adds two new ways to fail construction, so two new places a
    helpful `logger.exception` could publish the owner's bootstrap password.

    This pins the *safe* half, which is the one anything logs today. The unsafe half is
    pinned deliberately in `tests/providers/test_provider_urls.py`, so that a change which
    starts redacting it announces itself rather than looking like a regression.
    """
    phrase = "a correct horse battery staple"

    with pytest.raises(ValidationError) as caught:
        Settings(balance_sync_interval_minutes=0, bootstrap_password=phrase)

    assert phrase not in str(caught.value)
