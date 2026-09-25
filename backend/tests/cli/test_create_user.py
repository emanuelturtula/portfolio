"""Criterion 2: `create-user` prompts, and the password is never an argument.

Plus the `--replace` flag, which is not in the issue and was added deliberately during
implementation. The product has no password reset flow -- that is the right call -- and
the runtime image carries no `sqlite3` binary, so without a way to set a new password a
forgotten one would mean an instance nobody can get back into. The flag is guarded by a
typed confirmation and refuses to run unattended, because what it does is take the account
away from whoever holds its current password and from every browser signed in to it.

**Since #69 it replaces the credential, not the owner** (spec 013). It used to delete the
account and insert a new one, and every row the owner has hangs off `users.id`: the wallets
and their balance history went with it through the cascade, and the first exchange fill
would have made the command fail outright on its `RESTRICT`. The tests below that seed a
portfolio are the ones that fail against that implementation, and that is what they are
for.
"""

from __future__ import annotations

import getpass
import re
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from portfolio import cli
from portfolio.config import get_settings
from portfolio.services.password_hasher import PasswordHasher
from tests.address_vectors import BIP173_TESTNET_P2WPKH
from tests.balance_harness import sqlite_timestamp
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
ANOTHER_TOKEN_HASH = "1" * 64

FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=64, parallelism=1)

#: Where the owner row is moved before a replacement, so that "the id did not change" is an
#: assertion that can fail. SQLite gives an inserted row `max(rowid) + 1`, which is 1 again
#: once the table has been emptied: an owner created at id 1, deleted and re-inserted, comes
#: back at id 1, and an `id` check over it would pass for exactly the wrong reason.
PINNED_OWNER_ID: Final = 42

#: The same argument for `created_at`, and a second one: a value fixed in the past cannot
#: coincide with the clock reading a re-insert would take, however fast the host is.
PINNED_CREATED_AT: Final = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)

#: The tables whose rows the spec lets `--replace` change. Every other table is compared
#: whole, so a table added after this test was written is covered without editing it.
CREDENTIAL_TABLES: Final = frozenset({"users", "sessions"})

OBSERVED_AT: Final = datetime(2026, 9, 20, 12, 0, 0, 123456, tzinfo=UTC)
EXECUTED_AT: Final = datetime(2026, 9, 3, 15, 30, 0, 123000, tzinfo=UTC)

#: Two usernames no refusal message could contain by accident. "owner" would not do: it is
#: an English word, and a message about "more than one owner account" would contain it.
FIRST_OF_TWO: Final = "alpha-keeper"
SECOND_OF_TWO: Final = "bravo-keeper"
A_THIRD_NAME: Final = "charlie-keeper"

#: An owner who is not called `PORTFOLIO_BOOTSTRAP_USERNAME`: the reviewer's reproduction.
OPERATORS_OWN_NAME: Final = "alice"

#: A bootstrap username that appears nowhere else in the suite.
DISTINCT_BOOTSTRAP_NAME: Final = "bootstrap-keeper"


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


def create_the_owner(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    username: str = OWNER_USERNAME,
) -> int:
    """Create the account through the command, then pin its identity to known values.

    Through the command rather than by hand, because the command is also what migrates the
    file; the pinning is by hand, for the reasons `PINNED_OWNER_ID` gives.
    """
    script_prompts(monkeypatch, [OWNER_PHRASE, OWNER_PHRASE])
    assert cli.main(["create-user", "--username", username]) == 0
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE users SET id = :id, created_at = :created_at"),
            {"id": PINNED_OWNER_ID, "created_at": sqlite_timestamp(PINNED_CREATED_AT)},
        )
    return PINNED_OWNER_ID


