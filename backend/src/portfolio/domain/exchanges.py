"""The exchange vocabulary: which venues exist, and which way a fill went.

Two enums and nothing else, and they are here rather than in `providers/exchanges/` for the
reason `domain/chains.py` gives about `ChainKey`: a `CHECK` constraint in `db/models.py`
mirrors each of them, and `db` sits above `domain` and below `providers`. The value a column
admits, the value a provider produces and the value that crosses the API are one string
rather than three that have to be kept in step.

**Adding a member is therefore a migration, not an enum edit.** The `CHECK` texts are
literals in `db/models.py` and in the migration that created them, and a test reflects
each constraint off a migrated database and compares it against the model's constant. An
enum member the column refuses is an insert that fails in production, so the test that
pins these values is the one that should fail first.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ExchangeKey", "FillSide"]


class ExchangeKey(StrEnum):
    """Every venue spot fills can be imported from.

    A `StrEnum`, so `exchange_accounts.exchange_key == ExchangeKey.BITGET` compares the
    string the column holds. The order is alphabetical because the `CHECK` constraint lists
    the values that way and a reader comparing the two should not have to sort either.
    """

    BINGX = "bingx"
    BITGET = "bitget"


class FillSide(StrEnum):
    """Whether a fill bought or sold the base asset.

    Always from the owner's side of the trade, and always about the **base** asset: a
    `BUY` of `BTCUSDT` spent USDT and received BTC. A venue that reports the side of the
    taker, or reports it about the quote asset, must translate before building a
    `NormalizedFill` -- a fill recorded the wrong way round is a cost basis with the sign
    flipped, and nothing downstream can tell.
    """

    BUY = "buy"
    SELL = "sell"
