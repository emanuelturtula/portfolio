"""`domain.exchange_exports`: every format read into spec 042's shape (criteria 1 to 3).

Every sample is synthetic and written here: no row of a real export is in the repository, and
no address of any network appears, since none of these formats needs one to be read.
Expected figures are literals, never recomputed with the code under test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal

import pytest

from portfolio.domain.exchange_exports import (
    STABLECOINS,
    ExportError,
    ExportFormat,
    OperationKind,
    ParsedFile,
    ParsedOperation,
    Source,
    parse_export,
)

SAO_PAULO = timezone(timedelta(hours=-3), "America/Sao_Paulo")


def resolve(name: str) -> tzinfo:
    """The one zone these samples name. Anything else is unknown, as the service's is."""
    if name == "America/Sao_Paulo":
        return SAO_PAULO
    raise KeyError(name)


def parse(text: str, name: str = "export.csv") -> ParsedFile:
    return parse_export(name, text, resolve)


def only(parsed: ParsedFile) -> ParsedOperation:
    assert len(parsed.operations) == 1
    return parsed.operations[0]


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def test_the_stablecoins_are_the_specs() -> None:
    assert frozenset({"USDT", "USDC", "DAI"}) == STABLECOINS


# --------------------------------------------------------------------------------------
# Recognition
# --------------------------------------------------------------------------------------


def test_an_unknown_header_is_reported_with_its_rows_not_guessed() -> None:
    parsed = parse("Time,Coin,Interest\n2026-01-01 00:00:00,BTC,1\n\n2026-01-02 00:00:00,BTC,2\n")

    assert parsed == ParsedFile(None, 2, (), "not a format this importer reads")


def test_text_the_csv_reader_cannot_split_is_skipped_not_a_crash() -> None:
    # An unclosed quote turns the rest of a file into one field, past the reader's limit.
    parsed = parse('Note\n"' + "x" * 200_000 + "\n")

    assert parsed == ParsedFile(None, 0, (), "not a CSV file this importer can split into rows")


def test_an_empty_file_is_an_unknown_one() -> None:
    assert parse("") == ParsedFile(None, 0, (), "not a format this importer reads")


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        (
            "Date,Type,Order Id,Trading pair,Base Asset,Quote Asset,Direction,Price,"
            "Order amount,Executed,Average Price,Trading volume,Status",
            "Bitget spot order history: its fills are read from the spot order details",
        ),
        (
            "Date,Type,Funding account,Coin,Quantity,Address,TxID,Status",
            "Bitget deposits and withdrawals: they are read from the spot transactions, "
            "with the fee",
        ),
        (
            "Time(America/Sao_Paulo),type,Amount,newAvailableAmount,Assets",
            "BingX spot ledger: it repeats the spot order history without ids",
        ),
        (
            "Time(Asia/Shanghai),type,Details,Amount,newAvailableAmount,Assets,Futures",
            "BingX futures ledger: futures are read from the order history",
        ),
    ],
)
def test_a_known_format_that_is_not_read_says_why(header: str, reason: str) -> None:
    parsed = parse(f"\ufeff{header}\r\nx\r\n")

    assert parsed == ParsedFile(None, 1, (), reason)


def test_a_header_is_recognised_through_a_bom_padding_and_trailing_blank_cells() -> None:
    text = "\ufeff order , Date ,Coin,Type,Amount,Fee,Available,,,\n"

    assert parse(text) == ParsedFile(ExportFormat.BITGET_SPOT_TRANSACTIONS, 0, (), None)


# --------------------------------------------------------------------------------------
# Bitget spot order details
# --------------------------------------------------------------------------------------

BITGET_FILLS = (
    "\ufeffDate,Trading pair,Base Asset,Quote Asset,Direction,Price,Amount,Total,Fee,Fee Coin\r\n"
    "2026-09-01 16:47:01,KAS/USDT,KAS,USDT,Buy,0.025,1000,25,1,KAS,\r\n"
    "2026-09-01 16:47:01,KAS/USDT,KAS,USDT,Buy,0.025,1000,25,1,KAS,\r\n"
    "2026-09-02 10:00:00,BTC/USDC,BTC,USDC,Sell,100000,0.001,100,0.1,USDC,\r\n"
)


