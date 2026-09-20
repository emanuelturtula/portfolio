"""Login, logout, session resolution, password change, and the login throttle.

This module is the only place that turns a password or a cookie into an identity, and it
holds the transaction while it does. It knows nothing about HTTP: it raises its own
exceptions and the API layer decides which status code each one deserves. That is what
lets `create-user` and the request path share it without the CLI importing FastAPI.

Three decisions worth keeping in view while reading it:

* **Failure is uniform.** An unknown username and a wrong password raise the same
  exception and perform the same Argon2id verification, the second against a hash of a
  random password. Anything cheaper leaks which usernames exist, through the clock.
* **The throttle is in process.** A dict, pruned to a fifteen minute window. For a
  single-user application in a container that runs one worker this is exact, and the
  alternative -- a table -- would add the only migration in this change plus a cleanup job.
  The cost is that a restart clears the window; an attacker cannot cause a restart.
* **The token is the only thing the client ever sees.** Its SHA-256 is what is stored, so
  the database cannot hand a reader a working cookie.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from portfolio.domain.auth import SESSION_TOKEN_BYTES, hash_token, should_refresh_last_seen
from portfolio.domain.passwords import ensure_meets_policy
from portfolio.repositories.sessions import SessionRepository
from portfolio.repositories.users import UserRepository

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.domain.auth import SessionLifetime
    from portfolio.services.password_hasher import PasswordHasher

# What the client is told when a sign-in fails, and when a request carries no usable
# session. One string each, shared by every path, because two strings become two messages
# and two messages are how a client learns which usernames exist.
INVALID_CREDENTIALS_DETAIL: Final = "The username or password is incorrect."
SESSION_REQUIRED_DETAIL: Final = "Authentication is required."

# Five failures inside the window are tolerated, so the sixth attempt is refused. Keyed on
# the submitted username rather than on the client address: there is one real username, and
# an address key lets anyone on the same network rotate their way around the limit.
LOGIN_FAILURE_LIMIT: Final = 5
LOGIN_FAILURE_WINDOW: Final = timedelta(minutes=15)


def utc_now() -> datetime:
    """The clock, in one place, so a test can replace it with a value it chose."""
    return datetime.now(UTC)


class AuthError(Exception):
    """Base class for every failure this service raises."""


class InvalidCredentialsError(AuthError):
    """The username does not exist, or the password does not match. Deliberately one error."""


class TooManyAttemptsError(AuthError):
    """Too many recent failures for this username; the attempt was refused unverified."""


class SessionInvalidError(AuthError):
    """No session, an unknown token, or one that has expired by either rule."""


class UserExistsError(AuthError):
    """An account already exists and the caller did not ask to replace it."""


@dataclass(frozen=True, slots=True)
class Principal:
    """Who the current request belongs to, and which session says so."""

    user_id: int
    username: str
    session_id: int


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A freshly minted session: the plaintext token exists only here and in the cookie."""

    token: str
    expires_at: datetime


@dataclass
class LoginThrottle:
    """Recent failed logins per username, pruned to a sliding window.

    Deliberately mutable process state. One instance is built per application and shared
    by every request, which is the only way an in-process counter can mean anything.

    It grows with the number of distinct usernames tried, and nothing caps that. The bound
    that makes it acceptable is not in this class: every failed attempt pays for a full
    Argon2id verification first, so filling this dictionary costs the attacker roughly a
    quarter of a second per entry on the hardware this runs on, and the process is
    restarted by every deployment. A cap would be the wrong fix anyway -- an attacker who
    could overflow it could then use the overflow to evict the real username's counter.
    """

    limit: int = LOGIN_FAILURE_LIMIT
    window: timedelta = LOGIN_FAILURE_WINDOW
    _failures: dict[str, list[datetime]] = field(default_factory=dict, repr=False)

    @staticmethod
    def _key(username: str) -> str:
        """Case-folded, so changing the capitalisation is not a way around the counter."""
        return username.casefold()

    def _recent(self, username: str, now: datetime) -> list[datetime]:
        """The failures still inside the window, dropping the ones that have aged out."""
        key = self._key(username)
        recent = [at for at in self._failures.get(key, []) if now - at < self.window]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def is_throttled(self, username: str, now: datetime) -> bool:
        """Whether the next attempt for this username must be refused without verifying."""
        return len(self._recent(username, now)) >= self.limit

    def record_failure(self, username: str, now: datetime) -> None:
        """Count one failed attempt."""
        recent = self._recent(username, now)
        self._failures[self._key(username)] = [*recent, now]

    def clear(self, username: str) -> None:
        """Forget every failure for a username. A successful login is proof of ownership."""
        self._failures.pop(self._key(username), None)


