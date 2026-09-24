"""Criteria 7 and 8: an address never reaches a log record, and no fixture is mainnet.

An address is the owner's holdings in one string. Anyone who has it can read every balance
that address has ever held and every transaction it has ever been part of, forever and
without asking permission. A log line is a file that gets copied, tailed, shipped and
pasted into an issue, so an address in one is a permanent disclosure made by accident.

## Why three different tests, none of which is redundant

* **The redaction fragment** covers the field name. It is the backstop for the log
  statement somebody adds in a hurry in two years.
* **The captured log from a real request** covers the code as it is today. The fragment
  being correct does not prove the service never logs the value under a differently-named
  key -- `wallet=` or `target=` or inside an exception message -- and that is the failure
  mode that actually happens.
* **The source walk** covers what neither of the other two can see: a log call that is
  correct today on a path no test exercises.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from portfolio.api.errors import PROBLEM_CONTENT_TYPE
from portfolio.config import Settings
from portfolio.logging import (
    REDACTED,
    SENSITIVE_KEY_FRAGMENTS,
    configure_logging,
    is_sensitive_key,
    redact_sensitive,
)
from portfolio.repositories.wallets import WalletRepository
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WPKH_UPPERCASE,
    KASPA_TESTNET_V1_KEY,
    NAMED_CORRUPTIONS,
)
from tests.auth.conftest import BASE_URL as SECURE_BASE_URL
from tests.auth.conftest import JSON_HEADERS
from tests.security.conftest import (
    PRODUCTION_ORIGIN,
    assert_absent,
    rendered,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from fastapi import FastAPI

    from portfolio.db.models import Wallet

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
SOURCE_ROOT: Final = REPO_ROOT / "backend" / "src" / "portfolio"
TESTS_ROOT: Final = REPO_ROOT / "backend" / "tests"

WALLETS: Final = "/api/wallets"


# --------------------------------------------------------------------------------------
# Criterion 7, part one: `address` is a redacted key
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "address",
        "ADDRESS",
        "Address",
        "address_canonical",
        "address_display",
        "wallet_address",
        "addresses",
        "from_address",
        "x-address",
    ],
)
def test_address_key_is_redacted(key: str) -> None:
    """Criterion 7: any field whose name contains `address` has its value replaced.

    A substring match, so it also covers `address_canonical`, `address_display` and --
    deliberately -- `email_address`. That breadth is the point: the names a future log call
    will use are not knowable now, and the cost of over-redacting a field name is a log
    line that says `[REDACTED]` where it did not have to.
    """
    assert is_sensitive_key(key)
    assert redact_sensitive(None, "info", {key: BIP173_TESTNET_P2WPKH}) == {key: REDACTED}


def test_the_address_fragment_is_in_the_list() -> None:
    """Pinned, so that removing it is a visible line in a diff rather than a quiet one."""
    assert "address" in SENSITIVE_KEY_FRAGMENTS


def test_redaction_reaches_an_address_nested_in_a_structure() -> None:
    """A wallet usually arrives in a log as part of something, not as a bare field."""
    event: dict[str, Any] = {
        "event": "wallet_created",
        "wallet": {"id": 1, "address_display": BIP173_TESTNET_P2WPKH, "chain_key": "bitcoin"},
        "batch": [{"address": KASPA_TESTNET_V1_KEY}, {"address": BIP173_TESTNET_P2WPKH}],
    }

    redacted = json.dumps(redact_sensitive(None, "info", event))

    assert BIP173_TESTNET_P2WPKH not in redacted
    assert KASPA_TESTNET_V1_KEY not in redacted
    assert redacted.count(REDACTED) == 3
    assert "wallet_created" in redacted
    assert "bitcoin" in redacted


def test_an_ordinary_wallet_field_is_still_readable() -> None:
    """Over-redaction has a cost too: a log with nothing in it debugs nothing."""
    assert not is_sensitive_key("chain_key")
    assert not is_sensitive_key("wallet_id")
    assert not is_sensitive_key("archived")


# --------------------------------------------------------------------------------------
# Criterion 7, part two: a real request, and every line it actually printed
# --------------------------------------------------------------------------------------
#
# **These tests read stdout, not `structlog.testing.capture_logs`.** That is the whole
# point of this section and it was learned the hard way: the version of it that shipped
# used `capture_logs`, whose docstring claimed it caught an address "bound as a field,
# formatted into an event name, or carried inside an exception". It could not catch the
# third. `capture_logs` swaps the entire processor chain out for a `LogCapture`, so
# `format_exc_info` never runs, the traceback is never rendered to a string, and the
# captured entry holds `exc_info: True` and nothing else. An address inside an exception
# message was invisible to the assertion -- and one was: a concurrent duplicate `POST`
# raised `IntegrityError`, whose text carried SQLAlchemy's bound parameters, both address
# columns among them, straight into the production JSON log.
#
# The general form of the mistake is worth naming, because it has now appeared three times
# in this issue: a verifier that shares state with the thing it verifies can only confirm
# its own account. `capture_logs` replaces the pipeline and then reports on the pipeline.
# So these tests install the **production** pipeline, drive real requests through it, and
# read the bytes that reach stdout -- which is the artifact that actually gets copied,
# tailed and pasted into an issue.


async def test_no_log_line_contains_an_address(
    signed_in_api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[[], None],
) -> None:
    """Criterion 7, against the bytes the process actually writes.

    Every request the registry serves is driven here -- create, list, patch, archive, the
    three ordinary failure paths, **and a duplicate that loses the race**, which is the
    only one that reaches the unhandled-exception handler. The assertion is on rendered
    stdout, so an address is caught whether it was bound as a field, formatted into an
    event name, or carried inside an exception's text.

    The race is simulated rather than waited for. `find_by_canonical` is made to answer
    `None`, which is exactly what it answers when the competing transaction has not yet
    committed at the moment this request's pre-check runs. Everything after that point --
    the insert, the constraint, the exception, the handler, the renderer -- is the real
    code path, and the rendered `IntegrityError` is what carried both address columns into
    production's JSON log.
    """
    production_logging()
    corrupted = NAMED_CORRUPTIONS[0][2]

    created = await signed_in_api_client.post(
        WALLETS,
        json={
            "chain_key": "bitcoin",
            "address": BIP173_TESTNET_P2WPKH_UPPERCASE,
            "label": "Cold storage",
        },
        headers=JSON_HEADERS,
    )
    assert created.status_code == 201, created.text
    wallet_id = created.json()["id"]

    await signed_in_api_client.get(WALLETS)
    await signed_in_api_client.get(WALLETS, params={"include_archived": "true"})
    await signed_in_api_client.patch(
        f"{WALLETS}/{wallet_id}", json={"label": "Renamed"}, headers=JSON_HEADERS
    )
    # The failure paths, where an error message is most likely to quote what it refused.
    duplicate = await signed_in_api_client.post(
        WALLETS,
        json={"chain_key": "bitcoin", "address": BIP173_TESTNET_P2WPKH},
        headers=JSON_HEADERS,
    )
    assert duplicate.status_code == 409, duplicate.text
    invalid = await signed_in_api_client.post(
        WALLETS,
        json={"chain_key": "bitcoin", "address": corrupted},
        headers=JSON_HEADERS,
    )
    assert invalid.status_code == 422, invalid.text
    missing = await signed_in_api_client.delete(f"{WALLETS}/999999", headers=JSON_HEADERS)
    assert missing.status_code == 404

    lose_the_race(monkeypatch)
    raced = await signed_in_api_client.post(
        WALLETS,
        json={"chain_key": "bitcoin", "address": BIP173_TESTNET_P2WPKH, "label": "Raced"},
        headers=JSON_HEADERS,
    )
    assert raced.status_code in {409, 500}, raced.text

    written = capsys.readouterr().out

    assert written.strip(), "nothing was written to stdout, so this test proves nothing"
    assert "wallet" in written or "request" in written, (
        "stdout carried no request log at all; the pipeline is not the one under test"
    )
    assert_absent(written, BIP173_TESTNET_P2WPKH, BIP173_TESTNET_P2WPKH_UPPERCASE, corrupted)


def lose_the_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the duplicate pre-check miss **once**, then behave normally again.

    That single call is the whole race. Two requests arrive together, both run
    `find_by_canonical` before either has committed, both find nothing, both insert, and
    the second meets `uq_wallets_user_chain_address`. By the time the service looks again
    to establish *why* the insert was refused, the competing row is committed and visible
    -- so the recovery lookup must see it.

    Patching the method to answer `None` unconditionally, which is what this helper did
    first, models something else entirely: a database that has lost the row. The service
    correctly refuses to call that a conflict, re-raises, and the test then measures the
    handling of a bug rather than the handling of a race. A simulation that is wrong in
    that direction is worse than none, because it fails and looks like a real defect.
    """
    real = WalletRepository.find_by_canonical
    missed = False

    async def absent_once(
        self: WalletRepository,
        *,
        user_id: int,
        chain_key: str,
        address_canonical: str,
    ) -> Wallet | None:
        nonlocal missed
        if not missed:
            missed = True
            return None
        return await real(
            self,
            user_id=user_id,
            chain_key=chain_key,
            address_canonical=address_canonical,
        )

    monkeypatch.setattr(WalletRepository, "find_by_canonical", absent_once)


