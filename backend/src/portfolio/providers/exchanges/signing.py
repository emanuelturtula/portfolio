"""HMAC-SHA256 request signing, in the two encodings the target venues are believed to use.

Two functions and no knowledge of any venue. **Which string is signed is the venue's
business** -- Bitget is believed to sign `timestamp + METHOD + path + query + body` and
BingX its query string, both unconfirmed until #13 and #14 read the documentation -- and it
belongs in the provider that builds the request. What is shared is the primitive, and
getting the primitive exactly right once: UTF-8 on both inputs, SHA-256, and the output
encoding the venue asks for.

## The secret arrives as a `SecretStr` and is unwrapped inside

`get_secret_value()` is called here and nowhere else, so no provider ever holds the raw
secret in a local variable -- a local is what a traceback renders, what a debugger shows
and what a well-meaning `logger.debug` picks up. A plain `str` is refused statically by
`mypy --strict`, and at run time it has no `get_secret_value` and fails on the first call.

## The signature is itself sensitive

For the length of a request's receive window a signature authorises that request, and one
venue carries it in the query string. Nothing here logs it; the transport logs
`request_target` -- scheme, host and a label -- and never a path or a query, which is the
control a venue signing in the query string relies on.

## What was verified, and against what

Both encodings are checked against RFC 4231's HMAC-SHA256 test vectors, which are
published independently of this code. That confirms the primitive. It confirms nothing
about any venue: the string a venue expects to be signed, and the encoding it expects back,
are #13's and #14's to confirm against their documentation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import SecretStr

__all__ = ["hmac_sha256_base64", "hmac_sha256_hex"]


def hmac_sha256_hex(secret: SecretStr, message: str) -> str:
    """The HMAC-SHA256 of `message` under `secret`, as 64 lower-case hex characters."""
    return _digest(secret, message).hex()


def hmac_sha256_base64(secret: SecretStr, message: str) -> str:
    """The HMAC-SHA256 of `message` under `secret`, in standard padded Base64.

    The standard alphabet (`+` and `/`) with `=` padding, which is RFC 4648 section 4 --
    not the URL-safe alphabet. A venue that wants the URL-safe form is a venue that says
    so, and would get its own function rather than a flag here.
    """
    return base64.b64encode(_digest(secret, message)).decode("ascii")


def _digest(secret: SecretStr, message: str) -> bytes:
    """The raw 32-byte MAC. Both inputs are UTF-8 encoded; the secret is unwrapped here."""
    return hmac.new(
        secret.get_secret_value().encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
