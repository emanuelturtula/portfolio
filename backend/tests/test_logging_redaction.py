"""Security test: a credential must never survive the logging pipeline.

The values below are invented. They exist only so the assertions can prove the exact
string that entered the log never appears in what was written out.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal

import pytest
import structlog

from portfolio.config import Settings
from portfolio.logging import REDACTED, configure_logging, is_sensitive_key, redact_sensitive

if TYPE_CHECKING:
    from collections.abc import Iterator

# A canary: invented, unique, and asserted absent from every rendered line.
CANARY_VALUE = "canary-value-that-must-never-be-logged"


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Undo the global logging configuration these tests install."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    try:
        yield
    finally:
        structlog.reset_defaults()
        root.handlers[:] = handlers
        root.setLevel(level)


def redact(event: dict[str, Any]) -> dict[str, Any]:
    """Run one event through the processor exactly as structlog would."""
    return dict(redact_sensitive(None, "info", event))


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "API_KEY",
        "Api-Key",
        "x-api-key",
        "apikey",
        "bitget_apiKey",
        "api_secret",
        "SECRET",
        "client_secret",
        "passphrase",
        "PASSPHRASE",
        "password",
        "user_password",
        "token",
        "access_token",
        "refresh_Token",
        "authorization",
        "Authorization",
        "signature",
        "SIGNATURE",
        "request_signature",
        "xpub",
        "xpub_account_0",
        "YPUB",
        "ypubKey",
        "zpub_main",
    ],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    assert is_sensitive_key(key)
    assert redact({key: CANARY_VALUE}) == {key: REDACTED}


@pytest.mark.parametrize(
    "key",
    # `public_address_count` used to be on this list. It is not any more: #5 added
    # `address` to the fragments, the match is a substring, and so a key with `address`
    # in its name is now redacted whatever else it says. That breadth is deliberate --
    # see `SENSITIVE_KEY_FRAGMENTS` and `tests/security/test_address_logging.py` -- and
    # the honest replacement is a key that makes the same point without the collision,
    # not a narrower rule.
    ["event", "user_id", "symbol", "quantity", "path", "status", "wallet_count"],
)
def test_ordinary_keys_are_left_alone(key: str) -> None:
    assert not is_sensitive_key(key)
    assert redact({key: "visible"}) == {key: "visible"}


def test_redaction_reaches_nested_structures() -> None:
    event = {
        "event": "provider_request",
        "headers": {"Authorization": f"Bearer {CANARY_VALUE}", "Accept": "application/json"},
        "attempts": [
            {"signature": CANARY_VALUE, "status": 401},
            {"signature": CANARY_VALUE, "status": 200},
        ],
        "wallet": {"derivation": {"xpub": CANARY_VALUE, "index": 0}},
    }

    redacted = redact(event)

    assert json.dumps(redacted).count(CANARY_VALUE) == 0
    assert redacted["headers"] == {"Authorization": REDACTED, "Accept": "application/json"}
    assert [attempt["signature"] for attempt in redacted["attempts"]] == [REDACTED, REDACTED]
    assert redacted["wallet"]["derivation"] == {"xpub": REDACTED, "index": 0}
    assert redacted["event"] == "provider_request"


def test_non_string_keys_do_not_break_the_processor() -> None:
    redacted = redact({"batch": {1: "one", None: "none", "token": CANARY_VALUE}})

    assert redacted["batch"] == {1: "one", None: "none", "token": REDACTED}


def test_the_processor_does_not_mutate_the_event_it_was_given() -> None:
    event = {"api_key": CANARY_VALUE}

    assert redact(event) == {"api_key": REDACTED}
    assert event == {"api_key": CANARY_VALUE}


# A fictional origin, never a real hostname (rule 3). `Settings` refuses to build with
# `environment="prod"` while `allowed_origin` is still the development default, so a
# production settings object in a test has to name one.
PRODUCTION_ORIGIN = "https://portfolio.example"


@pytest.mark.parametrize("environment", ["prod", "dev"])
def test_configured_pipeline_writes_no_secret_to_stdout(
    environment: Literal["dev", "prod"],
    capsys: pytest.CaptureFixture[str],
    restore_logging: None,
) -> None:
    configure_logging(Settings(environment=environment, allowed_origin=PRODUCTION_ORIGIN))
    structlog.get_logger("test").warning(
        "provider_rejected_request",
        api_key=CANARY_VALUE,
        signature=CANARY_VALUE,
        xpub_account=CANARY_VALUE,
        exchange="example",
    )

    written = capsys.readouterr().out

    assert CANARY_VALUE not in written
    assert written.count(REDACTED) == 3
    assert "provider_rejected_request" in written
    assert "example" in written
