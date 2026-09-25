"""Criterion 4: the HMAC-SHA256 helpers, against vectors this code did not produce.

Signing bugs are the classic "works in Postman, fails in code": a key encoded as Latin-1, a
digest rendered upper-case, Base64 over the hex string instead of over the raw bytes. Each
of those produces a string that looks exactly like a signature and is refused by the venue
with a generic auth error. So every expected value here comes from outside the code under
test:

* **Hex**: RFC 4231 section 4.2 (test case 1) and 4.3 (test case 2), HMAC-SHA-256 row,
  transcribed from the RFC. `openssl` agrees with both transcriptions.
* **Base64**: the RFC publishes hex only, so each Base64 literal was computed with OpenSSL
  3.5.4 over the same key and message, with the exact command recorded beside it. `openssl`
  shares no code with Python's `hmac` module.
* **UTF-8**: one extra vector with a non-ASCII key and message, also from `openssl`, because
  the RFC vectors are ASCII and cannot tell UTF-8 from Latin-1.

The keys are the RFC's own synthetic keys (`0x0b` x 20, and `Jefe`), which is what "synthetic
secrets" means here: published, low-entropy, and useless to anyone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest
from pydantic import SecretStr

from portfolio.providers.exchanges.signing import hmac_sha256_base64, hmac_sha256_hex
from tests.providers.test_protocol import run_mypy

if TYPE_CHECKING:
    from pathlib import Path

# RFC 4231, test case 1: Key = 0x0b repeated 20 times, Data = "Hi There".
CASE_1_KEY: Final = "\x0b" * 20
CASE_1_DATA: Final = "Hi There"
# RFC 4231 section 4.2, HMAC-SHA-256.
CASE_1_HEX: Final = "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
# printf 'Hi There' | openssl dgst -sha256 -mac HMAC
#     -macopt hexkey:0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b -binary | base64
CASE_1_BASE64: Final = "sDRMYdjbOFNcqK/OrwvxK4gdwgDJgz2nJuk3bC4yz/c="

# RFC 4231, test case 2: Key = "Jefe", Data = "what do ya want for nothing?".
CASE_2_KEY: Final = "Jefe"
CASE_2_DATA: Final = "what do ya want for nothing?"
# RFC 4231 section 4.3, HMAC-SHA-256.
CASE_2_HEX: Final = "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
# printf 'what do ya want for nothing?' | openssl dgst -sha256 -mac HMAC
#     -macopt key:Jefe -binary | base64
CASE_2_BASE64: Final = "W9zBRr9gdU5qBCQmCJV1x1oAPwidJzmDnexYuWTsOEM="

# Not from the RFC. "Jefe" with an accent and "cafe" with one, each character U+00E9, which
# UTF-8 encodes as the two bytes c3 a9 and Latin-1 as the single byte e9 -- so a helper that
# encoded either side in anything but UTF-8 produces a different digest.
UTF8_KEY: Final = "Jefé"
UTF8_DATA: Final = "café"
# The key's UTF-8 bytes are 4a 65 66 c3 a9 (`printf 'Jef\xc3\xa9' | xxd`).
# printf 'caf\xc3\xa9' | openssl dgst -sha256 -mac HMAC -macopt hexkey:4a6566c3a9
UTF8_HEX: Final = "4e1368bf86ac2565a27bacc7a2d1a48229adf667ff366fba1d6636fd0a709b0a"
# printf 'caf\xc3\xa9' | openssl dgst -sha256 -mac HMAC -macopt hexkey:4a6566c3a9 -binary
#     | base64
UTF8_BASE64: Final = "ThNov4asJWWie6zHotGkgimt9mf/Nm+6HWY2/Qpwmwo="

VECTORS: Final = [
    pytest.param(CASE_1_KEY, CASE_1_DATA, CASE_1_HEX, CASE_1_BASE64, id="rfc4231-case-1"),
    pytest.param(CASE_2_KEY, CASE_2_DATA, CASE_2_HEX, CASE_2_BASE64, id="rfc4231-case-2"),
]


@pytest.mark.parametrize(("key", "data", "hex_digest", "base64_digest"), VECTORS)
def test_hex_digest_matches_rfc_4231(
    key: str, data: str, hex_digest: str, base64_digest: str
) -> None:
    """The published digest, byte for byte, lower-case -- which is what both venues compare."""
    del base64_digest

    assert hmac_sha256_hex(SecretStr(key), data) == hex_digest


@pytest.mark.parametrize(("key", "data", "hex_digest", "base64_digest"), VECTORS)
def test_base64_digest_matches_rfc_4231(
    key: str, data: str, hex_digest: str, base64_digest: str
) -> None:
    """Base64 over the raw 32 bytes, standard alphabet, padded.

    The mistake this catches is Base64 over the *hex string*, which yields 88 characters of
    plausible-looking output. The length is asserted as well so that failure is legible.
    """
    del hex_digest

    signature = hmac_sha256_base64(SecretStr(key), data)

    assert signature == base64_digest
    assert len(signature) == 44


def test_both_helpers_encode_key_and_message_as_utf8() -> None:
    """The RFC vectors are ASCII, and ASCII cannot tell UTF-8 from Latin-1. This vector can."""
    assert hmac_sha256_hex(SecretStr(UTF8_KEY), UTF8_DATA) == UTF8_HEX
    assert hmac_sha256_base64(SecretStr(UTF8_KEY), UTF8_DATA) == UTF8_BASE64


def test_the_two_helpers_agree_about_the_same_digest() -> None:
    """One MAC rendered two ways: the hex and the Base64 of one call decode to equal bytes."""
    import base64

    raw_from_hex = bytes.fromhex(hmac_sha256_hex(SecretStr(CASE_2_KEY), CASE_2_DATA))
    raw_from_base64 = base64.b64decode(hmac_sha256_base64(SecretStr(CASE_2_KEY), CASE_2_DATA))

    assert raw_from_hex == raw_from_base64
    assert len(raw_from_hex) == 32


@pytest.mark.parametrize("helper", [hmac_sha256_hex, hmac_sha256_base64])
def test_the_helpers_refuse_a_plain_string_secret(helper: object) -> None:
    """At run time a `str` has no `get_secret_value`, so the call fails rather than signing.

    Either `AttributeError` (the method is missing) or a `TypeError` from an explicit check
    is a refusal; what matters is that no signature comes back. The static half -- `mypy`
    rejecting the call before it ever runs -- is the test below.
    """
    assert callable(helper)

    with pytest.raises((AttributeError, TypeError)):
        helper(CASE_2_KEY, CASE_2_DATA)


CONFORMING_CALLER: Final = '''\
"""Calls the helpers the way a provider must. The control for the planted caller."""

from __future__ import annotations

from pydantic import SecretStr

from portfolio.providers.exchanges.signing import hmac_sha256_base64, hmac_sha256_hex


def sign(held: SecretStr) -> tuple[str, str]:
    return hmac_sha256_hex(held, "payload"), hmac_sha256_base64(held, "payload")
'''

PLAIN_STRING_CALLER: Final = '''\
"""Passes a plain `str` as the key -- the raw value unwrapped into a local."""

from __future__ import annotations

from portfolio.providers.exchanges.signing import hmac_sha256_base64, hmac_sha256_hex


def sign(raw: str) -> tuple[str, str]:
    return hmac_sha256_hex(raw, "payload"), hmac_sha256_base64(raw, "payload")
'''


def test_mypy_rejects_a_plain_string_secret(tmp_path: Path) -> None:
    """The static refusal, shown failing on the planted caller and passing on the control.

    One invocation over both files, for the reason `tests/providers/test_protocol.py` gives:
    a run that only checked the planted file would go green for any mypy failure at all,
    including an import it could not resolve.
    """
    conforming = tmp_path / "conforming_signer.py"
    planted = tmp_path / "plain_string_signer.py"
    conforming.write_text(CONFORMING_CALLER, encoding="utf-8")
    planted.write_text(PLAIN_STRING_CALLER, encoding="utf-8")

    result = run_mypy([conforming, planted], cache_dir=tmp_path / "mypy-cache")
    output = result.stdout + result.stderr

    assert result.returncode != 0, f"mypy accepted a plain str as the signing key:\n{output}"
    offending = [line for line in output.splitlines() if conforming.name in line]
    assert offending == [], f"the control file failed to type check:\n{chr(10).join(offending)}"
    planted_errors = [line for line in output.splitlines() if planted.name in line]
    # One error per helper: both refuse the `str`, not only the first one called.
    assert len([line for line in planted_errors if "arg-type" in line]) == 2, output
    assert "SecretStr" in output, output
