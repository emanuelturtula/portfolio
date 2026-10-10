"""Exchange exports: recognise a CSV by its header row and read its operations (spec 042).

`parse_export(name, text, resolve_zone)` is the whole interface. **Pure**: the caller decodes
the bytes and unpacks any zip, and hands this module one file's text. A named time zone, which
only BingX's header carries, is resolved by the `resolve_zone` the caller passes, because the
time-zone database is on disk and this package reads nothing from it.

## One shape for every format

Every format becomes `ParsedOperation`s with spec 042's rulings applied:

* **R1.** `kind` is one of `OperationKind`. A conversion is a `buy` of the coin received paid
  in the coin given, except when only the coin received is a stablecoin: then it is a `sell`
  of the coin given. So `USDT -> KAS` and `KAS -> USDT` are a buy and a sell of KAS, never a
  buy of USDT.
* **R2.** `quantity` is what the operation moved before its fee, and `fee_amount` is the fee
  on top. `buy`, `sell`, `reward`, `deposit` and `withdrawal` carry a magnitude; `transfer`
  and `other` keep the source's sign, since their direction is all they say.
* **R3.** `external_id` is the source's own id where it has one, and otherwise the format and
  a hash of the row. A second row with the same id in one file gets `#2`, a third `#3`, so
  identical fills (Bitget writes them) are each stored once rather than once between them.
* **R4.** A header this module does not know is reported, never guessed. A row it cannot read
  in a format it does know raises `ExportError` with the line, and the caller stores nothing.
* **R5.** Every `executed_at` is aware and in UTC.

Nothing here is a `float`: amounts are parsed from their text straight into `Decimal`.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

__all__ = [
    "STABLECOINS",
    "ExportError",
    "ExportFormat",
    "OperationKind",
    "ParsedFile",
    "ParsedOperation",
    "Source",
    "parse_export",
]

STABLECOINS: Final = frozenset({"USDT", "USDC", "DAI"})
"""The coins counted one for one as USDT (R7), and the ones a conversion is priced in (R1)."""

_ARGENTINA: Final = timezone(timedelta(hours=-3))
"""Bitget's and Buenbit's unmarked clock. Argentina has kept UTC-3 all year since 2009."""


class OperationKind(StrEnum):
    """What an operation did. The member is its own stored and wire form."""

    BUY = "buy"
    SELL = "sell"
    REWARD = "reward"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TRANSFER = "transfer"
    OTHER = "other"
    FEE = "fee"
    """A network fee no export lists, entered by hand (spec 043). No parser produces one."""


class Source(StrEnum):
    """Where a stored operation came from. `manual` is the owner's own entry (R11)."""

    BITGET = "bitget"
    BINGX = "bingx"
    BINANCE = "binance"
    NEXO = "nexo"
    BUENBIT = "buenbit"
    MANUAL = "manual"


class ExportFormat(StrEnum):
    """A format this module reads. The member is the name an upload's report gives."""

    BITGET_SPOT_ORDER_DETAILS = "bitget_spot_order_details"
    BITGET_SPOT_TRANSACTIONS = "bitget_spot_transactions"
    BINGX_SPOT_ORDER_HISTORY = "bingx_spot_order_history"
    BINGX_FUTURES_ORDER_HISTORY = "bingx_futures_order_history"
    BINGX_FUND_ACCOUNT = "bingx_fund_account"
    BINANCE_TRANSACTION_HISTORY = "binance_transaction_history"
    NEXO_TRANSACTIONS = "nexo_transactions"
    BUENBIT_HISTORY = "buenbit_history"


@dataclass(frozen=True, slots=True)
class ParsedOperation:
    """One operation of an export, in spec 042's shape. See the module docstring."""

    source: Source
    external_id: str
    executed_at: datetime
    kind: OperationKind
    asset: str
    quantity: Decimal
    quote_currency: str | None
    quote_amount: Decimal | None
    fee_asset: str | None
    fee_amount: Decimal | None
    description: str


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """What one file held.

    * `format` -- `None` when the header is not one this module reads.
    * `rows` -- the file's data rows, blank lines aside, whether or not they became operations.
    * `operations` -- in the file's order.
    * `skipped_reason` -- why nothing was read, exactly when `format` is `None`.
    """

    format: ExportFormat | None
    rows: int
    operations: tuple[ParsedOperation, ...]
    skipped_reason: str | None


