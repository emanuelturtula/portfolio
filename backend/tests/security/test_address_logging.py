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
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import pytest
import structlog
from structlog.testing import capture_logs

from portfolio.config import Settings
from portfolio.logging import (
    REDACTED,
    SENSITIVE_KEY_FRAGMENTS,
    configure_logging,
    is_sensitive_key,
    redact_sensitive,
)
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    BIP173_TESTNET_P2WPKH_UPPERCASE,
    KASPA_TESTNET_V1_KEY,
    NAMED_CORRUPTIONS,
)
from tests.auth.conftest import JSON_HEADERS

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from httpx import AsyncClient

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
# Criterion 7, part two: a real request, and every record it produced
# --------------------------------------------------------------------------------------


def rendered(entries: Sequence[Mapping[str, Any]]) -> str:
    """Every captured record as one string, so a search cannot miss a nested value."""
    return json.dumps(entries, default=repr)


async def test_no_log_event_contains_an_address(signed_in_api_client: AsyncClient) -> None:
    """Criterion 7, against the running code rather than against the helper.

    Every request the registry serves is driven here -- create, list, patch, archive, and
    the three failure paths -- with every log record structlog emits captured. The
    assertion is on the *rendered* records, so an address is caught whether it was bound
    as a field, formatted into an event name, or carried inside an exception.

    A test that only checked the redaction helper would pass while the service logged the
    address under `wallet=` or `target=`. This is the test that would not.
    """
    corrupted = NAMED_CORRUPTIONS[0][2]

    with capture_logs() as entries:
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
        # The failure paths, which are where an error message is most likely to quote the
        # input it refused.
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
        await signed_in_api_client.delete(f"{WALLETS}/{wallet_id}", headers=JSON_HEADERS)

    assert entries, "nothing was logged at all, so this test proves nothing"
    written = rendered(entries)

    for forbidden in (
        BIP173_TESTNET_P2WPKH,
        BIP173_TESTNET_P2WPKH_UPPERCASE,
        BIP173_TESTNET_P2WPKH.lower(),
        corrupted,
    ):
        assert forbidden not in written
        # A prefix is as good as the whole address to whoever reads the log.
        assert forbidden[:20] not in written
        assert forbidden[-20:] not in written


async def test_the_log_capture_really_captures(signed_in_api_client: AsyncClient) -> None:
    """The guard on the test above: an empty capture would pass it without checking.

    `test_no_log_event_contains_an_address` already asserts the capture is non-empty, but
    "non-empty" could be one unrelated record. This proves the request path being driven
    is one that logs, by making it log a refusal on purpose.
    """
    with capture_logs() as entries:
        response = await signed_in_api_client.delete(f"{WALLETS}/424242", headers=JSON_HEADERS)

    assert response.status_code == 404
    assert any(entry.get("event") == "request_failed" for entry in entries), entries


async def test_an_address_is_absent_from_a_log_of_an_unauthenticated_attempt(
    api_client: AsyncClient,
) -> None:
    """The middleware logs every refusal, and a refused request still carries a body."""
    with capture_logs() as entries:
        response = await api_client.post(
            WALLETS,
            json={"chain_key": "bitcoin", "address": KASPA_TESTNET_V1_KEY},
            headers=JSON_HEADERS,
        )

    assert response.status_code == 401
    assert entries
    assert KASPA_TESTNET_V1_KEY not in rendered(entries)


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """Undo the global logging configuration the test below installs."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    try:
        yield
    finally:
        structlog.reset_defaults()
        root.handlers[:] = handlers
        root.setLevel(level)


#: A fictional origin, never a real hostname (rule 3). `Settings` refuses to build with
#: `environment="prod"` while `allowed_origin` is still the development default.
PRODUCTION_ORIGIN: Final = "https://portfolio.example"


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
    SOURCE_ROOT / "domain" / "addresses.py",
    SOURCE_ROOT / "domain" / "chains.py",
    SOURCE_ROOT / "repositories" / "wallets.py",
    SOURCE_ROOT / "services" / "wallets.py",
    SOURCE_ROOT / "api" / "routers" / "wallets.py",
    SOURCE_ROOT / "api" / "schemas" / "wallets.py",
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
