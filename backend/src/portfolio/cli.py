"""Operator commands: `create-user` and `hash-benchmark`.

A sibling of `portfolio.api`, not a layer above it: both are entry points onto the same
services, and neither imports the other. Everything here is a thin shell around
`AuthService`, for the same reason the routers are -- the password policy has to be the
same one the API applies, and a second copy of it is a second thing to get wrong.

**The password is never an argument.** Not a flag, not an environment variable, not a
positional. A command-line argument lands in the shell history, in `ps` output and in any
process listing the machine keeps; an environment variable lands in `/proc/<pid>/environ`
and in every child process. It is read from the terminal with `getpass`, confirmed, and
held in a local for as long as it takes to hash it.

`print` is not used anywhere here: this process writes structured JSON logs to stdout in
production, and a stray `print` injects an unparseable line into that stream. Ruff's `T20`
enforces it; `sys.stdout` is resolved at call time so that a test can capture it.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from decimal import Decimal
from time import perf_counter_ns
from typing import TYPE_CHECKING

from portfolio.config import get_settings
from portfolio.db.alembic_config import upgrade_to_head
from portfolio.db.engine import (
    create_database_engine,
    create_session_factory,
    ensure_database_directory,
)
from portfolio.domain.auth import SessionLifetime
from portfolio.domain.passwords import (
    OWASP_MINIMUM_MEMORY_COST,
    OWASP_MINIMUM_TIME_COST,
    PasswordPolicyError,
)
from portfolio.services.auth import AuthError, LoginThrottle, build_auth_service
from portfolio.services.password_hasher import PasswordHasher

if TYPE_CHECKING:
    from portfolio.config import Settings

# What the issue asks the Raspberry Pi to be tuned to. Reported as guidance rather than
# enforced: the right number depends on how many people share the machine, and the only
# way to know it is to measure it where the application actually runs.
TARGET_MILLISECONDS = Decimal(250)
NANOSECONDS_PER_MILLISECOND = Decimal(1_000_000)
DEFAULT_BENCHMARK_ROUNDS = 5

CONFIRMATION_WORDS = frozenset({"y", "yes"})


class CommandError(Exception):
    """A failure with a message the operator should read, and no traceback worth showing."""


def emit(message: str) -> None:
    """Write one line to stdout, resolving the stream at call time."""
    sys.stdout.write(f"{message}\n")


def emit_error(message: str) -> None:
    """Write one line to stderr, resolving the stream at call time."""
    sys.stderr.write(f"{message}\n")


def prompt_for_password() -> str:
    """Read a password twice from the terminal and require the two to agree.

    The confirmation is not ceremony: this is the only place the value is ever typed, and
    a typo in it locks the owner out of an application with no password reset flow.
    """
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        message = "The two entries do not match. Nothing was changed."
        raise CommandError(message)
    return first


def confirm_replacement(username: str) -> None:
    """Make the operator say out loud that an existing account is about to be destroyed.

    Refused outright when stdin is not a terminal. An unattended `--replace` -- in a
    script, a Dockerfile, a CI job -- is exactly the shape of the accident this guards
    against, and treating "no terminal" as consent would be the wrong default.
    """
    if not sys.stdin.isatty():
        message = "--replace needs a terminal: refusing to replace an account unattended."
        raise CommandError(message)
    # Phrased as what the flag does rather than as a claim about what is in the database.
    # The check happens later, inside the transaction that does the work, and a database
    # with no account yet is a perfectly ordinary thing to point this command at.
    emit(f"--replace deletes any existing account, and every session it holds, for '{username}'.")
    answer = input(f"Type '{username}' or 'y' to confirm: ").strip()
    if answer != username and answer.casefold() not in CONFIRMATION_WORDS:
        message = "Not confirmed. Nothing was changed."
        raise CommandError(message)


def password_hasher_from(settings: Settings) -> PasswordHasher:
    """The hasher the running application would build, from the same settings."""
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
    )


async def store_user(settings: Settings, username: str, password: str, *, replace: bool) -> None:
    """Open the database the application uses and write the account into it."""
    engine = create_database_engine(settings.database_url)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            service = build_auth_service(
                session,
                hasher=password_hasher_from(settings),
                lifetime=SessionLifetime.from_days(
                    idle_days=settings.session_idle_days,
                    absolute_days=settings.session_absolute_days,
                ),
                throttle=LoginThrottle(),
            )
            await service.create_user(username, password, replace=replace)
    finally:
        await engine.dispose()


def create_user(args: argparse.Namespace) -> int:
    """`create-user`: prompt for a password and create -- or replace -- the owner account."""
    settings = get_settings()
    username: str = args.username or settings.bootstrap_username
    replace: bool = args.replace

    if replace:
        confirm_replacement(username)
    password = prompt_for_password()

    # The schema has to exist before a row can go in it, and an operator recovering a
    # forgotten password on a fresh volume has no other way to create it.
    ensure_database_directory(settings.database_url)
    upgrade_to_head(settings.database_url)
    asyncio.run(store_user(settings, username, password, replace=replace))
    emit(f"Account '{username}' is ready.")
    return 0


def median_nanoseconds(samples: list[int]) -> int:
    """The median of a list of integer nanosecond durations, as an integer.

    Integer arithmetic throughout. `statistics.median` returns a float for an even-length
    input, and a float is not a thing this code base produces where it can avoid one.
    """
    ordered = sorted(samples)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def hash_benchmark(args: argparse.Namespace) -> int:
    """`hash-benchmark`: time the configured Argon2id parameters on this machine.

    This is the whole answer to "tuned on the Raspberry Pi": the parameters are settings,
    and this is how the number behind them is measured on the hardware that will run
    them. Run it over SSH on the Pi after a deploy, then set the environment variables.
    """
    hasher = password_hasher_from(get_settings())
    rounds: int = args.rounds
    sample = "benchmark phrase, long enough to be representative"

    hasher.hash(sample)  # Warm up: the first call pays for the library's own setup.
    timings = [_time_one_hash(hasher, sample) for _ in range(rounds)]

    median = Decimal(median_nanoseconds(timings)) / NANOSECONDS_PER_MILLISECOND
    emit(
        f"argon2id time_cost={hasher.time_cost} memory_cost={hasher.memory_cost} KiB "
        f"parallelism={hasher.parallelism}"
    )
    emit(f"median of {rounds} hashes: {median.quantize(Decimal('0.1'))} ms")
    emit(f"target: {TARGET_MILLISECONDS} ms")
    emit(
        "OWASP floor: memory_cost >= "
        f"{OWASP_MINIMUM_MEMORY_COST} KiB, time_cost >= {OWASP_MINIMUM_TIME_COST}"
    )
    return 0


def _time_one_hash(hasher: PasswordHasher, sample: str) -> int:
    """One hash, in whole nanoseconds. A monotonic counter, so a clock change cannot lie."""
    started = perf_counter_ns()
    hasher.hash(sample)
    return perf_counter_ns() - started


def build_parser() -> argparse.ArgumentParser:
    """The command line. Note what is absent: there is no way to pass a password."""
    parser = argparse.ArgumentParser(prog="portfolio", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser(
        "create-user",
        help="create the owner account, prompting for the password",
    )
    create.add_argument(
        "--username",
        default=None,
        help="the account name (default: PORTFOLIO_BOOTSTRAP_USERNAME)",
    )
    create.add_argument(
        "--replace",
        action="store_true",
        help="replace the existing account and revoke its sessions (asks for confirmation)",
    )
    create.set_defaults(handler=create_user)

    benchmark = commands.add_parser(
        "hash-benchmark",
        help="time the configured Argon2id parameters on this machine",
    )
    benchmark.add_argument(
        "--rounds",
        type=int,
        default=DEFAULT_BENCHMARK_ROUNDS,
        help=f"how many hashes to time (default: {DEFAULT_BENCHMARK_ROUNDS})",
    )
    benchmark.set_defaults(handler=hash_benchmark)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run a command, turning an expected failure into a message and an exit code."""
    args = build_parser().parse_args(argv)
    try:
        exit_code: int = args.handler(args)
    except (CommandError, PasswordPolicyError, AuthError) as exc:
        emit_error(str(exc))
        return 1
    return exit_code
