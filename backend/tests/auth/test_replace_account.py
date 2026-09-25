"""Spec 013, criteria 1 and 2 at the service level: a replaced credential takes its sessions.

`create-user --replace` is how an owner who forgot the password gets back in, and also how
an owner who suspects a stolen cookie throws it away. Since #69 the account is updated in
place rather than deleted, so nothing cascades to `sessions` any more and the revoke has to
be explicit. If it were left out, the recovery would reset the password and leave the thief
signed in.

Everything here runs through the application's own flow: the token is one `login` issued,
the refusal is the one `resolve_session` gives the middleware on every request, and each
step opens its own database session, the way separate requests and a separate CLI process
do. The two halves are each other's positive companion. The old token works before the
replacement and fails after it. The new password fails before the replacement and works
after it. So neither result can come from a service that refuses everything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest

from portfolio.domain.auth import SessionLifetime
from portfolio.repositories.sessions import SessionRepository
from portfolio.repositories.users import UserRepository
from portfolio.services.auth import (
    AuthService,
    InvalidCredentialsError,
    LoginThrottle,
    SessionInvalidError,
    build_auth_service,
)
from portfolio.services.password_hasher import PasswordHasher
from tests.auth.conftest import (
    OWNER_PHRASE,
    OWNER_USERNAME,
    REPLACEMENT_PHRASE,
    SESSION_PATH,
    sign_in,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# The parameters `apply_auth_environment` gives the running application, so a hash this
# service writes is one the application's own hasher verifies without asking to rehash.
FAST_HASHER: Final = PasswordHasher(time_cost=1, memory_cost=64, parallelism=1)
LIFETIME: Final = SessionLifetime.from_days(idle_days=7, absolute_days=30)


class InjectedFaultError(Exception):
    """Raised by a repository method this suite broke on purpose, after it did its write."""


async def in_new_session[T](
    factory: async_sessionmaker[AsyncSession],
    throttle: LoginThrottle,
    step: Callable[[AuthService], Awaitable[T]],
) -> T:
    """Run one step over its own database session, the way one request or one command does."""
    async with factory() as session:
        service = build_auth_service(
            session,
            hasher=FAST_HASHER,
            lifetime=LIFETIME,
            throttle=throttle,
        )
        return await step(service)


async def test_after_replace_the_old_session_is_refused_and_the_new_password_signs_in(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The stolen token dies with the old password, and the new password is what opens it."""
    throttle = LoginThrottle()

    old = await in_new_session(
        sessionmaker, throttle, lambda auth: auth.login(OWNER_USERNAME, OWNER_PHRASE)
    )
    before = await in_new_session(
        sessionmaker, throttle, lambda auth: auth.resolve_session(old.token)
    )
    assert before.username == OWNER_USERNAME
    with pytest.raises(InvalidCredentialsError):
        await in_new_session(
            sessionmaker, throttle, lambda auth: auth.login(OWNER_USERNAME, REPLACEMENT_PHRASE)
        )

    await in_new_session(
        sessionmaker,
        throttle,
        lambda auth: auth.create_user(OWNER_USERNAME, REPLACEMENT_PHRASE, replace=True),
    )

    with pytest.raises(SessionInvalidError):
        await in_new_session(sessionmaker, throttle, lambda auth: auth.resolve_session(old.token))
    new = await in_new_session(
        sessionmaker, throttle, lambda auth: auth.login(OWNER_USERNAME, REPLACEMENT_PHRASE)
    )
    after = await in_new_session(
        sessionmaker, throttle, lambda auth: auth.resolve_session(new.token)
    )
    # The same account, not a new one that happens to carry the same name.
    assert after.user_id == before.user_id
    with pytest.raises(InvalidCredentialsError):
        await in_new_session(
            sessionmaker, throttle, lambda auth: auth.login(OWNER_USERNAME, OWNER_PHRASE)
        )


@pytest.mark.parametrize(
    ("repository", "method"),
    [(UserRepository, "set_credentials"), (SessionRepository, "delete_for_user")],
    ids=["the-update-fails", "the-revoke-fails"],
)
async def test_replace_is_one_transaction_so_a_failure_changes_nothing(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    repository: type[object],
    method: str,
) -> None:
    """Spec 013, criterion 1: the new credential and the revoke commit together or not at all.

    The broken method does its write first and fails afterwards, so by the time it fails
    both writes have usually been flushed, and the only thing that can undo them is the
    rollback of one transaction. Committing between the two steps would leave an account
    whose password changed while every old session survived. That is the outcome this
    change exists to prevent, reached through an error path instead of a missing line.

    Both steps are broken in turn, so the test does not depend on the order the service
    runs them in. Whichever one runs second is the case that proves the atomicity.
    """
    throttle = LoginThrottle()
    old = await in_new_session(
        sessionmaker, throttle, lambda auth: auth.login(OWNER_USERNAME, OWNER_PHRASE)
    )
    original = getattr(repository, method)

    async def write_then_fail(self: object, *args: object, **kwargs: object) -> None:
        await original(self, *args, **kwargs)
        raise InjectedFaultError(method)

    with monkeypatch.context() as patch:
        patch.setattr(repository, method, write_then_fail)
        # Raised at all is the proof the broken method was on the path.
        with pytest.raises(InjectedFaultError):
            await in_new_session(
                sessionmaker,
                throttle,
                lambda auth: auth.create_user(OWNER_USERNAME, REPLACEMENT_PHRASE, replace=True),
            )

    still = await in_new_session(
        sessionmaker, throttle, lambda auth: auth.resolve_session(old.token)
    )
    assert still.username == OWNER_USERNAME
    with pytest.raises(InvalidCredentialsError):
        await in_new_session(
            sessionmaker, throttle, lambda auth: auth.login(OWNER_USERNAME, REPLACEMENT_PHRASE)
        )
    await in_new_session(
        sessionmaker, throttle, lambda auth: auth.login(OWNER_USERNAME, OWNER_PHRASE)
    )


async def test_after_replace_the_old_cookie_is_refused_by_the_api(
    auth_client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The same claim at the request path: the browser that signed in earlier is signed out.

    `resolve_session` is what the middleware calls, but this asserts the status a stolen
    cookie actually gets. That makes it the check that notices if a later change caches a
    principal somewhere between the cookie and the database.
    """
    await sign_in(auth_client)
    assert (await auth_client.get(SESSION_PATH)).status_code == 200

    await in_new_session(
        sessionmaker,
        LoginThrottle(),
        lambda auth: auth.create_user(OWNER_USERNAME, REPLACEMENT_PHRASE, replace=True),
    )

    assert (await auth_client.get(SESSION_PATH)).status_code == 401
