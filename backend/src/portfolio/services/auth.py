"""Login, logout, session resolution, password change, and the login throttle.

This module is the only place that turns a password or a cookie into an identity, and it
holds the transaction while it does. It knows nothing about HTTP: it raises its own
exceptions and the API layer decides which status code each one deserves. That is what
lets `create-user` and the request path share it without the CLI importing FastAPI.

Three decisions worth keeping in view while reading it:

* **Failure is uniform.** An unknown username and a wrong password raise the same
  exception and perform the same Argon2id verification, the second against a hash of a
  random password. Anything cheaper leaks which usernames exist, through the clock.
* **The throttle is in process.** A dict and a list, both pruned to a fifteen minute
  window, counting failures per username and in total. For a single-user application in a
  container that runs one worker this is exact, and the alternative -- a table -- would add
  the only migration in this change plus a cleanup job. The cost is that a restart clears
  the window; an attacker cannot cause a restart. Both login and the password change are
  counted, because the password change is the request worth brute forcing.
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
TOO_MANY_ATTEMPTS_DETAIL: Final = "Too many failed attempts. Try again later."

# Five failures inside the window are tolerated, so the sixth attempt is refused. Keyed on
# the submitted username rather than on the client address: there is one real username, and
# an address key lets anyone on the same network rotate their way around the limit.
LOGIN_FAILURE_LIMIT: Final = 5
LOGIN_FAILURE_WINDOW: Final = timedelta(minutes=15)

# The same window, counted across every username at once. Ten times the per-username limit
# so that an owner fumbling one password can never reach it, and low enough that varying
# the username -- which defeats the per-username count entirely -- buys fifty verifications
# rather than an unbounded number.
TOTAL_FAILURE_LIMIT: Final = 50


def utc_now() -> datetime:
    """The clock, in one place, so a test can replace it with a value it chose."""
    return datetime.now(UTC)


class AuthError(Exception):
    """Base class for every failure this service raises."""


class InvalidCredentialsError(AuthError):
    """The username does not exist, or the password does not match. Deliberately one error."""


class TooManyAttemptsError(AuthError):
    """Too many recent failures, for this username or in total. Refused unverified."""


class SessionInvalidError(AuthError):
    """No session, an unknown token, or one that has expired by either rule."""


class UserExistsError(AuthError):
    """An account exists and the caller did not ask to replace it, or several exist.

    Several is only reachable through hand-written SQL, and `replace` refuses it rather
    than choosing which account the operator meant.
    """


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
    """Recent failed credential checks, counted per username and in total.

    Deliberately mutable process state. One instance is built per application and shared
    by every request, which is the only way an in-process counter can mean anything.

    **Two counters, because one of them is trivially avoidable.** The per-username count
    is what stops a password being guessed. It is keyed on the submitted username, so an
    attacker who varies the username never trips it -- measured on the running
    application, twenty logins with twenty distinct usernames left every key at one
    failure, and every one of them paid for a full Argon2id verification first. The total
    count is what stops that: fifty failures in the window and every attempt is refused,
    whatever username it names.

    The total also bounds the memory. `_failures` grows with the number of distinct
    usernames tried and nothing caps its size, because a cap is the wrong instrument --
    an attacker who could overflow a cap could then use the overflow to evict the real
    username's counter, which is the one entry that matters. The total limit is not
    evictable: once it is reached nothing further is recorded, because an attempt refused
    before verification is never counted, so both the list and the dictionary stop growing
    at that point.

    The cost is that fifty failures lock the owner out for fifteen minutes. That exposure
    is not new -- anyone who knows the one real username could already do it in five
    attempts -- and it is the trade the per-username counter already made.
    """

    limit: int = LOGIN_FAILURE_LIMIT
    total_limit: int = TOTAL_FAILURE_LIMIT
    window: timedelta = LOGIN_FAILURE_WINDOW
    _failures: dict[str, list[datetime]] = field(default_factory=dict, repr=False)
    _total: list[datetime] = field(default_factory=list, repr=False)

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

    def _recent_total(self, now: datetime) -> list[datetime]:
        """Every failure still inside the window, whatever username it named."""
        self._total = [at for at in self._total if now - at < self.window]
        return self._total

    def is_throttled(self, username: str, now: datetime) -> bool:
        """Whether the next attempt must be refused without verifying a password.

        Both counters are consulted, and both are pruned on the way past, so a window that
        has emptied itself costs nothing to keep.
        """
        over_total = len(self._recent_total(now)) >= self.total_limit
        over_username = len(self._recent(username, now)) >= self.limit
        return over_total or over_username

    def record_failure(self, username: str, now: datetime) -> None:
        """Count one failed attempt, against this username and against the total."""
        recent = self._recent(username, now)
        self._failures[self._key(username)] = [*recent, now]
        self._total = [*self._recent_total(now), now]

    def tracked_usernames(self) -> int:
        """How many usernames currently hold a failure inside the window.

        The memory this class holds, as a number, so that the bound the total limit
        provides can be asserted without a test reaching into a private attribute -- a
        coupling that would outlive the test that introduced it.
        """
        return len(self._failures)

    def clear(self, username: str) -> None:
        """Forget every failure for a username. A successful login is proof of ownership.

        The total is deliberately left alone: proving you own *this* account says nothing
        about the forty-nine failures that named other usernames, and letting one success
        reset the total would hand an attacker the reset button along with it.
        """
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
            raise TooManyAttemptsError(TOO_MANY_ATTEMPTS_DETAIL)

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

        Throttled through the same counter as login, keyed on the same username. The
        current-password check is the only thing between a borrowed browser -- or script
        running with the cookie -- and a permanent takeover, and unlike a failed login a
        success here is terminal: this product has no reset flow, so an attacker who
        guesses it owns the instance. Leaving this path unlimited would have meant the one
        endpoint worth brute forcing was the one nothing counted.

        The order is deliberate. The throttle refuses before any work is done; the policy
        check is arithmetic on the *new* password and is not an attempt at the old one, so
        it does not count as a failure; only a wrong current password does.
        """
        now = self._clock()
        if self._throttle.is_throttled(principal.username, now):
            raise TooManyAttemptsError(TOO_MANY_ATTEMPTS_DETAIL)

        ensure_meets_policy(new_password)
        user = await self._users.get_by_id(principal.user_id)
        stored = user.password_hash if user is not None else self._hasher.dummy_hash
        verified = self._hasher.verify(stored, current_password)
        if user is None or not verified:
            self._throttle.record_failure(principal.username, now)
            raise InvalidCredentialsError(INVALID_CREDENTIALS_DETAIL)

        self._throttle.clear(principal.username)
        await self._users.set_password_hash(user, self._hasher.hash(new_password))
        await self._sessions.delete_for_user(user.id)
        await self._session.commit()

    async def create_user(
        self,
        username: str,
        password: str,
        *,
        replace: bool = False,
        rename: bool = False,
    ) -> str:
        """Create the owner account, or with `replace` give the existing one a new credential.

        Returns the account's username as it stands afterwards, which is the only way a
        caller that did not rename can say which account it just changed.

        `replace` is the recovery path for a forgotten password: this product has no reset
        flow by design, and the runtime image carries no `sqlite3` binary, so without it a
        forgotten password would mean a lost instance.

        **It replaces the credential, never the account.** The row is updated in place --
        new hash, same `id` and `created_at` -- because every wallet, balance snapshot,
        exchange account and fill hangs off that `id`. Deleting the user instead, as this
        once did, cascaded through the wallets and their history, and would fail outright
        against the fills' `RESTRICT`. An update is correct for every table that references
        `users.id`, including the ones not written yet.

        **`username` names a new account; it renames an existing one only with `rename`.**
        The CLI always has a name to hand over, because creating an account needs one, and
        when the operator gave none it is `PORTFOLIO_BOOTSTRAP_USERNAME`. A name that came
        from a default is not a request to rename, and treating it as one renamed an
        account called `alice` to `owner` in the middle of a password recovery. So the
        decision is a separate argument, made by the one caller that knows whether the name
        was typed, and it defaults to keeping the name: a caller that forgets it gets the
        identity-preserving behaviour rather than the surprising one.

        **Every session is revoked explicitly, in the same transaction.** An update fires no
        cascade, so without the `delete_for_user` a stolen cookie would outlive the very
        recovery meant to defeat it. It is what `change_password` does, for the same reason.

        With no account, both modes create one: pointing the command at a fresh volume is
        ordinary. With more than one -- which only hand-written SQL can produce, since this
        method and `bootstrap_user` both refuse a second -- `replace` refuses and changes
        nothing. Choosing one would be a guess about which owner is meant; the message says
        how many there are and names none of them.

        The password is hashed only once both refusals are behind it, so a refusal never
        pays for an Argon2id hash.
        """
        ensure_meets_policy(password)
        accounts = await self._users.list_all()
        if accounts and not replace:
            message = "An account already exists. Use --replace to replace it."
            raise UserExistsError(message)
        if len(accounts) > 1:
            message = (
                f"--replace found {len(accounts)} accounts and will not guess which one "
                "to change. Nothing was changed."
            )
            raise UserExistsError(message)

        password_hash = self._hasher.hash(password)
        if accounts:
            (owner,) = accounts
            await self._users.set_credentials(
                owner,
                username=username if rename else owner.username,
                password_hash=password_hash,
            )
            await self._sessions.delete_for_user(owner.id)
        else:
            owner = await self._users.add(
                username=username,
                password_hash=password_hash,
                created_at=self._clock(),
            )
        # Read before the commit, so the answer does not depend on how the caller built its
        # session: under `expire_on_commit=True` the attribute would reload lazily, and a
        # lazy load on an `AsyncSession` raises rather than querying.
        resulting_username = owner.username
        await self._session.commit()
        return resulting_username

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