def test_a_bitget_fill_is_read_on_its_utc_minus_three_clock() -> None:
    parsed = parse(BITGET_FILLS)

    assert parsed.format is ExportFormat.BITGET_SPOT_ORDER_DETAILS
    assert parsed.rows == 3
    first = parsed.operations[0]
    assert first == ParsedOperation(
        source=Source.BITGET,
        external_id=first.external_id,
        executed_at=utc(2026, 9, 1, 19, 47, 1),
        kind=OperationKind.BUY,
        asset="KAS",
        quantity=Decimal(1000),
        quote_currency="USDT",
        quote_amount=Decimal(25),
        fee_asset="KAS",
        fee_amount=Decimal(1),
        description="Spot Buy",
    )
    sell = parsed.operations[2]
    assert (sell.kind, sell.asset, sell.quantity, sell.quote_currency) == (
        OperationKind.SELL,
        "BTC",
        Decimal("0.001"),
        "USDC",
    )
    assert (sell.fee_asset, sell.fee_amount) == ("USDC", Decimal("0.1"))


def test_identical_bitget_fills_get_distinct_ids_that_a_second_export_repeats() -> None:
    """Criterion 2: the hash, then `#2` for the second of two identical rows."""
    ids = [operation.external_id for operation in parse(BITGET_FILLS).operations]
    again = [operation.external_id for operation in parse(BITGET_FILLS).operations]

    assert ids == again
    assert len(set(ids)) == 3
    assert ids[0].startswith("bitget_spot_order_details:")
    assert len(ids[0]) == len("bitget_spot_order_details:") + 32
    assert ids[1] == f"{ids[0]}#2"


# --------------------------------------------------------------------------------------
# Bitget spot transactions
# --------------------------------------------------------------------------------------

BITGET_LEDGER = "order,Date,Coin,Type,Amount,Fee,Available\n"


def test_a_bitget_withdrawal_carries_its_fee_and_the_trades_are_left_to_the_fills() -> None:
    parsed = parse(
        BITGET_LEDGER + "\t1001,2026-09-03 09:55:06,KAS,Ordinary Withdrawal,-1000,-5,0\n"
        "\t1002,2026-09-03 09:00:00,KAS,Buy,1000,0,1005\n"
        "\t1002,2026-09-03 09:00:00,USDT,Sell,-25,0,0\n"
    )

    assert parsed.rows == 3
    assert only(parsed) == ParsedOperation(
        source=Source.BITGET,
        external_id="1001",
        executed_at=utc(2026, 9, 3, 12, 55, 6),
        kind=OperationKind.WITHDRAWAL,
        asset="KAS",
        quantity=Decimal(1000),
        quote_currency=None,
        quote_amount=None,
        fee_asset="KAS",
        fee_amount=Decimal(5),
        description="Ordinary Withdrawal",
    )


def test_a_bitget_ledger_line_without_an_order_is_hashed_and_keeps_its_sign() -> None:
    operation = only(parse(BITGET_LEDGER + ",2026-09-03 09:00:00,USDT,Rebate,-0.5,,1\n"))

    assert operation.external_id.startswith("bitget_spot_transactions:")
    assert (operation.kind, operation.quantity) == (OperationKind.OTHER, Decimal("-0.5"))
    assert (operation.fee_asset, operation.fee_amount) == (None, None)


def test_a_bitget_deposit_is_a_magnitude_without_a_fee() -> None:
    operation = only(parse(BITGET_LEDGER + "7,2026-09-03 09:00:00,usdt,Deposit,50,0,50\n"))

    assert (operation.kind, operation.asset, operation.quantity) == (
        OperationKind.DEPOSIT,
        "USDT",
        Decimal(50),
    )
    assert operation.fee_amount is None


# --------------------------------------------------------------------------------------
# BingX
# --------------------------------------------------------------------------------------

BINGX_SPOT = (
    "UID,Order No.,Time(America/Sao_Paulo),Pair,Type,Price,Amount,Order Value,Fee,Fee Coin,"
    "Order Type\n"
)


def test_a_bingx_order_is_read_on_the_clock_its_header_names() -> None:
    operation = only(
        parse(
            BINGX_SPOT + "1,2001,2026-10-07 11:35:35,KAS/USDT,Buy,0.04,1000,40,-1,KAS,Autonomous\n"
        )
    )

    assert operation == ParsedOperation(
        source=Source.BINGX,
        external_id="2001",
        executed_at=utc(2026, 10, 7, 14, 35, 35),
        kind=OperationKind.BUY,
        asset="KAS",
        quantity=Decimal(1000),
        quote_currency="USDT",
        quote_amount=Decimal(40),
        fee_asset="KAS",
        fee_amount=Decimal(1),
        description="Spot Buy",
    )