class ExportError(ValueError):
    """A recognised file has a row that cannot be read. `line` is 1-based, as an editor
    shows it."""

    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.reason = message


# --------------------------------------------------------------------------------------
# Recognition
# --------------------------------------------------------------------------------------

_ZONED_TIME: Final = re.compile(r"Time\((?P<zone>[^)]+)\)")
"""BingX names its clock in the header: `Time(America/Sao_Paulo)`."""

_TIME: Final = "Time(*)"
"""Where a signature below accepts any `Time(<zone>)` cell."""

_READ: Final[Mapping[tuple[str, ...], ExportFormat]] = {
    (
        "Date",
        "Trading pair",
        "Base Asset",
        "Quote Asset",
        "Direction",
        "Price",
        "Amount",
        "Total",
        "Fee",
        "Fee Coin",
    ): ExportFormat.BITGET_SPOT_ORDER_DETAILS,
    (
        "order",
        "Date",
        "Coin",
        "Type",
        "Amount",
        "Fee",
        "Available",
    ): ExportFormat.BITGET_SPOT_TRANSACTIONS,
    (
        "UID",
        "Order No.",
        _TIME,
        "Pair",
        "Type",
        "Price",
        "Amount",
        "Order Value",
        "Fee",
        "Fee Coin",
        "Order Type",
    ): ExportFormat.BINGX_SPOT_ORDER_HISTORY,
    (
        "UID",
        "Order No.",
        _TIME,
        "Pair",
        "Type",
        "Leverage",
        "DealPrice",
        "Quantity",
        "Amount",
        "Fee",
        "Fee Coin",
        "Realized PNL",
        "Quote Asset",
        "Order Type",
        "AvgPrice",
    ): ExportFormat.BINGX_FUTURES_ORDER_HISTORY,
    (
        "UID",
        "type",
        "amount",
        "new_available_amount",
        "asset_name",
        _TIME,
        "remark",
    ): ExportFormat.BINGX_FUND_ACCOUNT,
    (
        "User ID",
        "Time",
        "Account",
        "Operation",
        "Coin",
        "Change",
        "Remark",
    ): ExportFormat.BINANCE_TRANSACTION_HISTORY,
    (
        "Transaction",
        "Type",
        "Input Currency",
        "Input Amount",
        "Output Currency",
        "Output Amount",
        "USD Equivalent",
        "Fee",
        "Fee Currency",
        "Details",
        "Date / Time (UTC)",
    ): ExportFormat.NEXO_TRANSACTIONS,
    (
        "FECHA",
        "ID",
        "OPERACION",
        "ESTADO",
        "MONEDA",
        "MONTO",
        "COSTO DE RED",
        "TXID",
    ): ExportFormat.BUENBIT_HISTORY,
}
"""The formats read, by their header cells in order."""

_NOT_READ: Final[Mapping[tuple[str, ...], str]] = {
    (
        "Date",
        "Type",
        "Order Id",
        "Trading pair",
        "Base Asset",
        "Quote Asset",
        "Direction",
        "Price",
        "Order amount",
        "Executed",
        "Average Price",
        "Trading volume",
        "Status",
    ): "Bitget spot order history: its fills are read from the spot order details",
    (
        "Date",
        "Type",
        "Funding account",
        "Coin",
        "Quantity",
        "Address",
        "TxID",
        "Status",
    ): "Bitget deposits and withdrawals: they are read from the spot transactions, with the fee",
    (
        _TIME,
        "type",
        "Amount",
        "newAvailableAmount",
        "Assets",
    ): "BingX spot ledger: it repeats the spot order history without ids",
    (
        _TIME,
        "type",
        "Details",
        "Amount",
        "newAvailableAmount",
        "Assets",
        "Futures",
    ): "BingX futures ledger: futures are read from the order history",
}
"""Formats recognised and deliberately not read, with the reason the report gives (R4)."""

_UNKNOWN: Final = "not a format this importer reads"

_NOT_CSV: Final = "not a CSV file this importer can split into rows"

_BUENBIT_END: Final = re.compile(r"\\?-{3,}")
"""The line of dashes before Buenbit's stock operations, which have other columns and are not
read. It is written escaped, `\\-----`, in the files seen."""


