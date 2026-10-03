"""Spec 030 (#23), criteria 1 to 4 and 6: one pipeline for every record, read off stdout.

`configure_logging` is installed for real -- inside the test, after pytest has swapped
`sys.stdout`, through the `production_logging` installer -- and records are written the two
ways the application and its libraries write them: through structlog, and through a
standard-library logger. What is pinned:

* **a standard-library record is JSON on stdout** with its level, its logger's name, a
  timestamp and the bound `request_id`, and the value rule has run on its message, its
  arguments and its traceback (criteria 2 and 3);
* **a structlog record reaches every handler already redacted** (R7): stdout, a witness on
  the root logger and pytest's `caplog` alike, its exception a redacted string;
* **a full URL with a signature never reaches stdout** through either kind of record, with
  the vendor floor lifted (criterion 4);
* **uvicorn's loggers are routed to the root handler** and its access line is dropped, both
  as uvicorn configures them and from a real server answering a real socket (criteria 3, 6);
* **a record that cannot be written** is reported on stderr as one fixed line, never with
  its message or its arguments (R13, S3).

R10 is the accepted residual: a standard-library record reaches a handler other than the
root's unredacted. So the witness is asserted on for structlog records only, and stdout is the
side pinned for a standard-library one.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import logging.config
from typing import TYPE_CHECKING, Any, Final, override

import httpx
import pytest
import structlog
import uvicorn
from pydantic import SecretStr

from portfolio.config import Settings, get_settings
from portfolio.domain.passwords import OWASP_MINIMUM_MEMORY_COST, OWASP_MINIMUM_TIME_COST
from portfolio.logging import (
    HANDLER_ERROR_LINE,
    REDACTED,
    REQUEST_ID_KEY,
    SILENCED_VENDOR_LOGGERS,
    UVICORN_ACCESS_FLOOR,
    UVICORN_ACCESS_LOGGER,
    UVICORN_LOGGERS,
    VENDOR_LOG_FLOOR,
    SafeStreamHandler,
    configure_logging,
)
from tests.address_vectors import (
    BIP173_TESTNET_P2WPKH,
    KASPA_TESTNET_V0,
    SYNTHETIC_TPUB,
)
from tests.auth.conftest import apply_auth_environment
from tests.security.conftest import (
    PRODUCTION_ORIGIN,
    EveryRecord,
    assert_absent,
    assert_carried_something,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: Synthetic, distinctive, and none of them an address: what a secret looks like to the rule.
KEY_SENTINEL: Final = "sentinel-coingecko-0f3a9c"
OTHER_SENTINEL: Final = "sentinel-bitget-secret-77e1"
SIGNATURE: Final = "sentinel-signature-5d2b"
SIGNED_URL: Final = f"https://api.example.test/v2/spot/fills?apiKey=k&signature={SIGNATURE}"
REQUEST_ID: Final = "0b6f3a52-7c1e-4d2a-9f8e-2a4c6e8b0d1f"


def json_lines(written: str) -> list[dict[str, Any]]:
    """Every line of stdout, each of which must be one JSON object in production."""
    lines = [line for line in written.splitlines() if line.strip()]
    parsed: list[dict[str, Any]] = []
    for line in lines:
        value = json.loads(line)
        assert isinstance(value, dict), line
        parsed.append(value)
    return parsed


def with_secrets(log_level: str = "INFO", **secrets: str) -> Settings:
    return Settings(
        _env_file=None,
        environment="prod",
        allowed_origin=PRODUCTION_ORIGIN,
        log_level=log_level,
        argon2_memory_cost=OWASP_MINIMUM_MEMORY_COST,
        argon2_time_cost=OWASP_MINIMUM_TIME_COST,
        **{name: SecretStr(value) for name, value in secrets.items()},  # type: ignore[arg-type]
    )


@pytest.fixture
def unbound() -> Iterator[None]:
    """No context variable leaks into or out of a test that binds one."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def witness(restored_logging: None) -> Iterator[EveryRecord]:
    """A root handler of the test's own, added after the pipeline is installed by the test."""
    del restored_logging
    handler = EveryRecord()
    yield handler
    logging.getLogger().removeHandler(handler)


def install(settings: Settings, *extra: logging.Handler) -> None:
    """The pipeline, then any handler beside the root's: `basicConfig(force=True)` drops them."""
    configure_logging(settings)
    for handler in extra:
        logging.getLogger().addHandler(handler)