async def test_a_duplicate_that_loses_the_race_is_409_and_logs_no_address(
    signed_in_api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[[], None],
) -> None:
    """The defect criterion 7's original test could not see, pinned from both sides.

    Before the fix this answered `500` and printed SQLAlchemy's bound parameters --
    `address_canonical` and `address_display`, verbatim -- into the production log. Two
    separate things were wrong and each needs its own assertion, because fixing one
    without the other still leaves a bug:

    * the constraint firing is a **conflict**, not an internal error. The caller asked for
      something the current state of the data refuses, which is exactly 409, and a 500
      tells them to retry something that will never succeed;
    * a driver exception must not carry column values into a log. No amount of care in
      this repository's own code prevents that, because the string is built inside
      SQLAlchemy -- only `hide_parameters` does.
    """
    production_logging()
    first = await signed_in_api_client.post(
        WALLETS,
        json={"chain_key": "bitcoin", "address": BIP173_TESTNET_P2WPKH, "label": "First"},
        headers=JSON_HEADERS,
    )
    assert first.status_code == 201, first.text

    lose_the_race(monkeypatch)
    response = await signed_in_api_client.post(
        WALLETS,
        json={"chain_key": "bitcoin", "address": BIP173_TESTNET_P2WPKH, "label": "Second"},
        headers=JSON_HEADERS,
    )

    written = capsys.readouterr().out

    assert response.status_code == 409, (
        f"a lost race must be a conflict, not an internal error: {response.text}"
    )
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert BIP173_TESTNET_P2WPKH not in response.text
    assert written.strip(), "nothing was written to stdout, so this test proves nothing"
    assert_absent(written, BIP173_TESTNET_P2WPKH)