def test_a_header_naming_an_unknown_zone_refuses_the_file() -> None:
    text = BINGX_SPOT.replace("America/Sao_Paulo", "Mars/Olympus") + "1,2,x\n"

    with pytest.raises(ExportError, match="unknown time zone, 'Mars/Olympus'") as caught:
        parse(text)

    assert caught.value.line == 1


def test_a_bingx_futures_order_is_kept_as_other() -> None:
    header = (
        "UID,Order No.,Time(America/Sao_Paulo),Pair,Type,Leverage,DealPrice,Quantity,Amount,"
        "Fee,Fee Coin,Realized PNL,Quote Asset,Order Type,AvgPrice\n"
    )
    row = (
        "1,3001,2026-10-05 00:32:09,BTC-USDT,Close Long,5X,100000,0.004,400,-0.2,USDT,10,"
        "USDT,Grid,1\n"
    )

    operation = only(parse(header + row))

    assert operation.external_id == "futures:3001"
    assert (operation.kind, operation.asset, operation.quantity) == (
        OperationKind.OTHER,
        "BTC",
        Decimal("0.004"),
    )
    assert (operation.quote_currency, operation.quote_amount) == ("USDT", Decimal(400))
    assert (operation.fee_asset, operation.fee_amount) == ("USDT", Decimal("0.2"))
    assert operation.description == "Futures Close Long"


def test_the_bingx_fund_account_is_hashed_and_never_stores_the_remark() -> None:
    header = "UID,type,amount,new_available_amount,asset_name,Time(America/Sao_Paulo),remark\n"
    remark = "transactionId: 0123abcd"
    parsed = parse(
        header + f"1,Withdraw,-500,0,KAS,2026-10-07 11:36:25,{remark}\n"
        "1,P2P Buy,100,100,USDT,2026-10-07 11:35:06,\n"
        "1,Spot Account -> Fund Account,500,500,KAS,2026-10-07 11:36:24,\n"
    )

    kinds = [(o.kind, o.quantity) for o in parsed.operations]
    assert kinds == [
        (OperationKind.WITHDRAWAL, Decimal(500)),
        (OperationKind.BUY, Decimal(100)),
        (OperationKind.TRANSFER, Decimal(500)),
    ]
    assert all(o.external_id.startswith("bingx_fund_account:") for o in parsed.operations)
    assert all("0123abcd" not in str(o) for o in parsed.operations)
    assert parsed.operations[1].quote_currency is None


# --------------------------------------------------------------------------------------
# Binance
# --------------------------------------------------------------------------------------

BINANCE = "User ID,Time,Account,Operation,Coin,Change,Remark\n"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Binance-Transaction-History-202610090222(UTC--3)-part1-of1.csv", utc(2026, 5, 29, 3)),
        ("history(UTC-3).csv", utc(2026, 5, 29, 3)),
        ("history(UTC+0).csv", utc(2026, 5, 29)),
        ("history(UTC+5:30).csv", utc(2026, 5, 28, 18, 30)),
        ("history(UTC-3:30).csv", utc(2026, 5, 29, 3, 30)),
    ],
)
def test_binance_is_read_on_the_clock_its_file_name_carries(name: str, expected: datetime) -> None:
    operation = only(parse(BINANCE + "1,2026-05-29 00:00:00,Spot,Deposit,USDT,10,\n", name))

    assert operation.executed_at == expected


def test_binance_without_its_clock_in_the_name_is_refused() -> None:
    with pytest.raises(ExportError, match=r"as in \(UTC-3\)"):
        parse(BINANCE + "1,2026-05-29 00:00:00,Spot,Deposit,USDT,10,\n", "renamed.csv")