# --------------------------------------------------------------------------------------
# A standard-library record: JSON, its context, and the value rule on stdout
# --------------------------------------------------------------------------------------


def test_a_standard_library_record_is_one_json_line_with_its_context(
    capsys: pytest.CaptureFixture[str], restored_logging: None, unbound: None
) -> None:
    del restored_logging, unbound
    install(with_secrets())
    structlog.contextvars.bind_contextvars(**{REQUEST_ID_KEY: REQUEST_ID})

    logging.getLogger("some.library").warning("a library said %s", "hello")

    [line] = json_lines(capsys.readouterr().out)
    assert line["event"] == "a library said hello"
    assert line["level"] == "warning"
    assert line["logger"] == "some.library"
    assert line[REQUEST_ID_KEY] == REQUEST_ID
    assert line["timestamp"].endswith("Z")


def test_a_standard_library_records_message_and_arguments_are_redacted_on_stdout(
    capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    del restored_logging
    install(with_secrets(coingecko_api_key=KEY_SENTINEL))

    logging.getLogger("some.library").warning(
        "read %s with %s from %s via %s",
        BIP173_TESTNET_P2WPKH,
        KEY_SENTINEL,
        SYNTHETIC_TPUB,
        SIGNED_URL,
    )

    written = capsys.readouterr().out
    assert_carried_something(written, marker="read [REDACTED] with [REDACTED] from [REDACTED]")
    assert_absent(written, BIP173_TESTNET_P2WPKH, SYNTHETIC_TPUB)
    assert KEY_SENTINEL not in written
    assert SIGNATURE not in written
    assert "https://api.example.test/v2/spot/fills?[REDACTED]" in written


def test_a_standard_library_records_traceback_is_redacted_on_stdout(
    capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    del restored_logging
    install(with_secrets(coingecko_api_key=KEY_SENTINEL))

    try:
        message = f"{KASPA_TESTNET_V0} refused {KEY_SENTINEL} at {SIGNED_URL}"
        raise RuntimeError(message)
    except RuntimeError:
        logging.getLogger("some.library").exception("a library failed")

    written = capsys.readouterr().out
    [line] = json_lines(written)
    assert "Traceback" in line["exception"]
    assert "RuntimeError: [REDACTED] refused [REDACTED] at" in line["exception"]
    assert_absent(written, KASPA_TESTNET_V0)
    assert KEY_SENTINEL not in written
    assert SIGNATURE not in written


#: A synthetic secret that ends in the marker's last two characters (R15).
MARKER_TAIL: Final = "D]-tail0"


def test_a_standard_library_record_is_redacted_until_it_stops_changing(
    capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    """R12 and R15, through the root formatter, which redacts a standard-library record.

    The address becomes `[REDACTED]`, which with `-tail0` after it holds the secret: one pass
    printed it, and the repetition replaces it.
    """
    del restored_logging
    install(with_secrets(coingecko_api_key=MARKER_TAIL))

    logging.getLogger("some.library").warning("%s-tail0", BIP173_TESTNET_P2WPKH)

    written = capsys.readouterr().out
    [line] = json_lines(written)
    assert line["event"] == REDACTED.removesuffix("D]") + REDACTED
    assert MARKER_TAIL not in written
    assert_absent(written, BIP173_TESTNET_P2WPKH)


# --------------------------------------------------------------------------------------
# S3 (R13): a record that cannot be written is reported without its content
# --------------------------------------------------------------------------------------


def root_handler() -> SafeStreamHandler:
    """The handler `configure_logging` installs on the root logger, and the only one."""
    [handler] = logging.getLogger().handlers
    assert type(handler) is SafeStreamHandler
    return handler


def test_the_root_handler_is_the_safe_one() -> None:
    """Its report is the one fixed line, naming the exception's type and the logger."""
    assert issubclass(SafeStreamHandler, logging.StreamHandler)
    assert HANDLER_ERROR_LINE.format(error_type="TypeError", logger="some.library") == (
        "--- Logging error: TypeError in a record from logger some.library; "
        "the record was not written ---\n"
    )


def test_a_record_that_cannot_be_formatted_writes_one_line_to_stderr_and_nothing_of_it(
    capfd: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    """S3: the standard library's `handleError` prints the record's raw message and its
    arguments to stderr, past every rule. `%d` given a string makes the formatting raise, on a
    record that carries a secret in its message and an address in its arguments."""
    del restored_logging
    install(with_secrets(coingecko_api_key=KEY_SENTINEL))
    root_handler()

    logging.getLogger("some.library").warning(
        "balance %d for " + KEY_SENTINEL, BIP173_TESTNET_P2WPKH
    )

    out, err = capfd.readouterr()
    assert err == HANDLER_ERROR_LINE.format(error_type="TypeError", logger="some.library")
    assert out == ""
    assert KEY_SENTINEL not in err
    assert_absent(err, BIP173_TESTNET_P2WPKH)


def test_a_structlog_record_that_cannot_be_written_is_reported_the_same_way(
    capfd: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    """The same handler, whichever kind of record: a value whose rendering raises."""
    del restored_logging
    install(with_secrets(coingecko_api_key=KEY_SENTINEL))
    handler = root_handler()
    handler.setStream(Unwritable())

    structlog.get_logger("some.module").warning(
        "unwritable", note=f"{KEY_SENTINEL} {BIP173_TESTNET_P2WPKH}"
    )

    _out, err = capfd.readouterr()
    assert err == HANDLER_ERROR_LINE.format(error_type="OSError", logger="some.module")
    assert KEY_SENTINEL not in err
    assert_absent(err, BIP173_TESTNET_P2WPKH)


def test_with_raise_exceptions_off_a_record_that_cannot_be_written_is_silent(
    capfd: pytest.CaptureFixture[str], restored_logging: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`logging.raiseExceptions` is honoured as the standard library honours it."""
    del restored_logging
    install(with_secrets(coingecko_api_key=KEY_SENTINEL))
    root_handler()
    monkeypatch.setattr(logging, "raiseExceptions", False)

    logging.getLogger("some.library").warning("balance %d", BIP173_TESTNET_P2WPKH)

    assert capfd.readouterr() == ("", "")


class Unwritable(io.StringIO):
    """A stream whose every write fails, as a closed pipe's does."""

    @override
    def write(self, text: str, /) -> int:
        del text
        message = "the stream is gone"
        raise OSError(message)


def test_a_standard_library_record_carries_the_request_id_only_while_one_is_bound(
    capsys: pytest.CaptureFixture[str], restored_logging: None, unbound: None
) -> None:
    del restored_logging, unbound
    install(with_secrets())

    logging.getLogger("some.library").warning("outside")
    structlog.contextvars.bind_contextvars(**{REQUEST_ID_KEY: REQUEST_ID})
    logging.getLogger("some.library").warning("inside")

    outside, inside = json_lines(capsys.readouterr().out)
    assert REQUEST_ID_KEY not in outside
    assert inside[REQUEST_ID_KEY] == REQUEST_ID


def test_the_development_renderer_is_redacted_as_well(
    capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    del restored_logging
    install(Settings(_env_file=None, environment="dev", coingecko_api_key=SecretStr(KEY_SENTINEL)))

    structlog.get_logger("t").warning("dev_line", note=f"{BIP173_TESTNET_P2WPKH} {KEY_SENTINEL}")
    logging.getLogger("some.library").warning("%s %s", SIGNED_URL, SYNTHETIC_TPUB)

    written = capsys.readouterr().out
    assert_carried_something(written, marker="dev_line")
    assert "some.library" in written
    assert_absent(written, BIP173_TESTNET_P2WPKH, SYNTHETIC_TPUB)
    assert KEY_SENTINEL not in written
    assert SIGNATURE not in written
    assert not written.lstrip().startswith("{")


def test_a_second_configuration_redacts_its_own_secrets(
    capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    """The set is built on every call: a redactor built once would miss the second's."""
    del restored_logging
    install(with_secrets(coingecko_api_key=KEY_SENTINEL))
    install(
        with_secrets(
            bitget_api_secret=OTHER_SENTINEL,
            bitget_api_key="k" * 12,
            bitget_api_passphrase="p" * 12,
        )
    )

    structlog.get_logger("t").warning("second", value=OTHER_SENTINEL)
    logging.getLogger("some.library").warning("%s", OTHER_SENTINEL)

    written = capsys.readouterr().out
    assert_carried_something(written, marker="second")
    assert OTHER_SENTINEL not in written
    assert written.count(REDACTED) == 2


def test_a_record_below_the_configured_level_is_not_written(
    capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    del restored_logging
    install(with_secrets(log_level="INFO"))

    structlog.get_logger("t").debug("structlog_debug")
    logging.getLogger("some.library").debug("library debug")
    structlog.get_logger("t").info("structlog_info")

    [line] = json_lines(capsys.readouterr().out)
    assert line["event"] == "structlog_info"


# --------------------------------------------------------------------------------------
# A structlog record: redacted before any handler sees it (R7)
# --------------------------------------------------------------------------------------


def test_a_structlog_record_reaches_every_handler_already_redacted(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    witness: EveryRecord,
) -> None:
    install(with_secrets(coingecko_api_key=KEY_SENTINEL), witness, caplog.handler)

    structlog.get_logger("t").warning(
        f"event_with {BIP173_TESTNET_P2WPKH}",
        note=KEY_SENTINEL,
        nested={"deep": [SYNTHETIC_TPUB, SIGNED_URL]},
    )

    written = capsys.readouterr().out
    assert_carried_something(written, marker="event_with [REDACTED]")
    seen = [*witness.rendered, *(repr(record.__dict__) for record in caplog.records)]
    assert len(witness.rendered) == 1
    assert len(caplog.records) == 1
    for text in (written, *seen):
        assert_absent(text, BIP173_TESTNET_P2WPKH, SYNTHETIC_TPUB)
        assert KEY_SENTINEL not in text
        assert SIGNATURE not in text
    message = caplog.records[0].msg
    assert isinstance(message, dict)
    assert message["note"] == REDACTED
    assert message["nested"] == {
        "deep": [REDACTED, "https://api.example.test/v2/spot/fills?[REDACTED]"]
    }


def test_a_structlog_exception_reaches_every_handler_as_a_redacted_string(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    witness: EveryRecord,
) -> None:
    """Rendered in structlog's chain: no handler receives `exc_info`, so none can format it."""
    install(with_secrets(coingecko_api_key=KEY_SENTINEL), witness, caplog.handler)

    try:
        message = f"{BIP173_TESTNET_P2WPKH} refused {KEY_SENTINEL} at {SIGNED_URL}"
        raise RuntimeError(message)
    except RuntimeError:
        structlog.get_logger("t").exception("structlog_failure")

    written = capsys.readouterr().out
    [line] = json_lines(written)
    assert "RuntimeError: [REDACTED] refused [REDACTED] at" in line["exception"]
    [record] = caplog.records
    assert record.exc_info is None
    assert isinstance(record.msg, dict)
    assert "Traceback" in record.msg["exception"]
    for text in (written, *witness.rendered, repr(record.__dict__)):
        assert_absent(text, BIP173_TESTNET_P2WPKH)
        assert KEY_SENTINEL not in text
        assert SIGNATURE not in text


# --------------------------------------------------------------------------------------
# Criterion 4: a signed URL, with the vendor floor lifted
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("vendor", SILENCED_VENDOR_LOGGERS)
def test_a_signed_url_never_reaches_stdout_from_a_vendor_with_its_floor_lifted(
    vendor: str, capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    del restored_logging
    install(with_secrets(log_level="DEBUG"))
    logging.getLogger(vendor).setLevel(logging.DEBUG)

    logging.getLogger(vendor).info('HTTP Request: GET %s "HTTP/1.1 200 OK"', SIGNED_URL)

    written = capsys.readouterr().out
    assert_carried_something(written, marker="HTTP Request: GET https://api.example.test")
    assert SIGNATURE not in written
    assert "apiKey" not in written


def test_a_signed_url_never_reaches_stdout_from_structlog(
    capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    del restored_logging
    install(with_secrets(log_level="DEBUG"))

    structlog.get_logger("t").debug("provider_request", target=SIGNED_URL)

    written = capsys.readouterr().out
    assert_carried_something(written, marker="provider_request")
    assert SIGNATURE not in written
    assert "apiKey" not in written


def test_the_vendor_floors_stay_as_the_second_layer(restored_logging: None) -> None:
    del restored_logging
    install(with_secrets(log_level="DEBUG"))

    assert {
        name: logging.getLogger(name).level for name in SILENCED_VENDOR_LOGGERS
    } == dict.fromkeys(SILENCED_VENDOR_LOGGERS, VENDOR_LOG_FLOOR)
    assert VENDOR_LOG_FLOOR == logging.WARNING


# --------------------------------------------------------------------------------------
# Uvicorn's loggers
# --------------------------------------------------------------------------------------


def configure_as_uvicorn_does() -> None:
    """What uvicorn does before it imports the application: handlers, no propagation."""
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)


def test_uvicorns_loggers_are_handed_to_the_root_handler(restored_logging: None) -> None:
    del restored_logging
    configure_as_uvicorn_does()
    assert all(logging.getLogger(name).handlers for name in ("uvicorn", "uvicorn.access"))
    assert not logging.getLogger("uvicorn.access").propagate

    install(with_secrets())

    for name in UVICORN_LOGGERS:
        assert logging.getLogger(name).handlers == [], name
        assert logging.getLogger(name).propagate is True, name
    assert UVICORN_LOGGERS == ("uvicorn", "uvicorn.error", "uvicorn.access")
    assert UVICORN_ACCESS_LOGGER == "uvicorn.access"
    assert UVICORN_ACCESS_FLOOR == logging.WARNING
    assert logging.getLogger("uvicorn.access").level == logging.WARNING
    assert logging.getLogger("uvicorn.error").getEffectiveLevel() == logging.INFO


def test_uvicorns_error_logger_is_json_and_its_access_line_is_dropped(
    capsys: pytest.CaptureFixture[str], restored_logging: None
) -> None:
    del restored_logging
    configure_as_uvicorn_does()
    install(with_secrets())

    logging.getLogger("uvicorn.error").info("Started server process [%d]", 4242)
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:5000",
        "GET",
        f"/api/x?apiKey={KEY_SENTINEL}",
        "1.1",
        200,
    )
    logging.getLogger("uvicorn.access").warning("an access warning %s", SIGNED_URL)

    written = capsys.readouterr().out
    started, warned = json_lines(written)
    assert started["logger"] == "uvicorn.error"
    assert started["event"] == "Started server process [4242]"
    assert warned["logger"] == "uvicorn.access"
    assert KEY_SENTINEL not in written
    assert SIGNATURE not in written


async def test_a_real_uvicorn_server_writes_request_completed_and_no_access_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restored_logging: None,
) -> None:
    """Criterion 6, end to end: uvicorn configures its loggers, then builds the app.

    `--factory`, as the Dockerfile runs it, on a socket of the operating system's choosing.
    An unauthenticated request carrying a query is refused, and stdout has the request's
    `request_completed` line and no access line, no raw path and no query.
    """
    del restored_logging
    apply_auth_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("PORTFOLIO_ENVIRONMENT", "prod")
    monkeypatch.setenv("PORTFOLIO_ALLOWED_ORIGIN", PRODUCTION_ORIGIN)
    monkeypatch.setenv("PORTFOLIO_ARGON2_MEMORY_COST", str(OWASP_MINIMUM_MEMORY_COST))
    monkeypatch.setenv("PORTFOLIO_ARGON2_TIME_COST", str(OWASP_MINIMUM_TIME_COST))
    monkeypatch.setenv("PORTFOLIO_COINGECKO_API_KEY", KEY_SENTINEL)
    get_settings.cache_clear()
    config = uvicorn.Config(
        "portfolio.main:create_app", factory=True, host="127.0.0.1", port=0, lifespan="off"
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:  # noqa: ASYNC110 - uvicorn exposes a flag only
                await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            response = await client.get(f"/api/wallets?apiKey={KEY_SENTINEL}&signature={SIGNATURE}")
    finally:
        server.should_exit = True
        async with asyncio.timeout(10):
            await serving
        get_settings.cache_clear()

    assert response.status_code == 401
    written = capsys.readouterr().out
    lines = json_lines(written)
    completed = [line for line in lines if line["event"] == "request_completed"]
    assert completed == [
        {
            "event": "request_completed",
            "method": "GET",
            "route": "unmatched",
            "status": 401,
            "duration_ms": completed[0]["duration_ms"],
            "level": "info",
            REQUEST_ID_KEY: response.headers["X-Request-ID"],
            "timestamp": completed[0]["timestamp"],
        }
    ]
    assert any(line.get("logger") == "uvicorn.error" for line in lines)
    assert not any(line.get("logger") == "uvicorn.access" for line in lines)
    assert "HTTP/1.1" not in written
    # The session guard's own `request_refused` names the path, as it did before #23; no line
    # carries it with its query, and no line is uvicorn's `"GET /path"` access format.
    assert "/api/wallets?" not in written
    assert '"GET /api/wallets' not in written
    assert KEY_SENTINEL not in written
    assert SIGNATURE not in written
