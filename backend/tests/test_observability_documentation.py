"""Spec 030 (#23), criterion 14: `docs/operations.md` says what the logs and the health detail are.

Checked as substance, the way `tests/test_backup_documentation.py` checks its sections: the
wording is free to change, and what cannot change without failing a test here is

* **section 18, the logs, against the pipeline**: the keys a record carries are the keys the
  table lists, read off a real record; the stderr line shown is `HANDLER_ERROR_LINE`; the
  header is `X-Request-ID`; `request_completed`'s fields are the ones the middleware logs, and
  the route labels and documentation paths are the application's own; every key-name
  fragment, extended-key prefix, address prefix, the secret threshold, every credential and
  every vendor floor the code holds is named;
* **section 19, the health detail, against the service**: every state of every section has
  its row, in the order the code declares them; the late rule's two cases and its two
  intervals; the price limit; the `unavailable` sections and the event and fields the
  service logs for one; and the JSON example has exactly the fields the API serves.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

import pytest
import structlog
from pydantic import SecretStr
from structlog.testing import capture_logs

from portfolio.api.request_context import (
    REQUEST_COMPLETED_EVENT,
    REQUEST_ID_HEADER,
    SPA_ROUTE,
    UNMATCHED_ROUTE,
    RequestContextMiddleware,
    documentation_paths,
)
from portfolio.config import Settings
from portfolio.domain.exchanges import AccountSyncStatus
from portfolio.domain.health import (
    LATE_AFTER_INTERVALS,
    PriceHealthState,
    ReconciliationHealthState,
    SchedulerName,
    SchedulerState,
    SourceState,
)
from portfolio.domain.passwords import OWASP_MINIMUM_MEMORY_COST, OWASP_MINIMUM_TIME_COST
from portfolio.logging import (
    ADDRESS_PATTERNS,
    EXTENDED_KEY_PREFIXES,
    EXTENDED_PUBLIC_KEY_PREFIXES,
    HANDLER_ERROR_LINE,
    MIN_SUBSTRING_SECRET_LENGTH,
    REDACTED,
    REQUEST_ID_KEY,
    SENSITIVE_KEY_FRAGMENTS,
    SILENCED_VENDOR_LOGGERS,
    VENDOR_LOG_FLOOR,
    ValueRedactor,
    configure_logging,
)
from portfolio.services.health import SCHEDULER_ORDER, HealthSection
from portfolio.services.prices import STALE_AFTER
from tests.logging_harness import preserved_logging

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI
    from starlette.types import Message, Receive, Scope, Send

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
OPERATIONS_DOC: Final = REPO_ROOT / "docs" / "operations.md"
SERVICES: Final = REPO_ROOT / "backend" / "src" / "portfolio" / "services"
HEALTH_SERVICE_SOURCE: Final = SERVICES / "health.py"
SCHEDULER_SOURCE: Final = SERVICES / "scheduler.py"
#: The shortest address any rule takes: a bech32 one, its three-character prefix and 11 more.
SHORTEST_ADDRESS_LENGTH: Final = 14

SECTION_18: Final = "## 18. Logs: one line per record, one id per request, and what is redacted"
SECTION_19: Final = "## 19. The health detail: every source, and what to do about each"
REDACTED_HEADING: Final = "### What is redacted"
NOT_REDACTED_HEADING: Final = "### What is not redacted"
UNAVAILABLE_HEADING: Final = "### `unavailable`: a section that could not be read"
PRODUCTION_ORIGIN: Final = "https://portfolio.example"

#: How many of each credential the document counts: "the bootstrap password, the CoinGecko
#: key, the three Bitget variables and the two BingX ones".
DOCUMENTED_CREDENTIALS: Final = {"bootstrap": 1, "coingecko": 1, "bitget": 3, "bingx": 2}


def read(path: Path) -> str:
    assert path.is_file(), f"{path} does not exist"
    return path.read_text(encoding="utf-8")


def raw_section(text: str, heading: str) -> str:
    """One section, from its heading to the next heading of its level or above, as written."""
    assert text.count(heading + "\n") == 1, f"{heading!r} appears {text.count(heading)} times"
    start = text.index(heading + "\n")
    depth = len(heading.split(" ", 1)[0])
    following = re.search(rf"^#{{1,{depth}}} ", text[start + len(heading) :], re.MULTILINE)
    end = start + len(heading) + following.start() if following else len(text)
    return text[start:end]


def flat(text: str) -> str:
    """`text` on one line, so a phrase the document wraps is still found."""
    return " ".join(text.split())


def logged_keywords(source: Path, event: str) -> list[str | None]:
    """The keywords of the one call in `source` that logs `event`, in order, off its syntax tree."""
    [call] = [
        node
        for node in ast.walk(ast.parse(read(source)))
        if isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == event
    ]
    return [keyword.arg for keyword in call.keywords]


def named(text: str, before: str, after: str) -> list[str]:
    """The backticked names in the one stretch of `text` between `before` and `after`.

    Read from that stretch alone, so that a name dropped from a list is not found again in a
    later sentence that happens to mention it.
    """
    [stretch] = re.findall(f"{re.escape(before)}(.*?){re.escape(after)}", text)
    return re.findall(r"`([^`]+)`", stretch)


def table_rows(text: str, first_column: str) -> list[list[str]]:
    """The rows of the Markdown table whose header's first cell is `first_column`."""
    lines = text.splitlines()
    header = next(
        number
        for number, line in enumerate(lines)
        if line.startswith("|") and line.split("|")[1].strip() == first_column
    )
    rows = []
    for line in lines[header + 2 :]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def first_column(text: str, header: str) -> list[str]:
    """The first cell of every row, its backticks removed."""
    return [row[0].strip("`") for row in table_rows(text, header)]