def _signature(header: Sequence[str]) -> tuple[tuple[str, ...], str | None]:
    """The header's cells with blanks trimmed from the end, any `Time(<zone>)` cell as
    `_TIME`, and the zone it named."""
    cells = [cell.strip().lstrip("﻿").strip() for cell in header]
    while cells and not cells[-1]:
        cells.pop()
    zone = None
    for index, cell in enumerate(cells):
        match = _ZONED_TIME.fullmatch(cell)
        if match is not None:
            zone = match["zone"]
            cells[index] = _TIME
    return tuple(cells), zone


# --------------------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------------------


def parse_export(
    name: str,
    text: str,
    resolve_zone: Callable[[str], tzinfo],
) -> ParsedFile:
    """Recognise `text` by its first row and read every operation in it.

    `name` is the file's name, read for one thing only: Binance writes its clock there, as in
    `(UTC-3)`, and nowhere in the file. `resolve_zone` turns a zone name such as
    `America/Sao_Paulo` into a `tzinfo`, raising `KeyError` or `ValueError` for one it does
    not know.

    Raises:
        ExportError: a row of a recognised format cannot be read, or the clock it needs
            cannot be found.
    """
    try:
        lines = list(csv.reader(text.lstrip("﻿").splitlines(keepends=True)))
    except csv.Error:
        # A field past the reader's size limit: an unclosed quote in some other text file.
        return ParsedFile(None, 0, (), _NOT_CSV)
    if not lines:
        return ParsedFile(None, 0, (), _UNKNOWN)
    signature, zone_name = _signature(lines[0])
    export_format = _READ.get(signature)
    rows = []
    for number, cells in enumerate(lines[1:], start=2):
        if export_format is ExportFormat.BUENBIT_HISTORY and _BUENBIT_END.fullmatch(
            cells[0].strip() if cells else ""
        ):
            break
        if any(cell.strip() for cell in cells):
            rows.append((number, cells))

    if export_format is None:
        reason = _NOT_READ.get(signature, _UNKNOWN)
        return ParsedFile(None, len(rows), (), reason)

    table = _Table(signature, rows)
    zone: tzinfo = _ARGENTINA
    if zone_name is not None:
        zone = _resolved(zone_name, resolve_zone)
    if export_format is ExportFormat.BINANCE_TRANSACTION_HISTORY:
        zone = _binance_zone(name)

    operations = _PARSERS[export_format](table, zone)
    return ParsedFile(export_format, len(rows), _numbered(operations), None)


def _resolved(zone_name: str, resolve_zone: Callable[[str], tzinfo]) -> tzinfo:
    try:
        return resolve_zone(zone_name)
    except (KeyError, ValueError) as error:
        raise ExportError(1, f"the header names an unknown time zone, {zone_name!r}") from error


_BINANCE_ZONE: Final = re.compile(r"\(UTC[+-]?(?P<hours>[+-]\d{1,2})(?::?(?P<minutes>\d{2}))?\)")
"""Binance's `(UTC--3)`, `(UTC-3)` or `(UTC+0)`: an optional separator, then a signed offset."""


def _binance_zone(name: str) -> tzinfo:
    match = _BINANCE_ZONE.search(name)
    if match is None:
        message = (
            "Binance writes its clock only in the file name, as in (UTC-3); "
            "upload the file with the name Binance gave it"
        )
        raise ExportError(1, message)
    hours = int(match["hours"])
    minutes = int(match["minutes"] or 0)
    offset = timedelta(hours=hours, minutes=-minutes if hours < 0 else minutes)
    return timezone(offset)


def _numbered(operations: Iterator[ParsedOperation]) -> tuple[ParsedOperation, ...]:
    """Give a repeated id its occurrence number, so identical rows stay distinct (R3)."""
    seen: Counter[str] = Counter()
    numbered = []
    for operation in operations:
        seen[operation.external_id] += 1
        occurrence = seen[operation.external_id]
        if occurrence > 1:
            operation = replace(operation, external_id=f"{operation.external_id}#{occurrence}")
        numbered.append(operation)
    return tuple(numbered)


