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
from sqlalchemy import text

from portfolio.domain.auth import SessionLifetime
from portfolio.repositories.sessions import SessionRepository
from portfolio.repositories.users import UserRepository
from portfolio.services.auth import (
    AuthService,
    InvalidCredentialsError,
    LoginThrottle,
    SessionInvalidError,
    UserExistsError,
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


#: Where the bootstrapped owner is moved before a replacement, for the reason
#: `tests/cli/test_create_user.py::PINNED_OWNER_ID` gives. SQLite gives a re-inserted row
#: `max(rowid) + 1`, which is 1 again once the table is empty. So an owner left at id 1 that
#: is deleted and re-created keeps its id, and "the same account" would pass for the old
#: implementation too. The reviewer confirmed that it did.
PINNED_OWNER_ID: Final = 42

#: A second account, which only hand-written SQL can create.
SECOND_USERNAME: Final = "second-keeper"


class InjectedFaultError(Exception):
    """Raised by a repository method this suite broke on purpose, after it did its write."""


class CountingHasher(PasswordHasher):
    """The real hasher at the suite's cheap parameters, counting the hashes it computes.

    A subclass rather than a stub, so every hash it returns is a real Argon2id digest that
    the application verifies. Only the count is added.
    """

    def __init__(self) -> None:
        super().__init__(time_cost=1, memory_cost=64, parallelism=1)
        self.hashes = 0

    def hash(self, password: str) -> str:
        self.hashes += 1
        return super().hash(password)


async def in_new_session[T](
    factory: async_sessionmaker[AsyncSession],
    throttle: LoginThrottle,
    step: Callable[[AuthService], Awaitable[T]],
    *,
    hasher: PasswordHasher = FAST_HASHER,
) -> T:
    """Run one step over its own database session, the way one request or one command does."""
    async with factory() as session:
        service = build_auth_service(
            session,
            hasher=hasher,
            lifetime=LIFETIME,
            throttle=throttle,
        )
        return await step(service)


async def execute(factory: async_sessionmaker[AsyncSession], sql: str, **values: object) -> None:
    """One hand-written statement, committed. Raw SQL, because the app has no path for these."""
    async with factory() as session:
        await session.execute(text(sql), values)
        await session.commit()


async def test_after_replace_the_old_session_is_refused_and_the_new_password_signs_in(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The stolen token dies with the old password, and the new password is what opens it."""
    throttle = LoginThrottle()
    # Before any session exists, so no row references the id being moved.
    await execute(sessionmaker, "UPDATE users SET id = :id", id=PINNED_OWNER_ID)

    old = await in_new_session(
        sessionmaker, throttle, lambda auth: auth.login(OWNER_USERNAME, OWNER_PHRASE)
    )
    before = await in_new_session(
        sessionmaker, throttle, lambda auth: auth.resolve_session(old.token)
    )
    assert (before.user_id, before.username) == (PINNED_OWNER_ID, OWNER_USERNAME)
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
    # The same account, not a new one that happens to carry the same name. Only meaningful
    # because the id was pinned away from 1, which a re-insert cannot reproduce.
    assert after.user_id == PINNED_OWNER_ID
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


async def arrange_accounts(factory: async_sessionmaker[AsyncSession], accounts: int) -> None:
    """Leave exactly this many accounts: the bootstrapped owner removed, kept, or joined.

    Deleting the owner is safe here only because nothing references it yet: no session,
    wallet or exchange account has been created in this test.
    """
    if accounts == 0:
        await execute(factory, "DELETE FROM users")
    elif accounts == 2:
        await execute(
            factory,
            "INSERT INTO users (username, password_hash, created_at) "
            "VALUES (:username, 'not-a-hash', '2026-01-01 00:00:00.000000')",
            username=SECOND_USERNAME,
        )
    async with factory() as session:
        found = (await session.execute(text("SELECT COUNT(*) FROM users"))).scalar_one()
    assert found == accounts


@pytest.mark.parametrize(
    ("accounts", "replace"),
    [(2, True), (1, False), (2, False)],
    ids=["replace-with-two-accounts", "create-with-an-account", "create-with-two-accounts"],
)
async def test_a_refused_create_user_pays_for_no_hash(
    sessionmaker: async_sessionmaker[AsyncSession],
    accounts: int,
    replace: bool,
) -> None:
    """A refusal never spends an Argon2id hash, as `create_user`'s docstring promises.

    At the production cost a hash is a deliberate quarter of a second of CPU and 19 MiB of
    memory on the Pi. There is no reason to spend it on an answer that was already "no".
    The success paths below are the positive companion, so a count of zero here cannot
    mean that the spy never counts.
    """
    await arrange_accounts(sessionmaker, accounts)
    hasher = CountingHasher()

    with pytest.raises(UserExistsError):
        await in_new_session(
            sessionmaker,
            LoginThrottle(),
            lambda auth: auth.create_user(OWNER_USERNAME, REPLACEMENT_PHRASE, replace=replace),
            hasher=hasher,
        )

    assert hasher.hashes == 0


@pytest.mark.parametrize(
    ("accounts", "replace"),
    [(1, True), (0, True), (0, False)],
    ids=["replace-one-account", "replace-on-an-empty-database", "create-on-an-empty-database"],
)
async def test_a_successful_create_user_hashes_exactly_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    accounts: int,
    replace: bool,
) -> None:
    """Exactly one hash per success. The stored digest proves that one hash is the one kept."""
    await arrange_accounts(sessionmaker, accounts)
    hasher = CountingHasher()

    await in_new_session(
        sessionmaker,
        LoginThrottle(),
        lambda auth: auth.create_user(OWNER_USERNAME, REPLACEMENT_PHRASE, replace=replace),
        hasher=hasher,
    )

    assert hasher.hashes == 1
    async with sessionmaker() as session:
        stored = (await session.execute(text("SELECT password_hash FROM users"))).scalar_one()
    assert FAST_HASHER.verify(stored, REPLACEMENT_PHRASE)


#: The owner's own name, deliberately not `PORTFOLIO_BOOTSTRAP_USERNAME`. The rename the
#: reviewer reproduced only shows up when the two differ: `alice` became `owner`.
OPERATORS_OWN_NAME: Final = "alice"


@pytest.mark.parametrize(
    ("rename", "expected"),
    [(False, OPERATORS_OWN_NAME), (True, OWNER_USERNAME)],
    ids=["without-rename-the-name-is-kept", "with-rename-the-name-changes"],
)
async def test_replace_renames_only_when_asked(
    sessionmaker: async_sessionmaker[AsyncSession],
    rename: bool,
    expected: str,
) -> None:
    """Spec 013, change A: a username the caller merely defaulted to is not a rename.

    The CLI always hands over a name, the bootstrap default when the operator typed none.
    So the call here passes the default in both cases, and only `rename` decides. Each case
    is the other's positive companion: one shows the name can change, the other that it
    does not change unless asked.
    """
    await execute(sessionmaker, "UPDATE users SET username = :name", name=OPERATORS_OWN_NAME)
    throttle = LoginThrottle()

    resulting = await in_new_session(
        sessionmaker,
        throttle,
        lambda auth: auth.create_user(
            OWNER_USERNAME, REPLACEMENT_PHRASE, replace=True, rename=rename
        ),
    )

    assert resulting == expected
    async with sessionmaker() as session:
        stored = list((await session.execute(text("SELECT username FROM users"))).scalars())
    assert stored == [expected]
    # The password changed either way, under whichever name the account now has.
    await in_new_session(
        sessionmaker, throttle, lambda auth: auth.login(expected, REPLACEMENT_PHRASE)
    )


async def test_replace_on_an_empty_database_creates_the_account_under_the_given_name(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """With nothing to keep, the name handed over is the name created, rename or not."""
    await arrange_accounts(sessionmaker, 0)

    resulting = await in_new_session(
        sessionmaker,
        LoginThrottle(),
        lambda auth: auth.create_user(OPERATORS_OWN_NAME, REPLACEMENT_PHRASE, replace=True),
    )

    assert resulting == OPERATORS_OWN_NAME
    async with sessionmaker() as session:
        stored = list((await session.execute(text("SELECT username FROM users"))).scalars())
    assert stored == [OPERATORS_OWN_NAME]
