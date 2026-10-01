"""The fill history #93's transactions view is tested against, and the totals it adds up to.

Four suites read the same book -- the repository, the service, the endpoint and the logging
test -- so it lives here, the arrangement `tests/accounting_harness.py` has for #19.

## The book

Seven fills over two venues, chosen so that each rule of spec 024 has a fill that only it
explains:

* **Ties on `executed_at`**, one within a venue (1003 and 1004, both at `minute(20)`) and one
  across venues (1002 and 2001, both at `minute(10)`), inserted so that row-id order and time
  order disagree. Ordering by id alone, or by time with the tie-break dropped, gives a
  different sequence from the one the spec pins.
* **A fill exactly on a boundary** at `minute(10)` and `minute(20)`, for the half-open range.
* **Three quote assets**: USDT, USDC and BTC. A USDC-quoted sale and a BTC-quoted purchase are
  "not valued in USDT", and each leaves the USDT figures of its base asset partial.
* **A USDT value that is not quantity x price** (2002: 1 ETH at 3000 reported as
  3000.123456789012345678), so a value recomputed from the price is a different number.
* **Signed fees**: a rebate (-0.002 ETH), a fee and a rebate in BNB that sum to exactly zero --
  still listed -- and a zero fee with no fee asset, which adds nothing.
* **A derived quote quantity** (2003) and **a fill with no order id** (1004).
* **A distinctive trade id and payload** on every fill, so finding one in a response or a log
  line is a leak rather than a coincidence.

Every expected figure is worked by hand in `BOOK_TOTALS`, beside the fills it comes from.

## Nothing here is a real credential, address or hostname

Trade ids are small integers behind a synthetic prefix, and no address appears.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import text

from portfolio.domain.exchanges import ExchangeKey, FillSide
from tests.accounting_harness import plant_account, plant_fills
from tests.exchange_sync_harness import changed, make_fill

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from portfolio.providers.exchanges.base import NormalizedFill

FILLS_PATH: Final = "/api/exchanges/fills"

#: The first instant of the book. A whole minute, so every boundary built from it is exact.
BOOK_START: Final = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

#: A prefix no real venue id carries, so a trade id found in a response or a log is a leak.
TRADE_ID_PREFIX: Final = "tid-VIEW-"

#: The distinctive text inside every fill's `raw_payload`.
PAYLOAD_SENTINEL: Final = "payload-sentinel-" + "Wq8" * 5

#: The fields of one row on the wire, exactly as spec 024 pins them.
ROW_FIELDS: Final = frozenset(
    {
        "id",
        "executed_at",
        "exchange_key",
        "symbol",
        "base_asset",
        "quote_asset",
        "side",
        "quantity",
        "price",
        "quote_quantity",
        "quote_quantity_derived",
        "usdt_value",
        "fee_amount",
        "fee_asset",
        "order_id",
    }
)
TOP_LEVEL_FIELDS: Final = frozenset({"fills", "total_count", "totals"})
TOTALS_FIELDS: Final = frozenset({"fill_count", "by_asset", "usdt", "not_valued_in_usdt", "fees"})
ASSET_FIELDS: Final = frozenset(
    {
        "asset",
        "fill_count",
        "bought",
        "sold",
        "net",
        "usdt_spent",
        "usdt_received",
        "usdt_net",
        "usdt_unvalued_fill_count",
    }
)
USDT_FIELDS: Final = frozenset({"spent", "received", "net"})
NOT_VALUED_FIELDS: Final = frozenset({"fill_count", "by_quote_asset"})
QUOTE_FIELDS: Final = frozenset({"quote_asset", "fill_count", "spent", "received", "net"})
FEE_FIELDS: Final = frozenset({"asset", "amount"})

#: Every property of the response that carries an amount or a quantity.
MONEY_FIELDS: Final = frozenset(
    {
        "quantity",
        "price",
        "quote_quantity",
        "usdt_value",
        "fee_amount",
        "bought",
        "sold",
        "net",
        "usdt_spent",
        "usdt_received",
        "usdt_net",
        "spent",
        "received",
        "amount",
    }
)


def minute(offset: int) -> datetime:
    """`BOOK_START` plus `offset` minutes."""
    return BOOK_START + timedelta(minutes=offset)


def trade_id(number: int) -> str:
    """The distinctive trade id of fill `number`."""
    return f"{TRADE_ID_PREFIX}{number}"


def book_fill(number: int, executed_at: datetime, **fields: Any) -> NormalizedFill:
    """One valid fill of the book, with a distinctive trade id and payload."""
    fill = make_fill(
        number,
        executed_at,
        raw_payload=f'{{"note":"{PAYLOAD_SENTINEL}","n":{number}}}',
        **fields,
    )
    return changed(fill, external_trade_id=trade_id(number))


def the_book() -> dict[ExchangeKey, list[NormalizedFill]]:
    """Bitget's four fills, then BingX's three. Inserted in this order, so ids run 1001 first.

    The time order, newest first, with the row ids a fresh database assigns:

        2003 (7) minute 30  KAS  buy   USDT
        1004 (4) minute 20  BTC  sell  USDC   tie, higher id first
        1003 (3) minute 20  ETH  buy   BTC
        2001 (5) minute 10  BTC  buy   USDT   tie across venues, higher id first
        1002 (2) minute 10  BTC  sell  USDT
        2002 (6) minute  5  ETH  sell  USDT
        1001 (1) minute  0  BTC  buy   USDT
    """
    bitget = [
        book_fill(
            1001,
            minute(0),
            quantity="0.5",
            price="60000",
            quote_quantity="30000",
            fee_amount="0.0005",
            fee_asset="BTC",
            order_id="ord-1001",
        ),
        book_fill(
            1002,
            minute(10),
            side=FillSide.SELL,
            quantity="0.2",
            price="65000",
            quote_quantity="13000",
            fee_amount="13",
            fee_asset="USDT",
            order_id="ord-1002",
        ),
        book_fill(
            1003,
            minute(20),
            symbol="ETHBTC",
            base_asset="ETH",
            quote_asset="BTC",
            quantity="2",
            price="0.05",
            quote_quantity="0.1",
            fee_amount="-0.002",
            fee_asset="ETH",
            order_id="ord-1003",
        ),
        changed(
            book_fill(
                1004,
                minute(20),
                symbol="BTCUSDC",
                quote_asset="USDC",
                side=FillSide.SELL,
                quantity="0.1",
                price="61000",
                quote_quantity="6100",
                fee_amount="0",
                fee_asset=None,
            ),
            external_order_id=None,
        ),
    ]
    bingx = [
        book_fill(
            2001,
            minute(10),
            quantity="0.3",
            price="60000",
            quote_quantity="18000",
            fee_amount="0.01",
            fee_asset="BNB",
            order_id="ord-2001",
        ),
        book_fill(
            2002,
            minute(5),
            symbol="ETHUSDT",
            base_asset="ETH",
            side=FillSide.SELL,
            quantity="1",
            price="3000",
            quote_quantity="3000.123456789012345678",
            fee_amount="-0.01",
            fee_asset="BNB",
            order_id="ord-2002",
        ),
        changed(
            book_fill(
                2003,
                minute(30),
                symbol="KASUSDT",
                base_asset="KAS",
                quantity="1000",
                price="0.1",
                quote_quantity="100",
                fee_amount="0.1",
                fee_asset="USDT",
                order_id="ord-2003",
            ),
            quote_quantity_derived=True,
        ),
    ]
    return {ExchangeKey.BITGET: bitget, ExchangeKey.BINGX: bingx}


#: The book's trade numbers, newest first with ties broken by row id descending.
BOOK_ORDER: Final = (2003, 1004, 1003, 2001, 1002, 2002, 1001)


def D(value: str) -> Decimal:  # noqa: N802 - a literal, spelled short so the tables read
    """A `Decimal` from its exact text."""
    return Decimal(value)


#: The totals of the whole book, worked by hand, as `Decimal`s.
#:
#: BTC: bought 0.5 (1001) + 0.3 (2001) = 0.8; sold 0.2 (1002) + 0.1 (1004) = 0.3; net 0.5.
#:      USDT spent 30000 + 18000 = 48000, received 13000, net 35000. 1004 is USDC: 1 unvalued.
#: ETH: bought 2 (1003, BTC-quoted), sold 1 (2002); net 1. USDT spent 0, received
#:      3000.123456789012345678, net -3000.123456789012345678. 1003 is BTC-quoted: 1 unvalued.
#: KAS: bought 1000; USDT spent 100 (derived, still as stored). Nothing unvalued.
#: USDT: spent 48000 + 100 = 48100; received 13000 + 3000.123456789012345678; net
#:      48100 - 16000.123456789012345678 = 32099.876543210987654322.
#: Not valued: BTC-quoted 1003 spent 0.1 BTC; USDC-quoted 1004 received 6100 USDC.
#: Fees: BNB 0.01 - 0.01 = 0 (listed), BTC 0.0005, ETH -0.002 (a rebate), USDT 13 + 0.1.
#:      1004's zero fee with no asset adds nothing.
BOOK_TOTALS: Final[dict[str, Any]] = {
    "fill_count": 7,
    "by_asset": [
        {
            "asset": "BTC",
            "fill_count": 4,
            "bought": D("0.8"),
            "sold": D("0.3"),
            "net": D("0.5"),
            "usdt_spent": D("48000"),
            "usdt_received": D("13000"),
            "usdt_net": D("35000"),
            "usdt_unvalued_fill_count": 1,
        },
        {
            "asset": "ETH",
            "fill_count": 2,
            "bought": D("2"),
            "sold": D("1"),
            "net": D("1"),
            "usdt_spent": D("0"),
            "usdt_received": D("3000.123456789012345678"),
            "usdt_net": D("-3000.123456789012345678"),
            "usdt_unvalued_fill_count": 1,
        },
        {
            "asset": "KAS",
            "fill_count": 1,
            "bought": D("1000"),
            "sold": D("0"),
            "net": D("1000"),
            "usdt_spent": D("100"),
            "usdt_received": D("0"),
            "usdt_net": D("100"),
            "usdt_unvalued_fill_count": 0,
        },
    ],
    "usdt": {
        "spent": D("48100"),
        "received": D("16000.123456789012345678"),
        "net": D("32099.876543210987654322"),
    },
    "not_valued_in_usdt": {
        "fill_count": 2,
        "by_quote_asset": [
            {
                "quote_asset": "BTC",
                "fill_count": 1,
                "spent": D("0.1"),
                "received": D("0"),
                "net": D("0.1"),
            },
            {
                "quote_asset": "USDC",
                "fill_count": 1,
                "spent": D("0"),
                "received": D("6100"),
                "net": D("-6100"),
            },
        ],
    },
    "fees": [
        {"asset": "BNB", "amount": D("0")},
        {"asset": "BTC", "amount": D("0.0005")},
        {"asset": "ETH", "amount": D("-0.002")},
        {"asset": "USDT", "amount": D("13.1")},
    ],
}

#: What every figure is when nothing matched.
EMPTY_TOTALS: Final[dict[str, Any]] = {
    "fill_count": 0,
    "by_asset": [],
    "usdt": {"spent": D("0"), "received": D("0"), "net": D("0")},
    "not_valued_in_usdt": {"fill_count": 0, "by_quote_asset": []},
    "fees": [],
}


def decimals_of(node: object) -> object:
    """A response's totals with every money string turned into a `Decimal`, for comparison.

    Only a key in `MONEY_FIELDS` is converted, and only from a string: a money field that
    arrived as a JSON number stays a number and fails the comparison, which is the point.
    """
    if isinstance(node, dict):
        return {
            key: (
                Decimal(value)
                if key in MONEY_FIELDS and isinstance(value, str)
                else decimals_of(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [decimals_of(item) for item in node]
    return node


async def user_id_of(factory: async_sessionmaker[AsyncSession], username: str = "owner") -> int:
    """The id of a `users` row that already exists."""
    async with factory() as session:
        found = await session.scalar(
            text("SELECT id FROM users WHERE username = :name"), {"name": username}
        )
    assert found is not None, f"no user {username!r}"
    return int(found)


async def plant_history(
    factory: async_sessionmaker[AsyncSession],
    user_id: int,
    fills: Mapping[ExchangeKey, Sequence[NormalizedFill]],
) -> dict[ExchangeKey, int]:
    """`user_id`'s account at each venue, created if missing, and its fills. Committed.

    Returns each venue's account id. Fills go through the application's own insert.
    """
    accounts: dict[ExchangeKey, int] = {}
    async with factory() as session:
        for exchange_key, venue_fills in fills.items():
            existing = await session.scalar(
                text(
                    "SELECT id FROM exchange_accounts WHERE user_id = :user AND exchange_key = :key"
                ),
                {"user": user_id, "key": str(exchange_key)},
            )
            account = (
                int(existing)
                if existing is not None
                else await plant_account(session, user_id, exchange_key)
            )
            accounts[exchange_key] = account
            if venue_fills:
                await plant_fills(session, account, venue_fills)
    return accounts


async def row_ids(factory: async_sessionmaker[AsyncSession]) -> dict[int, int]:
    """Each book fill's number, mapped to the row id the database gave it."""
    async with factory() as session:
        result = await session.execute(text("SELECT id, external_trade_id FROM exchange_fills"))
        found = {str(row.external_trade_id): int(row.id) for row in result}
    return {
        int(external.removeprefix(TRADE_ID_PREFIX)): identifier
        for external, identifier in found.items()
        if external.startswith(TRADE_ID_PREFIX)
    }