@pytest.mark.parametrize(
    ("operation", "change", "kind", "quantity"),
    [
        ("P2P Trading", "100", OperationKind.BUY, "100"),
        ("P2P Trading", "-100", OperationKind.SELL, "100"),
        ("Buy Crypto With Fiat", "20", OperationKind.BUY, "20"),
        ("Withdraw", "-30", OperationKind.WITHDRAWAL, "30"),
        ("Deposit", "40", OperationKind.DEPOSIT, "40"),
        ("Transfer Funds to Spot", "-5", OperationKind.TRANSFER, "-5"),
        ("Simple Earn Flexible Interest", "0.1", OperationKind.REWARD, "0.1"),
        ("Asset Recovery", "-0.7", OperationKind.OTHER, "-0.7"),
    ],
)
def test_a_binance_operation_is_named_by_the_venues_words(
    operation: str, change: str, kind: OperationKind, quantity: str
) -> None:
    row = f"1,2026-05-29 06:05:15,Spot,{operation},USDT,{change},a remark\n"

    parsed = only(parse(BINANCE + row, "x(UTC-3).csv"))

    assert (parsed.kind, parsed.quantity) == (kind, Decimal(quantity))
    assert parsed.description == operation
    assert parsed.external_id.startswith("binance_transaction_history:")


# --------------------------------------------------------------------------------------
# Nexo
# --------------------------------------------------------------------------------------

NEXO = (
    "Transaction,Type,Input Currency,Input Amount,Output Currency,Output Amount,USD Equivalent,"
    "Fee,Fee Currency,Details,Date / Time (UTC)  \n"
)


def nexo(row: str) -> ParsedOperation:
    return only(parse(NEXO + row))


def test_a_nexo_purchase_takes_the_fee_out_of_what_was_paid() -> None:
    operation = nexo(
        'NXT1,Exchange,USDT,-66.39,KAS,1684.35,$66.30,0.99,USDT,"approved / x",'
        "2026-05-31 16:50:34\n"
    )

    assert operation == ParsedOperation(
        source=Source.NEXO,
        external_id="NXT1",
        executed_at=utc(2026, 5, 31, 16, 50, 34),
        kind=OperationKind.BUY,
        asset="KAS",
        quantity=Decimal("1684.35"),
        quote_currency="USDT",
        quote_amount=Decimal("65.40"),
        fee_asset="USDT",
        fee_amount=Decimal("0.99"),
        description="Exchange",
    )


def test_a_nexo_sale_into_a_stablecoin_is_a_sell_of_what_went_out() -> None:
    operation = nexo("NXT2,Exchange,KAS,-1684,USDT,81.7,$81,19.5,KAS,x,2026-06-01 00:00:00\n")

    assert (operation.kind, operation.asset, operation.quantity) == (
        OperationKind.SELL,
        "KAS",
        Decimal("1664.5"),
    )
    assert (operation.quote_currency, operation.quote_amount) == ("USDT", Decimal("81.7"))
    assert (operation.fee_asset, operation.fee_amount) == ("KAS", Decimal("19.5"))


def test_a_nexo_conversion_charged_in_what_came_in_adds_the_fee_back() -> None:
    operation = nexo("NXT3,Exchange,USDT,-10,NEXO,9,$10,1,NEXO,x,2026-06-01 00:00:00\n")

    assert (operation.kind, operation.asset, operation.quantity) == (
        OperationKind.BUY,
        "NEXO",
        Decimal(10),
    )
    assert operation.quote_amount == Decimal(10)


def test_a_nexo_conversion_without_a_fee() -> None:
    operation = nexo("NXT4,Exchange,BTC,-0.006,USDT,550,$550,-,-,x,2026-06-01 00:00:00\n")

    assert (operation.kind, operation.quantity, operation.quote_amount) == (
        OperationKind.SELL,
        Decimal("0.006"),
        Decimal(550),
    )
    assert (operation.fee_asset, operation.fee_amount) == (None, None)


def test_a_nexo_withdrawal_is_what_left_less_its_fee() -> None:
    operation = nexo("NXT5,Withdrawal,USDT,-596,USDT,595,$595,1,USDT,x,2026-06-01 00:00:00\n")

    assert (operation.kind, operation.asset, operation.quantity) == (
        OperationKind.WITHDRAWAL,
        "USDT",
        Decimal(595),
    )
    assert (operation.fee_asset, operation.fee_amount) == ("USDT", Decimal(1))