# --------------------------------------------------------------------------------------
# Reading cells
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Row:
    """One data row, by header cell. `Time(<zone>)` is under `_TIME`."""

    line: int
    cells: Mapping[str, str]

    def text(self, column: str) -> str:
        return self.cells[column].strip()

    def required(self, column: str) -> str:
        value = self.text(column)
        if not value:
            raise ExportError(self.line, f"{column} is empty")
        return value

    def symbol(self, column: str) -> str:
        return self.required(column).upper()

    def amount(self, column: str, *, thousands: bool = False) -> Decimal:
        return _amount(self.required(column), self.line, column, thousands=thousands)

    def optional_amount(self, column: str) -> Decimal | None:
        value = self.text(column)
        if value in {"", "-"}:
            return None
        return _amount(value, self.line, column, thousands=False)

    def digest(self, export_format: ExportFormat) -> str:
        """The format and a hash of every cell, for a row without an id of its own (R3)."""
        joined = "\x1f".join(self.cells.values())
        return f"{export_format}:{hashlib.sha256(joined.encode()).hexdigest()[:32]}"


class _Table:
    def __init__(self, header: tuple[str, ...], rows: list[tuple[int, list[str]]]) -> None:
        self._header = header
        self._rows = rows

    def __iter__(self) -> Iterator[_Row]:
        width = len(self._header)
        for line, cells in self._rows:
            if any(cell.strip() for cell in cells[width:]):
                raise ExportError(line, "the row has more cells than the header")
            if len(cells) < width:
                raise ExportError(line, "the row has fewer cells than the header")
            yield _Row(line, dict(zip(self._header, cells, strict=False)))


_NUMBER: Final = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")


def _amount(text: str, line: int, column: str, *, thousands: bool) -> Decimal:
    cleaned = text.replace(",", "") if thousands else text
    if _NUMBER.fullmatch(cleaned) is None:
        raise ExportError(line, f"{column} is not a number: {text!r}")
    return Decimal(cleaned)


_ISO_TIME: Final = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})")
_DAY_FIRST_TIME: Final = re.compile(r"(\d{2})/(\d{2})/(\d{4}) (\d{2}):(\d{2}):(\d{2})")


def _instant(row: _Row, column: str, zone: tzinfo, *, day_first: bool = False) -> datetime:
    """The cell's wall-clock time on `zone`'s clock, in UTC (R5)."""
    text = row.required(column)
    match = (_DAY_FIRST_TIME if day_first else _ISO_TIME).fullmatch(text)
    if match is None:
        raise ExportError(row.line, f"{column} is not a time: {text!r}")
    parts = [int(part) for part in match.groups()]
    if day_first:
        parts[0], parts[2] = parts[2], parts[0]
    year, month, day, hour, minute, second = parts
    try:
        local = datetime(year, month, day, hour, minute, second, tzinfo=zone)
    except ValueError as error:
        raise ExportError(row.line, f"{column} is not a time: {text!r}") from error
    return local.astimezone(UTC)


def _pair(row: _Row, column: str) -> tuple[str, str]:
    """`KAS/USDT` or `BTC-USDT` as base and quote."""
    text = row.symbol(column)
    parts = re.split(r"[/-]", text)
    if len(parts) != 2 or not all(parts):
        raise ExportError(row.line, f"{column} is not a pair: {text!r}")
    return parts[0], parts[1]


def _side(row: _Row, column: str) -> OperationKind:
    text = row.required(column).lower()
    if text == "buy":
        return OperationKind.BUY
    if text == "sell":
        return OperationKind.SELL
    raise ExportError(row.line, f"{column} is neither Buy nor Sell: {text!r}")


@dataclass(frozen=True, slots=True)
class _Trade:
    kind: OperationKind
    asset: str
    quantity: Decimal
    quote_currency: str
    quote_amount: Decimal


def _conversion(received: str, received_qty: Decimal, given: str, given_qty: Decimal) -> _Trade:
    """R1: a buy of what came in, unless only what came in is a stablecoin."""
    if received in STABLECOINS and given not in STABLECOINS:
        return _Trade(OperationKind.SELL, given, given_qty, received, received_qty)
    return _Trade(OperationKind.BUY, received, received_qty, given, given_qty)


def _fee(amount: Decimal | None, asset: str | None) -> tuple[str | None, Decimal | None]:
    """A fee as stored: positive, with its asset, or neither when there is none."""
    if amount is None or amount == 0 or not asset or asset == "-":
        return None, None
    return asset.upper(), abs(amount)