async def test_a_constraint_refusal_that_is_not_a_duplicate_logs_no_address(
    api_app: FastAPI,
    signed_in_api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[[], None],
) -> None:
    """The path where `hide_parameters` is the *only* thing standing between the row and
    the log.

    `wallets` carries three constraints and only one of them means "duplicate". A foreign
    key or chain-check refusal is a bug in this process rather than a race, so the service
    deliberately re-raises it: it becomes a 500, and `handle_unexpected_error` calls
    `logger.exception`, which renders the chained driver error in full.

    On the duplicate path that never happens -- the conflict is translated to a 409 and
    nothing is logged at all -- so reverting `hide_parameters` alone leaves
    `test_a_duplicate_that_loses_the_race_is_409_and_logs_no_address` green. **This is the
    test that goes red for that mutation**, and without it the flag would be held in place
    only by the repository-level tests, which do not exercise the handler that does the
    rendering.

    Driven by pointing the insert at an account that does not exist, which is a real
    foreign key violation from a real driver, rather than by raising a stand-in.
    """
    production_logging()
    real_add = WalletRepository.add

    async def add_for_a_missing_account(
        self: WalletRepository,
        *,
        user_id: int,
        chain_key: str,
        address_canonical: str,
        address_display: str,
        label: str | None,
        created_at: datetime,
    ) -> Wallet:
        del user_id
        return await real_add(
            self,
            user_id=999_999,
            chain_key=chain_key,
            address_canonical=address_canonical,
            address_display=address_display,
            label=label,
            created_at=created_at,
        )

    monkeypatch.setattr(WalletRepository, "add", add_for_a_missing_account)

    # A client that does **not** re-raise the application's exception, because uvicorn
    # does not either: in production `handle_unexpected_error` renders a 500 problem
    # document and the server keeps going. `ASGITransport` defaults to re-raising, which
    # is convenient for most tests and wrong for this one -- the response is half the
    # claim being made.
    transport = ASGITransport(app=api_app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url=SECURE_BASE_URL,
        cookies=signed_in_api_client.cookies,
    ) as client:
        response = await client.post(
            WALLETS,
            json={
                "chain_key": "bitcoin",
                "address": BIP173_TESTNET_P2WPKH,
                "label": "Cold storage",
            },
            headers=JSON_HEADERS,
        )
    written = capsys.readouterr().out

    # Not a conflict: nothing holds the slot, so reporting 409 would send the owner
    # hunting for a duplicate that does not exist.
    assert response.status_code == 500, response.text
    assert BIP173_TESTNET_P2WPKH not in response.text
    assert "unhandled_exception" in written, written[:400]
    assert_absent(written, BIP173_TESTNET_P2WPKH)
    assert "Cold storage" not in written