@pytest.fixture(scope="module")
def logs() -> str:
    return raw_section(read(OPERATIONS_DOC), SECTION_18)


@pytest.fixture(scope="module")
def health() -> str:
    return raw_section(read(OPERATIONS_DOC), SECTION_19)


# --------------------------------------------------------------------------------------
# Section 18: what a record carries
# --------------------------------------------------------------------------------------


@pytest.fixture
def restored() -> Iterator[None]:
    with preserved_logging():
        structlog.contextvars.clear_contextvars()
        yield
        structlog.contextvars.clear_contextvars()


def test_the_keys_table_is_the_keys_a_record_carries(
    logs: str, restored: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Read off real records: a library's, during a request, with a traceback."""
    del restored
    configure_logging(
        Settings(
            _env_file=None,
            environment="prod",
            allowed_origin=PRODUCTION_ORIGIN,
            argon2_memory_cost=OWASP_MINIMUM_MEMORY_COST,
            argon2_time_cost=OWASP_MINIMUM_TIME_COST,
        )
    )
    capsys.readouterr()
    structlog.contextvars.bind_contextvars(**{REQUEST_ID_KEY: str(uuid4())})
    try:
        message = "a fault"
        raise RuntimeError(message)
    except RuntimeError:
        logging.getLogger("some.library").exception("a library's message")
    structlog.get_logger("portfolio.x").info("the application's event")

    library, application = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    documented = first_column(logs, "Key")

    assert set(library) == set(documented)
    assert set(application) == set(documented) - {"logger", "exception"}


def test_the_stderr_line_shown_is_the_handlers_own(logs: str) -> None:
    shown = HANDLER_ERROR_LINE.format(error_type="TypeError", logger="httpx").strip()

    assert f"\n{shown}\n" in logs


def test_the_header_and_the_id_are_named(logs: str) -> None:
    text = flat(logs)

    assert f"`{REQUEST_ID_HEADER}`" in text
    assert "ignored" in text
    assert "36 characters in five hyphenated groups" in text


# --------------------------------------------------------------------------------------
# Section 18: `request_completed`
# --------------------------------------------------------------------------------------


async def test_the_request_completed_fields_are_the_ones_the_middleware_logs(logs: str) -> None:
    async def answer(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        del message

    middleware = RequestContextMiddleware(answer, is_api_path=lambda path: path.startswith("/api"))
    with capture_logs() as records:
        await middleware({"type": "http", "method": "GET", "path": "/api/x"}, receive, send)
    [record] = [entry for entry in records if entry["event"] == REQUEST_COMPLETED_EVENT]
    logged = set(record) - {"event", "log_level", REQUEST_ID_KEY}

    assert (
        set(first_column(logs, "Field")) == logged == {"method", "route", "status", "duration_ms"}
    )


def test_the_route_labels_and_documentation_paths_are_the_applications(
    logs: str, app: FastAPI
) -> None:
    text = flat(logs)

    for path in documentation_paths(app):
        assert f"`{path}`" in text, path
    assert f"**`{SPA_ROUTE}`**" in text
    assert f"**`{UNMATCHED_ROUTE}`**" in text
    assert "Never the raw path and never the query string." in text


# --------------------------------------------------------------------------------------
# Section 18: what is redacted, and the second layer
# --------------------------------------------------------------------------------------


def test_every_key_fragment_and_key_prefix_is_named(logs: str) -> None:
    by_key = flat(raw_section(logs, REDACTED_HEADING)).split("**By value**")[0]

    for fragment in SENSITIVE_KEY_FRAGMENTS:
        assert f"`{fragment}`" in by_key, fragment
    for prefix in EXTENDED_KEY_PREFIXES:
        assert f"`{prefix}`" in by_key, prefix


def test_every_value_rule_is_named_with_its_numbers(logs: str) -> None:
    by_value = flat(raw_section(logs, REDACTED_HEADING)).split("**By value**")[1]

    for prefix in EXTENDED_PUBLIC_KEY_PREFIXES:
        assert f"`{prefix}`" in by_value, prefix
    assert "100 or more Base58 characters" in by_value
    assert f"{MIN_SUBSTRING_SECRET_LENGTH} characters or longer" in by_value
    for start in ("`bc1`", "`tb1`", "`bcrt1`", "`1`", "`3`", "`m`", "`n`", "`2`"):
        assert start in by_value, start
    kaspa = re.search(r"\(\?:([a-z|]+)\):", ADDRESS_PATTERNS[2].pattern)
    assert kaspa is not None
    for prefix in kaspa.group(1).split("|"):
        assert f"`{prefix}:`" in by_value, prefix


def test_the_credentials_counted_are_every_secret_setting() -> None:
    """The document counts seven credentials by vendor; the model holds exactly those."""
    secrets = [
        name
        for name, field in Settings.model_fields.items()
        if field.annotation is not None and "SecretStr" in str(field.annotation)
    ]
    counted = {
        vendor: sum(1 for name in secrets if name.startswith(vendor))
        for vendor in DOCUMENTED_CREDENTIALS
    }

    assert counted == DOCUMENTED_CREDENTIALS
    assert len(secrets) == sum(DOCUMENTED_CREDENTIALS.values())
    assert SecretStr.__name__ == "SecretStr"


def test_the_second_layer_names_every_floored_vendor(logs: str) -> None:
    floor = f"are held at `{logging.getLevelName(VENDOR_LOG_FLOOR)}`"

    assert set(named(flat(logs), "**The second layer.**", floor)) == set(SILENCED_VENDOR_LOGGERS)


def test_the_residuals_are_documented(logs: str) -> None:
    text = flat(raw_section(logs, NOT_REDACTED_HEADING))

    assert "`x<address>`" in text
    assert "`wallet_<address>` is redacted" in text
    assert "Parts of two addresses joined with nothing between them" in text
    assert "percent-encoded, base64" in text


def test_the_request_id_lengths_stated_are_the_rules(logs: str) -> None:
    """ "Its longest run ... is 12, and the shortest address a rule recognises is 14": the
    shortest is a bech32 one, `tb1` and eleven data characters, and one fewer is not taken."""
    redactor = ValueRedactor()
    shortest = "tb1" + "q" * (SHORTEST_ADDRESS_LENGTH - len("tb1"))

    assert redactor.redact_text(shortest) == REDACTED
    assert redactor.redact_text(shortest[:-1]) == shortest[:-1]
    assert "of characters without a hyphen is 12" in flat(logs)
    assert f"the shortest address a rule recognises is {SHORTEST_ADDRESS_LENGTH}" in flat(logs)


# --------------------------------------------------------------------------------------
# Section 19: every state of every section
# --------------------------------------------------------------------------------------


def test_every_timer_state_has_a_row_in_order(health: str) -> None:
    assert first_column(health, "`state`")[:4] == [state.value for state in SchedulerState]


def test_the_late_row_states_both_cases_and_two_intervals(health: str) -> None:
    [late] = [row for row in table_rows(health, "`state`") if row[0] == "`late`"]
    meaning = flat(late[1])

    assert LATE_AFTER_INTERVALS == 2
    assert "A tick is in flight, and it started more than two intervals ago" in meaning
    assert "no tick is in flight, and the last one finished more than two intervals ago" in meaning
    assert "before the first tick, the timer started more than two intervals ago" in meaning


def test_the_tick_failure_line_named_is_the_timers(health: str) -> None:
    text = flat(health)

    assert "scheduler" in logged_keywords(SCHEDULER_SOURCE, "scheduler_tick_failed")
    assert "the log has `scheduler_tick_failed` with `scheduler` and the traceback" in text


@pytest.mark.parametrize(
    ("heading", "header", "states"),
    [
        ("### `chains`: the balance sync per chain", "`state`", list(SourceState)),
        ("### `exchanges`: one entry per account", "`balances_state`", list(SourceState)),
        ("### `prices`", "`state`", [s for s in PriceHealthState if s.value != "unavailable"]),
        (
            "### `reconciliation`: the holdings check, in short",
            "`state`",
            [s for s in ReconciliationHealthState if s.value != "unavailable"],
        ),
    ],
    ids=["chains", "exchanges", "prices", "reconciliation"],
)
def test_every_section_state_has_a_row_in_order(
    health: str, heading: str, header: str, states: list[Any]
) -> None:
    assert first_column(raw_section(health, heading), header) == [state.value for state in states]


def test_the_sync_states_named_are_the_accounts(health: str) -> None:
    text = flat(raw_section(health, "### `exchanges`: one entry per account"))
    listed = named(text, "`sync_state` is the fill sync's status --", ", as section 13")

    assert set(listed) == {status.value for status in AccountSyncStatus}


def test_the_price_limit_is_the_dashboards(health: str) -> None:
    assert timedelta(hours=1) == STALE_AFTER
    assert "at most an hour old" in flat(raw_section(health, "### `prices`"))


def test_the_unavailable_sections_and_their_log_line_are_the_services(health: str) -> None:
    """The sections that can be `unavailable`, and `health_section_failed`'s keywords, read
    off the service's own source."""
    text = flat(raw_section(health, UNAVAILABLE_HEADING))
    sections = named(text, UNAVAILABLE_HEADING, "can each be `unavailable`")
    keywords = logged_keywords(HEALTH_SERVICE_SOURCE, "health_section_failed")

    assert sections == [section.value for section in HealthSection]
    assert keywords == ["section", "error_type"]
    assert "The log has `health_section_failed` with `section` and `error_type`," in text


def test_the_json_example_has_exactly_the_fields_served(health: str, app: FastAPI) -> None:
    [block] = re.findall(r"```json\n(.*?)\n```", health, re.DOTALL)
    example = json.loads(block)
    schemas = app.openapi()["components"]["schemas"]

    def fields(name: str) -> set[str]:
        return set(schemas[name]["properties"])

    assert set(example) == fields("HealthDetailResponse") - {"backup"}
    assert [timer["name"] for timer in example["schedulers"]] == [
        name.value for name in SCHEDULER_ORDER
    ]
    assert {name.value for name in SCHEDULER_ORDER} == {name.value for name in SchedulerName}
    for timer in example["schedulers"]:
        assert set(timer) == fields("SchedulerStatusResponse")
    assert set(example["chains"]) == fields("ChainsHealthResponse")
    for chain in example["chains"]["items"]:
        assert set(chain) == fields("ChainHealthResponse")
    assert set(example["exchanges"]) == fields("ExchangesHealthResponse")
    for account in example["exchanges"]["items"]:
        assert set(account) == fields("ExchangeHealthResponse")
    assert set(example["prices"]) == fields("PricesHealthResponse")
    assert set(example["reconciliation"]) == fields("ReconciliationHealthResponse")