# --------------------------------------------------------------------------------------
# The formats
# --------------------------------------------------------------------------------------


def _bitget_order_details(table: _Table, zone: tzinfo) -> Iterator[ParsedOperation]:
    """One fill per row, with no id: the content hash and its occurrence number (R3)."""
    for row in table:
        fee_asset, fee_amount = _fee(row.amount("Fee"), row.text("Fee Coin"))
        yield ParsedOperation(
            source=Source.BITGET,
            external_id=row.digest(ExportFormat.BITGET_SPOT_ORDER_DETAILS),
            executed_at=_instant(row, "Date", zone),
            kind=_side(row, "Direction"),
            asset=row.symbol("Base Asset"),
            quantity=abs(row.amount("Amount")),
            quote_currency=row.symbol("Quote Asset"),
            quote_amount=abs(row.amount("Total")),
            fee_asset=fee_asset,
            fee_amount=fee_amount,
            description=f"Spot {row.required('Direction')}",
        )


def _bitget_spot_transactions(table: _Table, zone: tzinfo) -> Iterator[ParsedOperation]:
    """The ledger, less its `Buy` and `Sell` lines, which repeat the order details."""
    for row in table:
        kind_text = row.required("Type")
        if kind_text in {"Buy", "Sell"}:
            continue
        amount = row.amount("Amount")
        kind = _ledger_kind(kind_text, amount)
        fee_asset, fee_amount = _fee(row.optional_amount("Fee"), row.text("Coin"))
        order = row.text("order")
        yield ParsedOperation(
            source=Source.BITGET,
            external_id=order or row.digest(ExportFormat.BITGET_SPOT_TRANSACTIONS),
            executed_at=_instant(row, "Date", zone),
            kind=kind,
            asset=row.symbol("Coin"),
            quantity=_signed(kind, amount),
            quote_currency=None,
            quote_amount=None,
            fee_asset=fee_asset,
            fee_amount=fee_amount,
            description=kind_text,
        )


def _bingx_spot_orders(table: _Table, zone: tzinfo) -> Iterator[ParsedOperation]:
    """One order per row, with its `Order No.`. The fee is negative and on top (R2)."""
    for row in table:
        base, quote = _pair(row, "Pair")
        fee_asset, fee_amount = _fee(row.amount("Fee"), row.text("Fee Coin"))
        yield ParsedOperation(
            source=Source.BINGX,
            external_id=row.required("Order No."),
            executed_at=_instant(row, _TIME, zone),
            kind=_side(row, "Type"),
            asset=base,
            quantity=abs(row.amount("Amount")),
            quote_currency=quote,
            quote_amount=abs(row.amount("Order Value")),
            fee_asset=fee_asset,
            fee_amount=fee_amount,
            description=f"Spot {row.required('Type')}",
        )


def _bingx_futures_orders(table: _Table, zone: tzinfo) -> Iterator[ParsedOperation]:
    """Futures orders, kept as `other`: they move no coin the wallets hold."""
    for row in table:
        base, _ = _pair(row, "Pair")
        fee_asset, fee_amount = _fee(row.amount("Fee"), row.text("Fee Coin"))
        yield ParsedOperation(
            source=Source.BINGX,
            external_id=f"futures:{row.required('Order No.')}",
            executed_at=_instant(row, _TIME, zone),
            kind=OperationKind.OTHER,
            asset=base,
            quantity=row.amount("Quantity"),
            quote_currency=row.symbol("Quote Asset"),
            quote_amount=row.amount("Amount"),
            fee_asset=fee_asset,
            fee_amount=fee_amount,
            description=f"Futures {row.required('Type')}",
        )


def _bingx_fund_account(table: _Table, zone: tzinfo) -> Iterator[ParsedOperation]:
    """The funding ledger. No id, so the content hash; the remark is never stored."""
    for row in table:
        kind_text = row.required("type")
        amount = row.amount("amount")
        kind = _ledger_kind(kind_text, amount)
        yield ParsedOperation(
            source=Source.BINGX,
            external_id=row.digest(ExportFormat.BINGX_FUND_ACCOUNT),
            executed_at=_instant(row, _TIME, zone),
            kind=kind,
            asset=row.symbol("asset_name"),
            quantity=_signed(kind, amount),
            quote_currency=None,
            quote_amount=None,
            fee_asset=None,
            fee_amount=None,
            description=kind_text,
        )