async def test_the_stdout_capture_really_captures(
    signed_in_api_client: AsyncClient,
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[[], None],
) -> None:
    """The guard on the two tests above: an empty capture would pass them silently.

    This is the failure mode that let the redaction test pass for a while without ever
    logging anything, so it is now asserted for the stdout path as well -- positively, on
    a request that is guaranteed to log a refusal.
    """
    production_logging()
    response = await signed_in_api_client.delete(f"{WALLETS}/424242", headers=JSON_HEADERS)
    written = capsys.readouterr().out

    assert response.status_code == 404
    assert "request_failed" in written, written[:400]
    assert '"status": 404' in written or '"status":404' in written, written[:400]


async def test_an_address_is_absent_from_a_log_of_an_unauthenticated_attempt(
    api_client: AsyncClient,
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[[], None],
) -> None:
    """The middleware logs every refusal, and a refused request still carries a body."""
    production_logging()
    response = await api_client.post(
        WALLETS,
        json={"chain_key": "kaspa", "address": KASPA_TESTNET_V1_KEY},
        headers=JSON_HEADERS,
    )
    written = capsys.readouterr().out

    assert response.status_code == 401
    assert "request_refused" in written, written[:400]
    assert_absent(written, KASPA_TESTNET_V1_KEY)


def test_the_pipeline_does_not_redact_an_exception_message(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[[], None],
) -> None:
    """The residual, asserted rather than assumed, because assuming it is what went wrong.

    Redaction works on **key names**. An exception's rendered traceback arrives as the
    value of `exception`, which is not a sensitive name and whose text no processor
    inspects, so anything inside an exception message is printed in full. That is not a
    bug to be fixed here -- a redactor that scanned every string for anything
    address-shaped would be slow, would mangle tracebacks, and would still miss a
    truncated address.

    It is the reason the two rules that *do* protect this path have to hold: nothing in
    this repository may put an address into an exception message, and the database driver
    must not either. This test exists so that nobody reads the redaction processor and
    concludes the pipeline is a safety net for exception text. It is a statement of what
    is **not** covered, in the same spirit as the residual documented in
    `tests/security/test_no_float.py`.
    """
    production_logging()
    sentinel = "sentinel-value-carried-inside-an-exception"

    try:
        message = f"failing row contained {sentinel}"
        raise ValueError(message)
    except ValueError:
        structlog.get_logger("test").exception("unhandled_exception", path="/api/wallets")

    written = capsys.readouterr().out

    assert "unhandled_exception" in written
    assert sentinel in written, (
        "the pipeline now redacts exception text; if that is deliberate, this test should "
        "be replaced by one asserting the redaction rather than deleted"
    )