class AuthService:
    """The unit of work for everything authentication does.

    It owns the transaction: the repositories flush, and this class is what commits. The
    caller -- a request dependency or the CLI -- owns the session and closes it, so an
    exception leaves uncommitted work rolled back rather than half applied.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        users: UserRepository,
        sessions: SessionRepository,
        hasher: PasswordHasher,
        lifetime: SessionLifetime,
        throttle: LoginThrottle,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._users = users
        self._sessions = sessions
        self._hasher = hasher
        self._lifetime = lifetime
        self._throttle = throttle
        self._clock = clock

    async def login(self, username: str, password: str) -> IssuedSession:
        """Verify a password and issue a session, or raise without saying which half failed."""
        now = self._clock()
        if self._throttle.is_throttled(username, now):
            message = "Too many failed sign-in attempts. Try again later."
            raise TooManyAttemptsError(message)

        user = await self._users.get_by_username(username)
        # The absent user is verified against a hash of a random password so that both
        # paths perform one Argon2id verification at the configured cost.
        stored = user.password_hash if user is not None else self._hasher.dummy_hash
        verified = self._hasher.verify(stored, password)
        if user is None or not verified:
            self._throttle.record_failure(username, now)
            raise InvalidCredentialsError(INVALID_CREDENTIALS_DETAIL)

        self._throttle.clear(username)
        if self._hasher.needs_rehash(user.password_hash):
            # The one moment the plaintext is in hand after the cost parameters were
            # raised. Without this, tuning on the Pi would only apply to a new account.
            await self._users.set_password_hash(user, self._hasher.hash(password))

        token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        row = await self._sessions.add(
            user_id=user.id,
            token_hash=hash_token(token),
            created_at=now,
            expires_at=self._lifetime.absolute_expiry(now),
        )
        expires_at = row.expires_at
        await self._session.commit()
        return IssuedSession(token=token, expires_at=expires_at)

    async def resolve_session(self, token: str) -> Principal:
        """Turn a cookie value into a principal, sliding the idle window as it goes.

        The token is a non-empty string: a request with no cookie is refused by the
        middleware before this is reached, without opening a database session at all. A
        second emptiness check here would be a branch nothing could reach and no test
        could cover.

        An expired row is deleted on the way out rather than left to accumulate: that is
        why this change ships no sweeper. A single user produces a handful of rows a year.
        """
        found = await self._sessions.get_with_username(hash_token(token))
        if found is None:
            raise SessionInvalidError(SESSION_REQUIRED_DETAIL)

        row, username = found
        now = self._clock()
        if not self._lifetime.is_valid(
            now=now,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
        ):
            await self._sessions.delete_by_id(row.id)
            await self._session.commit()
            raise SessionInvalidError(SESSION_REQUIRED_DETAIL)

        if should_refresh_last_seen(now=now, last_seen_at=row.last_seen_at):
            await self._sessions.touch(row, now)
            await self._session.commit()
        return Principal(user_id=row.user_id, username=username, session_id=row.id)

    async def logout(self, principal: Principal) -> None:
        """Revoke the caller's own session. The cookie is worthless from here on."""
        await self._sessions.delete_by_id(principal.session_id)
        await self._session.commit()

    async def change_password(
        self,
        principal: Principal,
        current_password: str,
        new_password: str,
    ) -> None:
        """Replace the password and revoke every session, the caller's own included.

        Revoking everything is the product's whole session-management story: there is no
        session list and no "sign out other devices", because changing the password is the
        one revocation a single-user application needs.
        """
        ensure_meets_policy(new_password)
        user = await self._users.get_by_id(principal.user_id)
        stored = user.password_hash if user is not None else self._hasher.dummy_hash
        verified = self._hasher.verify(stored, current_password)
        if user is None or not verified:
            raise InvalidCredentialsError(INVALID_CREDENTIALS_DETAIL)

        await self._users.set_password_hash(user, self._hasher.hash(new_password))
        await self._sessions.delete_for_user(user.id)
        await self._session.commit()

    async def create_user(self, username: str, password: str, *, replace: bool = False) -> None:
        """Create the owner account, optionally replacing the one that is already there.

        `replace` is the recovery path for a forgotten password: this product has no reset
        flow by design, and the runtime image carries no `sqlite3` binary, so without it a
        forgotten password would mean a lost instance. The delete and the insert are one
        transaction, and the delete cascades to the old account's sessions.
        """
        ensure_meets_policy(password)
        exists = await self._users.count() > 0
        if exists and not replace:
            message = "An account already exists. Use --replace to replace it."
            raise UserExistsError(message)
        if exists:
            await self._users.delete_all()
        await self._users.add(
            username=username,
            password_hash=self._hasher.hash(password),
            created_at=self._clock(),
        )
        await self._session.commit()

    async def bootstrap_user(self, username: str, password: str) -> bool:
        """Create the account from the bootstrap password, and report whether it did.

        Returns `False` when an account already exists, which is what stops the variable
        being left in an environment file from resetting the password on every deploy.
        """
        if await self._users.count() > 0:
            return False
        await self.create_user(username, password)
        return True


def build_auth_service(
    session: AsyncSession,
    *,
    hasher: PasswordHasher,
    lifetime: SessionLifetime,
    throttle: LoginThrottle,
    clock: Callable[[], datetime] = utc_now,
) -> AuthService:
    """Assemble the service over one database session.

    The repositories are built here rather than injected because there is exactly one
    implementation of each; the hasher and the throttle are injected because they are
    process-wide and outlive the session.
    """
    return AuthService(
        session=session,
        users=UserRepository(session),
        sessions=SessionRepository(session),
        hasher=hasher,
        lifetime=lifetime,
        throttle=throttle,
        clock=clock,
    )