def _binance_history(table: _Table, zone: tzinfo) -> Iterator[ParsedOperation]:
    """One balance change per row, with no id. The remark is never stored."""
    for row in table:
        kind_text = row.required("Operation")
        amount = row.amount("Change")
        kind = _ledger_kind(kind_text, amount)
        yield ParsedOperation(
            source=Source.BINANCE,
            external_id=row.digest(ExportFormat.BINANCE_TRANSACTION_HISTORY),
            executed_at=_instant(row, "Time", zone),
            kind=kind,
            asset=row.symbol("Coin"),
            quantity=_signed(kind, amount),
            quote_currency=None,
            quote_amount=None,
            fee_asset=None,
            fee_amount=None,
            description=kind_text,
        )


def _ledger_kind(text: str, amount: Decimal) -> OperationKind:
    """A ledger line's kind from the venue's own words.

    A buy or sell named here has no counterpart in the row (a P2P purchase paid in pesos), so
    it is stored without a quote and counts as an unvalued trade (R7) if its coin is tracked.
    """
    lowered = text.lower()
    if lowered in {"p2p buy", "p2p sell", "p2p trading", "buy crypto with fiat"}:
        return OperationKind.BUY if amount > 0 else OperationKind.SELL
    if "withdraw" in lowered:
        return OperationKind.WITHDRAWAL
    if ("deposit" in lowered or "top up" in lowered) and "fee" not in lowered:
        return OperationKind.DEPOSIT
    if "transfer" in lowered or "->" in lowered:
        return OperationKind.TRANSFER
    if any(word in lowered for word in ("interest", "reward", "cashback")):
        return OperationKind.REWARD
    return OperationKind.OTHER


def _signed(kind: OperationKind, amount: Decimal) -> Decimal:
    """R2: a magnitude, except for the two kinds whose sign is their direction."""
    if kind in {OperationKind.TRANSFER, OperationKind.OTHER}:
        return amount
    return abs(amount)


def _nexo_transactions(table: _Table, zone: tzinfo) -> Iterator[ParsedOperation]:
    """Nexo's ledger, in UTC, with its own `NXT` ids.

    An `Exchange` or `Withdrawal` input includes the fee when the fee is in the input's coin,
    so it is taken out to bring the quantity to R2's before-the-fee figure.
    """
    del zone  # The header says UTC.
    for row in table:
        kind_text = row.required("Type")
        lowered = kind_text.lower()
        given = row.text("Input Currency").upper()
        received = row.text("Output Currency").upper()
        given_qty = abs(row.amount("Input Amount"))
        received_qty = abs(row.amount("Output Amount"))
        fee_asset, fee_amount = _fee(row.optional_amount("Fee"), row.text("Fee Currency"))
        if fee_asset is not None and fee_amount is not None and fee_asset == given:
            given_qty -= fee_amount
        quote_currency: str | None = None
        quote_amount: Decimal | None = None
        if lowered == "exchange":
            if fee_asset is not None and fee_amount is not None and fee_asset == received:
                received_qty += fee_amount
            trade = _conversion(received, received_qty, given, given_qty)
            kind, asset, quantity = trade.kind, trade.asset, trade.quantity
            quote_currency, quote_amount = trade.quote_currency, trade.quote_amount
        elif "withdraw" in lowered:
            kind, asset, quantity = OperationKind.WITHDRAWAL, given, given_qty
        else:
            kind = _ledger_kind(kind_text, received_qty)
            asset = received
            quantity = _signed(kind, row.amount("Output Amount"))
        if not asset:
            raise ExportError(row.line, "the row names no currency")
        yield ParsedOperation(
            source=Source.NEXO,
            external_id=row.required("Transaction"),
            executed_at=_instant(row, "Date / Time (UTC)", UTC),
            kind=kind,
            asset=asset,
            quantity=quantity,
            quote_currency=quote_currency,
            quote_amount=quote_amount,
            fee_asset=fee_asset,
            fee_amount=fee_amount,
            description=kind_text,
        )