def test_capture_logs_would_not_have_seen_that(
    capsys: pytest.CaptureFixture[str],
    production_logging: Callable[[], None],
) -> None:
    """Why this whole section reads stdout instead of `structlog.testing.capture_logs`.

    Pinned as a test rather than left in a comment, because the comment was there and the
    blind spot shipped anyway. `capture_logs` replaces the processor chain, so
    `format_exc_info` never runs: the captured entry carries `exc_info: True` and no
    rendered traceback at all. Every assertion written against it is therefore blind to
    anything an exception carries.

    If a future structlog renders the exception into the captured entry, this goes red --
    and at that point `capture_logs` becomes safe for this purpose again and the stdout
    plumbing above could be simplified. Until then, deleting this test would remove the
    only record of why the plumbing is there.
    """
    production_logging()
    sentinel = "sentinel-value-carried-inside-an-exception"

    with capture_logs() as entries:
        try:
            message = f"failing row contained {sentinel}"
            raise ValueError(message)
        except ValueError:
            structlog.get_logger("test").exception("unhandled_exception")

    assert entries, "capture_logs recorded nothing, so this comparison proves nothing"
    assert sentinel not in rendered(entries), (
        "capture_logs now renders exception text, so it is no longer blind here"
    )


@pytest.mark.parametrize("environment", ["prod", "dev"])
def test_the_configured_pipeline_redacts_an_address_it_is_handed(
    environment: Literal["dev", "prod"],
    capsys: pytest.CaptureFixture[str],
    restored_logging: None,
) -> None:
    """The processor is wired into the real pipeline, not merely importable.

    Driven through `structlog.get_logger` rather than by calling the processor, because
    what is being asserted is the *order* of the processors: redaction has to run before
    the renderer, and a pipeline with them the other way round would pass every unit test
    of the helper and still print the address.

    **The pipeline is configured here rather than assumed.** An earlier version of this
    test logged without configuring anything and asserted only that the address was
    absent from what `capsys` captured. Nothing was captured, so it passed -- and kept
    passing with `address` deleted from the redaction fragments, which is precisely the
    mutation it exists to catch. The three positive assertions below are what make a
    vacuous pass impossible: an empty capture now fails on the event name.
    """
    del restored_logging  # The fixture's value is its teardown.
    configure_logging(Settings(environment=environment, allowed_origin=PRODUCTION_ORIGIN))

    structlog.get_logger("test").warning(
        "wallet_registered",
        address=BIP173_TESTNET_P2WPKH,
        address_canonical=BIP173_TESTNET_P2WPKH,
        chain_key="bitcoin",
    )

    written = capsys.readouterr().out

    assert BIP173_TESTNET_P2WPKH not in written
    assert BIP173_TESTNET_P2WPKH[:20] not in written
    # The line really was written, and the two address fields really were the ones hidden.
    assert "wallet_registered" in written
    assert "bitcoin" in written
    assert written.count(REDACTED) == 2


