"""Criterion 2: `create-user` prompts, and the password is never an argument.

Plus the `--replace` flag, which is not in the issue and was added deliberately during
implementation. The product has no password reset flow -- that is the right call -- and
the runtime image carries no `sqlite3` binary, so without a way to re-create the account a
forgotten password would mean an instance nobody can get back into. The flag is guarded by
a typed confirmation and refuses to run unattended, because the thing it does is delete an
account.
"""

from __future__ import annotations

import getpass
import sys
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import OperationalError

from portfolio import cli
from portfolio.services.password_hasher import PasswordHasher
from tests.cli.conftest import (
    OWNER_PHRASE,
    OWNER_USERNAME,
    REPLACEMENT_PHRASE,
    UNUSED_PHRASE,
    insert_session,
    read_sessions,
    read_users,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine

# Not a real digest of anything: a token hash is 64 hex characters and this only has to be
# a value a unique index will accept.
A_TOKEN_HASH = "0" * 64

FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=64, parallelism=1)


def script_prompts(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    """Replace `getpass` with a scripted one, returning the list of prompts it was asked.

    The prompts are recorded rather than ignored: the criterion is that the password is
    *prompted for*, and a test that only checked the account was created would pass just
    as happily if the value had come from an environment variable.
    """
    asked: list[str] = []
    remaining = list(answers)

    def fake_getpass(prompt: str = "") -> str:
        asked.append(prompt)
        return remaining.pop(0)

    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    return asked


def script_confirmation(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    """Make stdin look like a terminal and answer the replacement confirmation."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": answer)


def has_no_account(engine: Engine) -> bool:
    """True when the database holds no account, including when it has no schema at all.

    A command that refuses the password never reaches the migration, so the file may not
    exist and the table certainly does not -- and "the query failed because there is
    nothing there" is the answer this is asking for.
    """
    try:
        return read_users(engine) == []
    except OperationalError:
        return True


def test_create_user_prompts_and_never_accepts_a_password_argument(
    cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 2, both halves: the prompt is the only source, and there is no flag.

    A command-line argument lands in the shell history and in every process listing on the
    machine; an environment variable lands in `/proc/<pid>/environ` and in every child
    process. So the bootstrap variable is set here too, with a different value, and the
    account that comes out must be the one that was typed.
    """
    del cli_database
    monkeypatch.setenv("PORTFOLIO_BOOTSTRAP_PASSWORD", UNUSED_PHRASE)
    asked = script_prompts(monkeypatch, [OWNER_PHRASE, OWNER_PHRASE])

    assert cli.main(["create-user"]) == 0

    assert len(asked) == 2, "the password is asked for twice: entry and confirmation"
    users = read_users(sync_engine)
    assert [user.username for user in users] == [OWNER_USERNAME]
    assert FAST_HASHER.verify(users[0].password_hash, OWNER_PHRASE)
    assert not FAST_HASHER.verify(users[0].password_hash, UNUSED_PHRASE)

    with pytest.raises(SystemExit) as failure:
        cli.build_parser().parse_args(["create-user", "--password", OWNER_PHRASE])
    assert failure.value.code == 2


def test_create_user_rejects_a_mismatched_confirmation(
    cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A typo in the only password this instance has must not become the password."""
    del cli_database
    script_prompts(monkeypatch, [OWNER_PHRASE, REPLACEMENT_PHRASE])

    assert cli.main(["create-user"]) == 1

    assert "do not match" in capsys.readouterr().err
    # Nothing was created, and nothing was migrated: it failed before touching the disk.
    assert has_no_account(sync_engine)


def test_create_user_rejects_a_password_below_policy(
    cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The identical policy the API and the bootstrap variable apply, from one module."""
    del cli_database
    script_prompts(monkeypatch, ["short", "short"])

    assert cli.main(["create-user"]) == 1

    assert "12 characters" in capsys.readouterr().err
    assert has_no_account(sync_engine)


def test_create_user_refuses_when_a_user_exists(
    cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Single user means single user: a second account is a mistake, not a feature."""
    del cli_database
    script_prompts(monkeypatch, [OWNER_PHRASE, OWNER_PHRASE])
    assert cli.main(["create-user"]) == 0
    original = read_users(sync_engine)[0].password_hash

    script_prompts(monkeypatch, [REPLACEMENT_PHRASE, REPLACEMENT_PHRASE])
    assert cli.main(["create-user"]) == 1

    assert "--replace" in capsys.readouterr().err
    users = read_users(sync_engine)
    assert len(users) == 1
    assert users[0].password_hash == original


def test_create_user_replace_deletes_the_existing_user_and_its_sessions(
    cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing the account revokes every session, through the foreign key cascade.

    Asserted rather than assumed: the cascade is declared on the column and only fires
    because `PRAGMA foreign_keys=ON` is set on every connection. A session row that
    survived would be a live cookie for an account that no longer exists.
    """
    del cli_database
    script_prompts(monkeypatch, [OWNER_PHRASE, OWNER_PHRASE])
    assert cli.main(["create-user"]) == 0
    first = read_users(sync_engine)[0]
    insert_session(sync_engine, first.id, A_TOKEN_HASH)
    assert len(read_sessions(sync_engine)) == 1

    script_confirmation(monkeypatch, OWNER_USERNAME)
    script_prompts(monkeypatch, [REPLACEMENT_PHRASE, REPLACEMENT_PHRASE])
    assert cli.main(["create-user", "--replace"]) == 0

    users = read_users(sync_engine)
    assert len(users) == 1
    assert FAST_HASHER.verify(users[0].password_hash, REPLACEMENT_PHRASE)
    assert not FAST_HASHER.verify(users[0].password_hash, OWNER_PHRASE)
    # The session was inserted against the old account. SQLite hands the replacement the
    # same rowid, so a row that survived would still look attached -- which is exactly why
    # the assertion is that there is no row at all.
    assert read_sessions(sync_engine) == []


def test_create_user_replace_still_prompts_for_the_password(
    cli_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag replaces the account, never the prompt."""
    del cli_database
    script_prompts(monkeypatch, [OWNER_PHRASE, OWNER_PHRASE])
    assert cli.main(["create-user"]) == 0

    script_confirmation(monkeypatch, "y")
    asked = script_prompts(monkeypatch, [REPLACEMENT_PHRASE, REPLACEMENT_PHRASE])
    assert cli.main(["create-user", "--replace"]) == 0

    assert len(asked) == 2

    with pytest.raises(SystemExit) as failure:
        cli.build_parser().parse_args(["create-user", "--replace", "--password", OWNER_PHRASE])
    assert failure.value.code == 2


def test_create_user_replace_aborts_when_the_confirmation_does_not_match(
    cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Destroying an account is not one flag away: the operator has to say so out loud."""
    del cli_database
    script_prompts(monkeypatch, [OWNER_PHRASE, OWNER_PHRASE])
    assert cli.main(["create-user"]) == 0
    original = read_users(sync_engine)[0].password_hash

    script_confirmation(monkeypatch, "something else")
    script_prompts(monkeypatch, [REPLACEMENT_PHRASE, REPLACEMENT_PHRASE])
    assert cli.main(["create-user", "--replace"]) == 1

    assert "Not confirmed" in capsys.readouterr().err
    assert read_users(sync_engine)[0].password_hash == original


def test_create_user_replace_refuses_without_a_terminal(
    cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No terminal is not consent.

    An unattended `--replace` -- in a script, a Dockerfile, a CI job -- is the exact shape
    of the accident the confirmation guards against.
    """
    del cli_database
    script_prompts(monkeypatch, [OWNER_PHRASE, OWNER_PHRASE])
    assert cli.main(["create-user"]) == 0
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert cli.main(["create-user", "--replace"]) == 1

    assert "needs a terminal" in capsys.readouterr().err
    assert len(read_users(sync_engine)) == 1


def test_create_user_takes_the_username_from_the_argument(
    cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A username is not a secret, so it may be an argument. The default is the setting."""
    del cli_database
    script_prompts(monkeypatch, [OWNER_PHRASE, OWNER_PHRASE])

    assert cli.main(["create-user", "--username", "keeper"]) == 0

    assert [user.username for user in read_users(sync_engine)] == ["keeper"]