_BUENBIT_DONE: Final = frozenset({"EXITOSO", "ACREDITADO"})
"""The states of an operation that happened. Any other is not stored."""


def _buenbit_history(table: _Table, zone: tzinfo) -> Iterator[ParsedOperation]:
    """Buenbit's history, as Nexo's download carries it.

    A line without a date continues the dated line above it: for a `CONVERSION` it is what
    went out, and for an `INTERES` it is the same day's interest in another currency. Ids
    repeat across currencies, so each is joined with its currency (R3).
    """
    head: _Row | None = None
    pending_conversion: _Row | None = None
    for row in table:
        if row.text("FECHA"):
            if pending_conversion is not None:
                raise ExportError(pending_conversion.line, "a conversion without what went out")
            head = row
            operation = row.required("OPERACION")
            if row.text("ESTADO") not in _BUENBIT_DONE:
                continue
            if operation == "CONVERSION":
                pending_conversion = row
                continue
            yield _buenbit_single(row, row, zone)
            continue

        if head is None:
            raise ExportError(row.line, "a continuation line with nothing above it")
        if head.text("ESTADO") not in _BUENBIT_DONE:
            continue
        if pending_conversion is not None:
            yield _buenbit_conversion(pending_conversion, row, zone)
            pending_conversion = None
        elif head.required("OPERACION") == "INTERES":
            yield _buenbit_single(head, row, zone)
        else:
            raise ExportError(row.line, "a continuation line under an operation that has none")
    if pending_conversion is not None:
        raise ExportError(pending_conversion.line, "a conversion without what went out")


def _buenbit_single(head: _Row, row: _Row, zone: tzinfo) -> ParsedOperation:
    operation = head.required("OPERACION")
    asset = row.symbol("MONEDA")
    amount = row.amount("MONTO", thousands=True)
    kind = {
        "DEPOSITO": OperationKind.DEPOSIT,
        "RETIRO": OperationKind.WITHDRAWAL,
        "INTERES": OperationKind.REWARD,
    }.get(operation, OperationKind.OTHER)
    fee = None
    if row.text("COSTO DE RED"):
        fee = row.amount("COSTO DE RED", thousands=True)
    fee_asset, fee_amount = _fee(fee, asset)
    return ParsedOperation(
        source=Source.BUENBIT,
        external_id=f"{head.required('ID')}:{asset}",
        executed_at=_instant(head, "FECHA", zone, day_first=True),
        kind=kind,
        asset=asset,
        quantity=_signed(kind, amount),
        quote_currency=None,
        quote_amount=None,
        fee_asset=fee_asset,
        fee_amount=fee_amount,
        description=operation,
    )


def _buenbit_conversion(head: _Row, out: _Row, zone: tzinfo) -> ParsedOperation:
    received = head.symbol("MONEDA")
    trade = _conversion(
        received,
        abs(head.amount("MONTO", thousands=True)),
        out.symbol("MONEDA"),
        abs(out.amount("MONTO", thousands=True)),
    )
    return ParsedOperation(
        source=Source.BUENBIT,
        external_id=f"{head.required('ID')}:{received}",
        executed_at=_instant(head, "FECHA", zone, day_first=True),
        kind=trade.kind,
        asset=trade.asset,
        quantity=trade.quantity,
        quote_currency=trade.quote_currency,
        quote_amount=trade.quote_amount,
        fee_asset=None,
        fee_amount=None,
        description="CONVERSION",
    )


_PARSERS: Final[Mapping[ExportFormat, Callable[[_Table, tzinfo], Iterator[ParsedOperation]]]] = {
    ExportFormat.BITGET_SPOT_ORDER_DETAILS: _bitget_order_details,
    ExportFormat.BITGET_SPOT_TRANSACTIONS: _bitget_spot_transactions,
    ExportFormat.BINGX_SPOT_ORDER_HISTORY: _bingx_spot_orders,
    ExportFormat.BINGX_FUTURES_ORDER_HISTORY: _bingx_futures_orders,
    ExportFormat.BINGX_FUND_ACCOUNT: _bingx_fund_account,
    ExportFormat.BINANCE_TRANSACTION_HISTORY: _binance_history,
    ExportFormat.NEXO_TRANSACTIONS: _nexo_transactions,
    ExportFormat.BUENBIT_HISTORY: _buenbit_history,
}
