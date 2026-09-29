"""The input fingerprint: a SHA-256 over a canonical rendering of everything replay depends on.

**What it covers is exactly what can change the answer**: `METHOD`, `ENGINE_VERSION`, the
sorted cash assets, and every event after deduplication, in replay order. `ENGINE_VERSION`
is in it on purpose. #19 skips a recompute when the fingerprint is unchanged, so an engine
fix that left the fingerprint alone would leave the snapshot it fixed wrong for good.

**Canonical means that equal inputs render identically and unequal ones do not** (I3):

* JSON with sorted keys and no whitespace, encoded as UTF-8. Every text field was refused
  at construction if it could not be encoded, so the encoding cannot raise here.
* An amount as `quantize(value, 18)` in fixed notation, so `1`, `1.0` and `1E+0` are one
  string, and a negative zero is written as zero -- `-0` and `0` are the same amount, and a
  venue that sends either has sent no fee.
* An instant as UTC ISO-8601 with microseconds and a `Z`. The key normalised it to UTC when
  it was built, so two spellings of one instant are one string.
* `None` as JSON `null`, and every field of an event under its own name, so a change to
  any single field changes the digest.

The key names are the engine's own (spec 019, R10). Nothing outside this module parses the
document; it exists to be hashed.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from portfolio.domain.accounting.constants import AMOUNT_SCALE, ENGINE_VERSION, METHOD
from portfolio.domain.accounting.events import Adjustment, EventKey, Trade
from portfolio.domain.money import quantize

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from decimal import Decimal

    from portfolio.domain.accounting.events import AccountingConfig, AccountingEvent

__all__ = ["event_kind", "fingerprint"]

type _Json = str | int | Sequence[_Json] | Mapping[str, _Json] | None


def fingerprint(events: Sequence[AccountingEvent], config: AccountingConfig) -> str:
    """The SHA-256 hex digest of the canonical document for `events` under `config`.

    `events` must already be deduplicated and in replay order: the order is part of the
    input, and putting it in order is `replay`'s job rather than a second copy of the sort
    key here.
    """
    document: dict[str, _Json] = {
        "cash_assets": sorted(config.cash_assets),
        "engine_version": ENGINE_VERSION,
        "events": [_render(event) for event in events],
        "method": METHOD,
    }
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def event_kind(event: AccountingEvent) -> str:
    """`"adjustment"`, `"trade"` or `"transfer"` (spec 019, R10).

    Part of an event's identity -- a venue's withdrawal ids and trade ids are separate number
    spaces -- and the last element of the replay order, so it is defined once, here, where
    the fingerprint also writes it.
    """
    if isinstance(event, Trade):
        return "trade"
    if isinstance(event, Adjustment):
        return "adjustment"
    return "transfer"


def _render(event: AccountingEvent) -> dict[str, _Json]:
    """One event as a JSON object: its kind, its key, and every field it has."""
    rendered: dict[str, _Json] = {"kind": event_kind(event), "key": _render_key(event.key)}
    if isinstance(event, Trade):
        rendered |= {
            "base_asset": event.base_asset,
            "quote_asset": event.quote_asset,
            "side": event.side.value,
            "quantity": _amount(event.quantity),
            "quote_quantity": _amount(event.quote_quantity),
            "fee_amount": _amount(event.fee_amount),
            "fee_asset": event.fee_asset,
        }
    elif isinstance(event, Adjustment):
        rendered |= {
            "asset": event.asset,
            "quantity": _amount(event.quantity),
            "unit_cost": None if event.unit_cost is None else _amount(event.unit_cost),
        }
    else:
        rendered |= {
            "asset": event.asset,
            "quantity": _amount(event.quantity),
            "from_location": event.from_location,
            "to_location": event.to_location,
        }
    return rendered


def _render_key(key: EventKey) -> dict[str, _Json]:
    """An `EventKey`, with the instant as `YYYY-MM-DDTHH:MM:SS.ffffffZ`.

    `isoformat` rather than `strftime`: `%Y` does not zero-pad a year before 1000 on every
    platform, and `isoformat` always writes four digits. The key holds the instant in UTC,
    so the offset `isoformat` writes is always `+00:00`, and it is swapped for the `Z`.
    """
    instant = key.occurred_at.isoformat(timespec="microseconds").removesuffix("+00:00")
    return {"occurred_at": f"{instant}Z", "source": key.source, "external_id": key.external_id}


def _amount(value: Decimal) -> str:
    """`value` at exactly `AMOUNT_SCALE` places, in fixed notation, with `-0` written as `0`.

    Written out from the digits rather than through `str()`, which switches to exponent
    notation for a value as small as `1E-18`, or `format()`, whose output for a given value
    is one more thing to trust. Validation has already bounded the value to fit: at most 38
    digits, so the joins below are far inside every conversion limit.
    """
    sign, digits, _ = quantize(value, AMOUNT_SCALE).as_tuple()
    padded = "".join(str(digit) for digit in digits).rjust(AMOUNT_SCALE + 1, "0")
    negative = sign == 1 and any(digits)
    return f"{'-' if negative else ''}{padded[:-AMOUNT_SCALE]}.{padded[-AMOUNT_SCALE:]}"