def run_replace(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> int:
    """`create-user --replace`, confirmed, with the replacement phrase typed twice."""
    script_confirmation(monkeypatch, "y")
    script_prompts(monkeypatch, [REPLACEMENT_PHRASE, REPLACEMENT_PHRASE])
    return cli.main(["create-user", "--replace", *arguments])


def database_contents(engine: Engine) -> dict[str, list[dict[str, object]]]:
    """Every row of every table, as SQLite stores it, keyed by table name.

    Read raw -- no ORM, no type decorators -- so two readings compare the stored values
    byte for byte, and in `rowid` order so they compare in the order they were written.
    """
    names = sorted(inspect(engine).get_table_names())
    with engine.connect() as connection:
        return {
            # The table name comes from the schema's own catalogue, never from input.
            name: [
                dict(row)
                for row in connection.execute(
                    text(f'SELECT * FROM "{name}" ORDER BY rowid')  # noqa: S608
                ).mappings()
            ]
            for name in names
        }


def give_the_owner_a_portfolio(engine: Engine, owner_id: int, *, with_fill: bool) -> None:
    """One wallet with one balance snapshot, and one exchange account, optionally with a fill.

    Raw SQL through a second connection, the way the balance and exchange suites seed their
    rows: what is being protected is what is in the file, not what an ORM session believes.
    The snapshot needs a run to point at, and a real run records its chain, so both are
    written too. The address is a BIP-173 testnet vector; rule 3 forbids a mainnet one.
    """
    now = sqlite_timestamp(OBSERVED_AT)
    with engine.begin() as connection:
        wallet_id = connection.execute(
            text(
                "INSERT INTO wallets (user_id, chain_key, address_canonical, address_display, "
                "label, archived_at, created_at, updated_at) "
                "VALUES (:user_id, 'bitcoin', :address, :address, 'cold storage', NULL, "
                ":now, :now) RETURNING id"
            ),
            {"user_id": owner_id, "address": BIP173_TESTNET_P2WPKH, "now": now},
        ).scalar_one()
        run_id = connection.execute(
            text(
                "INSERT INTO sync_runs (trigger, status, started_at, finished_at, duration_ms, "
                "wallets_total, wallets_succeeded, wallets_failed) "
                "VALUES ('scheduled', 'success', :now, :now, 1, 1, 1, 0) RETURNING id"
            ),
            {"now": now},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO sync_run_chains (sync_run_id, chain_key, status, wallets_read, "
                "error_kind, detail) VALUES (:run_id, 'bitcoin', 'success', 1, NULL, NULL)"
            ),
            {"run_id": run_id},
        )
        connection.execute(
            text(
                "INSERT INTO balance_snapshots (wallet_id, sync_run_id, confirmed, pending, "
                "decimals, observed_at) VALUES (:wallet_id, :run_id, 123456789, 42, 8, :now)"
            ),
            {"wallet_id": wallet_id, "run_id": run_id, "now": now},
        )
        account_id = connection.execute(
            text(
                "INSERT INTO exchange_accounts (user_id, exchange_key, created_at) "
                "VALUES (:user_id, 'bitget', :now) RETURNING id"
            ),
            {"user_id": owner_id, "now": now},
        ).scalar_one()
        if with_fill:
            connection.execute(
                text(
                    "INSERT INTO exchange_fills (exchange_account_id, external_trade_id, "
                    "external_order_id, symbol, base_asset, quote_asset, side, quantity, "
                    "price, quote_quantity, quote_quantity_derived, fee_amount, fee_asset, "
                    "executed_at, raw_payload, ingested_at) "
                    "VALUES (:account_id, '1001', '5001', 'BTCUSDT', 'BTC', 'USDT', 'buy', "
                    "'0.000424242424242424', '86000.100000000000000000', "
                    "'36.484273484273484273', 0, '0.036484273484273484', 'USDT', "
                    ":executed_at, :raw_payload, :now)"
                ),
                {
                    "account_id": account_id,
                    "executed_at": sqlite_timestamp(EXECUTED_AT),
                    "raw_payload": '{"tradeId":"1001"}',
                    "now": now,
                },
            )


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


@pytest.mark.parametrize(
    "with_fill", [True, False], ids=["after-the-first-fill", "before-any-fill"]
)
def test_create_user_replace_keeps_every_row_that_belongs_to_the_owner(
    cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    with_fill: bool,
) -> None:
    """Spec 013, criteria 1 and 2: recovering the password takes nothing else with it.

    Both of the ways the old delete-then-insert lost data are here, one per parameter.
    **After the first fill** the delete cascades to the exchange account, the fill's
    `RESTRICT` refuses, and the command dies on an `IntegrityError` -- a forgotten password
    becomes a lost instance again. **Before any fill**, which is every deployment today, the
    command succeeds and quietly takes every wallet and its whole balance history with it.

    Every table outside `users` and `sessions` is compared whole, row by row and value by
    value, rather than counted: a count survives a row that was deleted and re-created with
    a new id, and a table added later is covered without anyone editing this test.
    """
    del cli_database
    owner_id = create_the_owner(monkeypatch, sync_engine)
    give_the_owner_a_portfolio(sync_engine, owner_id, with_fill=with_fill)
    insert_session(sync_engine, owner_id, A_TOKEN_HASH)
    before = database_contents(sync_engine)
    seeded = {
        "wallets": 1,
        "sync_runs": 1,
        "balance_snapshots": 1,
        "exchange_accounts": 1,
        "exchange_fills": 1 if with_fill else 0,
    }
    # The comparison below is only worth something if there was something to compare.
    assert {table: len(before[table]) for table in seeded} == seeded

    assert run_replace(monkeypatch) == 0

    after = database_contents(sync_engine)
    assert {table: rows for table, rows in after.items() if table not in CREDENTIAL_TABLES} == {
        table: rows for table, rows in before.items() if table not in CREDENTIAL_TABLES
    }
    # Still the owner's: the rows point at the account that now answers to the new password.
    [owner] = after["users"]
    assert owner["id"] == owner_id
    assert after["wallets"][0]["user_id"] == owner_id
    assert after["exchange_accounts"][0]["user_id"] == owner_id
    assert FAST_HASHER.verify(str(owner["password_hash"]), REPLACEMENT_PHRASE)
    assert after["sessions"] == []


def test_create_user_replace_replaces_the_credential_and_revokes_its_sessions(
    cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing the credential keeps the account and revokes every session it holds.

    The revoke is explicit now: the account is updated in place, so there is no delete for
    a cascade to follow. A session row that survived would be a stolen cookie outliving the
    very recovery meant to defeat it -- which is why the assertion is that there is no row
    *at all*, not merely none for this user id: a revoke aimed at the wrong id leaves one.
    """
    del cli_database
    owner_id = create_the_owner(monkeypatch, sync_engine)
    insert_session(sync_engine, owner_id, A_TOKEN_HASH)
    insert_session(sync_engine, owner_id, ANOTHER_TOKEN_HASH)
    assert len(read_sessions(sync_engine)) == 2

    assert run_replace(monkeypatch) == 0

    users = database_contents(sync_engine)["users"]
    assert [(user["id"], user["username"], user["created_at"]) for user in users] == [
        (owner_id, OWNER_USERNAME, sqlite_timestamp(PINNED_CREATED_AT))
    ]
    assert FAST_HASHER.verify(str(users[0]["password_hash"]), REPLACEMENT_PHRASE)
    assert not FAST_HASHER.verify(str(users[0]["password_hash"]), OWNER_PHRASE)
    assert read_sessions(sync_engine) == []


def test_create_user_replace_can_rename_the_owner(
    cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--replace --username` renames the one account in place, not by making a second."""
    del cli_database
    owner_id = create_the_owner(monkeypatch, sync_engine)
    assert [user.username for user in read_users(sync_engine)] == [OWNER_USERNAME]

    assert run_replace(monkeypatch, "--username", "keeper") == 0

    users = database_contents(sync_engine)["users"]
    assert [(user["id"], user["username"], user["created_at"]) for user in users] == [
        (owner_id, "keeper", sqlite_timestamp(PINNED_CREATED_AT))
    ]
    assert FAST_HASHER.verify(str(users[0]["password_hash"]), REPLACEMENT_PHRASE)


def test_create_user_replace_without_a_username_keeps_the_owners_name(
    cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec 013, change A: no `--username` means the account keeps the name it has.

    The owner here is not called `PORTFOLIO_BOOTSTRAP_USERNAME`, and that difference is the
    whole test. The command used to resolve a missing `--username` to the default before it
    knew an account existed, so copying the recovery line from `docs/operations.md` renamed
    `alice` to `owner`, during a recovery. A recovery is when the operator is least likely
    to notice.
    """
    del cli_database
    owner_id = create_the_owner(monkeypatch, sync_engine, username=OPERATORS_OWN_NAME)
    assert get_settings().bootstrap_username != OPERATORS_OWN_NAME
    capsys.readouterr()

    assert run_replace(monkeypatch) == 0

    # The success line names the account that was changed, not the default it fell back to.
    last_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert last_line == f"Account '{OPERATORS_OWN_NAME}' is ready."
    users = database_contents(sync_engine)["users"]
    assert [(user["id"], user["username"], user["created_at"]) for user in users] == [
        (owner_id, OPERATORS_OWN_NAME, sqlite_timestamp(PINNED_CREATED_AT))
    ]
    # Kept the name, and still changed the password: not a command that did nothing.
    assert FAST_HASHER.verify(str(users[0]["password_hash"]), REPLACEMENT_PHRASE)
    assert not FAST_HASHER.verify(str(users[0]["password_hash"]), OWNER_PHRASE)


def test_create_user_replace_on_an_empty_database_creates_the_account(
    cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no account yet, `--replace` creates one: a fresh volume is an ordinary target.

    With no `--username`, the new account is named by `PORTFOLIO_BOOTSTRAP_USERNAME`. The
    setting is changed to a value used nowhere else, so a name hard-coded anywhere on the
    path cannot pass for the setting.
    """
    del cli_database
    monkeypatch.setenv("PORTFOLIO_BOOTSTRAP_USERNAME", DISTINCT_BOOTSTRAP_NAME)
    get_settings.cache_clear()

    assert run_replace(monkeypatch) == 0

    users = read_users(sync_engine)
    assert [user.username for user in users] == [DISTINCT_BOOTSTRAP_NAME]
    assert FAST_HASHER.verify(users[0].password_hash, REPLACEMENT_PHRASE)


def test_create_user_replace_refuses_when_more_than_one_account_exists(
    cli_database: Path,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec 013, criterion 4: two accounts, and the command will not guess which one is meant.

    Nothing in the application can make a second account, so these two come from SQL typed
    by hand. Updating either would be a guess about who the operator is, so the command
    refuses, changes nothing anywhere in the file, and names neither account. The operator
    passes a third name, so that neither existing name can reach the output by any route.
    """
    del cli_database
    first_id = create_the_owner(monkeypatch, sync_engine, username=FIRST_OF_TWO)
    with sync_engine.begin() as connection:
        second_id = connection.execute(
            text(
                "INSERT INTO users (username, password_hash, created_at) "
                "VALUES (:username, :password_hash, :created_at) RETURNING id"
            ),
            {
                "username": SECOND_OF_TWO,
                "password_hash": FAST_HASHER.hash(UNUSED_PHRASE),
                "created_at": sqlite_timestamp(PINNED_CREATED_AT),
            },
        ).scalar_one()
    insert_session(sync_engine, first_id, A_TOKEN_HASH)
    insert_session(sync_engine, second_id, ANOTHER_TOKEN_HASH)
    before = database_contents(sync_engine)
    assert len(before["users"]) == 2
    assert len(before["sessions"]) == 2
    capsys.readouterr()

    assert run_replace(monkeypatch, "--username", A_THIRD_NAME) == 1

    captured = capsys.readouterr()
    assert database_contents(sync_engine) == before
    assert captured.err.strip() != ""
    assert re.search(r"\b(2|two)\b", captured.err, flags=re.IGNORECASE), captured.err
    for name in (FIRST_OF_TWO, SECOND_OF_TWO):
        assert name not in captured.out
        assert name not in captured.err


@pytest.mark.parametrize(
    ("account_exists", "arguments", "name", "rename"),
    [
        (True, [], OWNER_USERNAME, False),
        (True, ["--username", "keeper"], "keeper", True),
        (False, [], OWNER_USERNAME, False),
    ],
    ids=["existing-account-no-username", "existing-account-with-username", "empty-database"],
)
def test_create_user_replace_says_what_it_keeps(
    cli_database: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    account_exists: bool,
    arguments: list[str],
    name: str,
    rename: bool,
) -> None:
    """Spec 013, criterion 5 and change B: the confirmation is true in every case it can meet.

    The command asks before it has read the database, so the same words have to be true
    whether or not an account exists. They must not claim anything is deleted. They must
    say sessions are signed out and data is kept. They must name a rename only when
    `--username` asked for one: without it, the text says the username is kept.

    What was on screen is captured at the moment the question is asked, so this reads the
    warning the operator answers and not the success line that follows it.
    """
    del cli_database
    if account_exists:
        script_prompts(monkeypatch, [OWNER_PHRASE, OWNER_PHRASE])
        assert cli.main(["create-user"]) == 0
    capsys.readouterr()

    shown: list[str] = []

    def answer(prompt: str = "") -> str:
        shown.append(capsys.readouterr().out + prompt)
        return "y"

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", answer)
    script_prompts(monkeypatch, [REPLACEMENT_PHRASE, REPLACEMENT_PHRASE])
    assert cli.main(["create-user", "--replace", *arguments]) == 0

    assert len(shown) == 1
    warning = shown[0].casefold()
    assert "delet" not in warning
    assert "session" in warning
    assert re.search(r"\bsign(s|ed)? out\b", warning), warning
    assert "wallets" in warning
    # What happens on a database with no account yet, which this may well be.
    assert f"creates '{name}'" in warning, warning
    [kept] = [sentence for sentence in re.split(r"(?<=\.)\s+", warning) if "kept" in sentence]
    if rename:
        assert f"becomes '{name}'" in warning, warning
        assert "username" not in kept, warning
    else:
        assert "becomes" not in warning, warning
        assert "username" in kept, warning


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
    """Changing the only credential is not one flag away: the operator has to say so."""
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