# --------------------------------------------------------------------------------------
# Criterion 7, part three: nothing logs an address in the first place
# --------------------------------------------------------------------------------------

#: The modules that handle an address. A log call in one of these that bound an address
#: under a name the fragment list does not match would be redacted by nothing.
ADDRESS_HANDLING_MODULES: Final = (
    # Not a wallet module, and it is on this list because of a real leak. The unhandled
    # exception handler logs on a path every wallet request can reach, and the exception
    # it is handed is the one object in the process most likely to be carrying an address
    # -- a driver error quoting its bound parameters is exactly that. Binding any part of
    # it as a log field would put one in the log.
    SOURCE_ROOT / "api" / "errors.py",
    SOURCE_ROOT / "domain" / "addresses.py",
    SOURCE_ROOT / "domain" / "chains.py",
    SOURCE_ROOT / "repositories" / "wallets.py",
    SOURCE_ROOT / "services" / "wallets.py",
    SOURCE_ROOT / "api" / "routers" / "wallets.py",
    SOURCE_ROOT / "api" / "schemas" / "wallets.py",
    # #10. These are the first modules that handle an address *and* produce something an
    # endpoint serves. `services/balance_sync.py` is the one that matters: it catches three
    # kinds of failure from a provider that was just handed every address on a chain, and
    # the exception it is holding is the object in the process most likely to be carrying
    # one -- a `KeyError` raised while correlating a balance has an address for its `str()`.
    SOURCE_ROOT / "repositories" / "balances.py",
    SOURCE_ROOT / "repositories" / "sync_runs.py",
    SOURCE_ROOT / "services" / "balance_sync.py",
    SOURCE_ROOT / "services" / "balances.py",
    SOURCE_ROOT / "services" / "scheduler.py",
    SOURCE_ROOT / "api" / "routers" / "balances.py",
    SOURCE_ROOT / "api" / "schemas" / "balances.py",
)

#: Names that would carry an address into a log call's keyword arguments.
ADDRESS_BEARING_NAMES: Final = frozenset(
    {"address", "raw", "canonical", "display", "address_canonical", "address_display"}
)

LOGGING_METHODS: Final = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log", "msg"}
)


def log_call_arguments(path: Path) -> list[tuple[int, str]]:
    """Every `(line, argument source)` handed to something that looks like a log call.

    Deliberately shallow and deliberately noisy: it does not resolve what `_logger` is
    bound to. A false positive here is a conversation about a log line, which is cheap; a
    false negative is an address in a file somebody ships.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in LOGGING_METHODS:
            continue
        for argument in node.args:
            found.append((node.lineno, ast.unparse(argument)))
        for keyword in node.keywords:
            found.append((node.lineno, f"{keyword.arg}={ast.unparse(keyword.value)}"))
    return found


def test_no_wallet_module_passes_an_address_to_a_log_call() -> None:
    """Criterion 7's third leg: the code does not try, so redaction is never load bearing.

    Redaction is defence in depth. The control is that nothing in the wallet registry hands
    an address to a logger at all -- which is a property of the source, not of a request,
    and so is the only one of the three that also covers a path no test exercises.
    """
    offences: list[str] = []
    scanned = 0
    for path in ADDRESS_HANDLING_MODULES:
        if not path.is_file():
            continue
        scanned += 1
        for line, source in log_call_arguments(path):
            names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", source))
            if names & ADDRESS_BEARING_NAMES:
                offences.append(f"{path.name}:{line}: {source}")

    assert scanned == len(ADDRESS_HANDLING_MODULES), "a module in the list does not exist"
    assert offences == []


def test_the_log_call_scan_can_actually_fail(tmp_path: Path) -> None:
    """A walk that finds nothing passes the test above while checking nothing."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import structlog\n"
        "_logger = structlog.get_logger(__name__)\n"
        "def f(address: str) -> None:\n"
        "    _logger.info('wallet_created', wallet=address)\n",
        encoding="utf-8",
    )

    found = log_call_arguments(planted)

    assert [source for _line, source in found] == ["'wallet_created'", "wallet=address"]
    assert any(
        set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", source)) & ADDRESS_BEARING_NAMES
        for _line, source in found
    )