@pytest.mark.parametrize(
    ("kind_text", "kind", "quantity"),
    [
        ("Interest", OperationKind.REWARD, "0.5"),
        ("Exchange Cashback", OperationKind.REWARD, "0.5"),
        ("Top up Crypto", OperationKind.DEPOSIT, "0.5"),
        ("Deposit To Exchange", OperationKind.DEPOSIT, "0.5"),
        ("Fiat Deposit Fee", OperationKind.OTHER, "-0.5"),
    ],
)
def test_other_nexo_types_are_named_after_their_output(
    kind_text: str, kind: OperationKind, quantity: str
) -> None:
    operation = nexo(f"NXT6,{kind_text},NEXO,0.5,NEXO,-0.5,$1,-,-,x,2026-06-01 00:00:00\n")

    assert (operation.kind, operation.asset, operation.quantity) == (
        kind,
        "NEXO",
        Decimal(quantity),
    )


def test_a_nexo_row_naming_no_currency_is_refused() -> None:
    with pytest.raises(ExportError, match="line 2: the row names no currency"):
        nexo("NXT7,Interest,,0,,1,$1,-,-,x,2026-06-01 00:00:00\n")


# --------------------------------------------------------------------------------------
# Buenbit
# --------------------------------------------------------------------------------------

BUENBIT = "FECHA,ID,OPERACION,ESTADO,MONEDA,MONTO,COSTO DE RED,TXID,,,,  \n"


def buenbit(*rows: str) -> ParsedFile:
    return parse(BUENBIT + "".join(f"{row},,,,\n" for row in rows))


def test_a_buenbit_conversion_is_its_dated_line_and_the_line_below() -> None:
    parsed = buenbit(
        '"10/05/2025 12:12:03",T1,CONVERSION,EXITOSO,BTC,0.00512345,0.00000000,',
        ',,,,USDT,"-1,550.05",0.00000000,',
    )

    assert parsed.rows == 2
    assert only(parsed) == ParsedOperation(
        source=Source.BUENBIT,
        external_id="T1:BTC",
        executed_at=utc(2025, 5, 10, 15, 12, 3),
        kind=OperationKind.BUY,
        asset="BTC",
        quantity=Decimal("0.00512345"),
        quote_currency="USDT",
        quote_amount=Decimal("1550.05"),
        fee_asset=None,
        fee_amount=None,
        description="CONVERSION",
    )


def test_a_buenbit_conversion_into_a_stablecoin_is_a_sell() -> None:
    operation = only(
        buenbit(
            "01/06/2025 00:00:00,T2,CONVERSION,EXITOSO,DAI,507.26,0,",
            ",,,,BTC,-0.006,0,",
        )
    )

    assert (operation.kind, operation.asset, operation.quantity) == (
        OperationKind.SELL,
        "BTC",
        Decimal("0.006"),
    )
    assert (operation.quote_currency, operation.quote_amount) == ("DAI", Decimal("507.26"))
    assert operation.external_id == "T2:DAI"


def test_buenbit_interest_continues_on_lines_below_with_one_id_per_currency() -> None:
    parsed = buenbit(
        "02/06/2025 00:00:00,T3,INTERES,ACREDITADO,BTC,0.00000010,0,",
        ",,,,USDC,0.0155,0,",
        ",,,,USDT,0.0566,0,",
    )

    assert [(o.external_id, o.kind, o.asset) for o in parsed.operations] == [
        ("T3:BTC", OperationKind.REWARD, "BTC"),
        ("T3:USDC", OperationKind.REWARD, "USDC"),
        ("T3:USDT", OperationKind.REWARD, "USDT"),
    ]
    assert {o.executed_at for o in parsed.operations} == {utc(2025, 6, 2, 3)}


def test_buenbit_deposits_withdrawals_and_others() -> None:
    parsed = buenbit(
        '03/06/2025 00:00:00,T4,DEPOSITO,EXITOSO,ARS,"1,000,000.00",0.00000000,',
        "04/06/2025 00:00:00,T5,RETIRO,EXITOSO,BTC,-0.01,0.0001,",
        "05/06/2025 00:00:00,T6,AJUSTE,EXITOSO,USDT,-1,,",
    )

    assert [(o.kind, o.quantity, o.fee_amount) for o in parsed.operations] == [
        (OperationKind.DEPOSIT, Decimal(1000000), None),
        (OperationKind.WITHDRAWAL, Decimal("0.01"), Decimal("0.0001")),
        (OperationKind.OTHER, Decimal(-1), None),
    ]
    assert parsed.operations[1].fee_asset == "BTC"