# --------------------------------------------------------------------------------------
# Criterion 8: every fixture in the suite is testnet
# --------------------------------------------------------------------------------------

#: The same shapes `.gitleaks.toml` rejects, so a fixture that would fail the secret scan
#: fails a test first -- with a file and a line, several minutes earlier, and without
#: having to have been committed.
MAINNET_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("bitcoin mainnet bech32", r"\bbc1[02-9ac-hj-np-z]{11,71}\b"),
    (
        "bitcoin mainnet base58",
        r"(?:^|[^A-Za-z0-9])([13][a-km-zA-HJ-NP-Z1-9]{25,34})(?:[^A-Za-z0-9]|$)",
    ),
    ("kaspa mainnet", r"\bkaspa:[qp][a-z0-9]{59,}\b"),
    ("extended public key", r"\b(?:xpub|ypub|zpub)[1-9A-HJ-NP-Za-km-z]{100,112}\b"),
)


def test_fixtures_contain_no_mainnet_address() -> None:
    """Criterion 8: rule 3, enforced over the test tree rather than trusted.

    The patterns are copied from `.gitleaks.toml` on purpose. The secret scan is the
    control and it runs over the whole history; this runs in the unit test suite, so a
    mainnet vector pasted into a fixture fails in seconds with a file and a line number
    instead of at the pre-push hook.
    """
    offences: list[str] = []
    scanned = 0
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        scanned += 1
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for name, pattern in MAINNET_PATTERNS:
                if re.search(pattern, line):
                    offences.append(f"{path.relative_to(TESTS_ROOT)}:{number}: {name}")

    assert scanned > 20, "the walk found almost no test files, so it proves nothing"
    assert offences == []


def test_the_mainnet_scan_can_actually_fail() -> None:
    """The patterns must match something, or the test above is a tautology.

    The strings below are deliberately **not** real addresses -- they are the shapes the
    rules match, assembled here so that no valid mainnet address has to exist in this file
    in order to prove the rules work.

    Assembled with `"".join` rather than with `+`, which is not fussiness. CPython folds
    a constant `+` at compile time, so `"bc1" + "q" * 38` becomes a single literal in the
    `.pyc` -- and gitleaks, run over a working tree that has one, then reports a mainnet
    bech32 address in the test whose whole purpose is to prove that rule works. A `join`
    is not folded, so the shape only ever exists at run time.
    """
    bech32_shaped = "".join(("bc1", "q" * 38))
    base58_shaped = "".join(("1", "A" * 30))
    kaspa_shaped = "".join(("kaspa:q", "a" * 60))
    xpub_shaped = "".join(("xpub", "1" * 105))

    matched = {
        name
        for name, pattern in MAINNET_PATTERNS
        for candidate in (bech32_shaped, base58_shaped, kaspa_shaped, xpub_shaped)
        if re.search(pattern, candidate)
    }

    assert matched == {name for name, _pattern in MAINNET_PATTERNS}


@pytest.mark.parametrize(
    "address",
    [BIP173_TESTNET_P2WPKH, KASPA_TESTNET_V1_KEY, NAMED_CORRUPTIONS[6][2]],
    ids=["tb1", "kaspatest", "base58 testnet"],
)
def test_the_mainnet_scan_does_not_flag_a_testnet_fixture(address: str) -> None:
    """The other half: the rules must leave the vectors this suite is required to use."""
    assert [name for name, pattern in MAINNET_PATTERNS if re.search(pattern, address)] == []