def test_a_buenbit_operation_that_did_not_happen_is_not_stored_nor_its_lines() -> None:
    parsed = buenbit(
        "06/06/2025 00:00:00,T7,CONVERSION,CANCELADO,BTC,1,0,",
        ",,,,USDT,-100,0,",
        "07/06/2025 00:00:00,T8,RETIRO,PENDIENTE,BTC,-1,0,",
    )

    assert parsed.rows == 3
    assert parsed.operations == ()


def test_buenbits_stock_section_is_not_read() -> None:
    text = (
        BUENBIT + "08/06/2025 00:00:00,T9,DEPOSITO,EXITOSO,USD,10,0,,,,,\n"
        "\\-----,-----,-----,-----,-----,-----,-----,-----,-----,-----,-----,-----\n"
        "Operaciones con acciones de Buenbit,,,,,,,,,,,\n"
        "status,operation,origin_currency,x,y,z,w,v,u,t,s,r\n"
    )

    parsed = parse(text)

    assert parsed.rows == 1
    assert only(parsed).asset == "USD"


@pytest.mark.parametrize(
    ("rows", "line", "message"),
    [
        (
            [
                "01/06/2025 00:00:00,T1,CONVERSION,EXITOSO,BTC,1,0,",
                "02/06/2025 00:00:00,T2,DEPOSITO,EXITOSO,USD,1,0,",
            ],
            2,
            "a conversion without what went out",
        ),
        (["01/06/2025 00:00:00,T1,CONVERSION,EXITOSO,BTC,1,0,"], 2, "without what went out"),
        ([",,,,USDT,-1,0,"], 2, "a continuation line with nothing above it"),
        (
            ["01/06/2025 00:00:00,T1,DEPOSITO,EXITOSO,USD,1,0,", ",,,,USDT,-1,0,"],
            3,
            "under an operation that has none",
        ),
    ],
)
def test_a_buenbit_file_out_of_shape_is_refused(rows: list[str], line: int, message: str) -> None:
    with pytest.raises(ExportError, match=message) as caught:
        buenbit(*rows)

    assert caught.value.line == line


# --------------------------------------------------------------------------------------
# Rows that cannot be read
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (
            "2026-09-01 16:47:01,KAS/USDT,KAS,USDT,Buy,1,abc,1,1,KAS",
            "Amount is not a number: 'abc'",
        ),
        ("2026-09-01 16:47:01,KAS/USDT,KAS,USDT,Buy,1,1e,1,1,KAS", "Amount is not a number"),
        ("01/09/2026 16:47:01,KAS/USDT,KAS,USDT,Buy,1,1,1,1,KAS", "Date is not a time"),
        ("2026-02-30 16:47:01,KAS/USDT,KAS,USDT,Buy,1,1,1,1,KAS", "Date is not a time"),
        ("2026-09-01 16:47:01,KAS/USDT,KAS,USDT,Hold,1,1,1,1,KAS", "neither Buy nor Sell: 'hold'"),
        ("2026-09-01 16:47:01,KAS/USDT,,USDT,Buy,1,1,1,1,KAS", "Base Asset is empty"),
        ("2026-09-01 16:47:01,KAS/USDT,KAS,USDT,Buy,1,1,1,1,KAS,extra", "more cells than"),
        ("2026-09-01 16:47:01,KAS/USDT,KAS,USDT,Buy,1,1,1,1", "fewer cells than"),
    ],
)
def test_an_unreadable_row_refuses_the_file_with_its_line(row: str, message: str) -> None:
    """Criterion 3: the line, and why; the caller stores nothing."""
    header = "Date,Trading pair,Base Asset,Quote Asset,Direction,Price,Amount,Total,Fee,Fee Coin\n"
    good = "2026-09-01 16:47:01,KAS/USDT,KAS,USDT,Buy,1,1,1,1,KAS\n"

    with pytest.raises(ExportError, match=message) as caught:
        parse(header + good + row + "\n")

    assert caught.value.line == 3
    assert str(caught.value).startswith("line 3: ")


@pytest.mark.parametrize("pair", ["KASUSDT", "KAS/", "A/B/C"])
def test_a_pair_that_is_not_two_coins_is_refused(pair: str) -> None:
    with pytest.raises(ExportError, match="Pair is not a pair"):
        parse(BINGX_SPOT + f"1,2,2026-10-07 11:35:35,{pair},Buy,1,1,1,-1,KAS,x\n")
