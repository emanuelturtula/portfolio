"""Exchange operations: upload an export, enter one by hand, list and delete (spec 042).

`domain.exchange_exports` reads one file's text. This module does what that module may not:
it decodes the upload, opens a zip in memory, resolves a named time zone from the host's
database, and stores what is new.

## An upload is all or nothing

Every file is parsed before anything is written. A row that cannot be read refuses the whole
upload, naming the file and the line (R4), and the transaction is never committed. A file that
is not a known format is reported as skipped with its row count, never dropped silently.

## The limits (R12)

The decoded upload may be at most `MAX_UPLOAD_BYTES`. A zip may hold at most `MAX_ZIP_FILES`
files and `MAX_ZIP_BYTES` once uncompressed, both checked against its directory before any
member is read.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import lzma
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from portfolio.domain.exchange_exports import (
    ExportError,
    ExportFormat,
    OperationKind,
    ParsedOperation,
    Source,
    parse_export,
)
from portfolio.repositories.exchange_operations import (
    ExchangeOperationRepository,
    NewOperation,
    OperationFilter,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import tzinfo
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession

    from portfolio.db.models import ExchangeOperation

__all__ = [
    "MAX_UPLOAD_BYTES",
    "MAX_ZIP_BYTES",
    "MAX_ZIP_FILES",
    "ExchangeOperationService",
    "FileReport",
    "ImportReport",
    "ManualOperation",
    "OperationFilter",
    "OperationNotFoundError",
    "OperationNotManualError",
    "OperationPage",
    "OperationView",
    "UploadRefusedError",
    "build_exchange_operation_service",
    "utc_now",
]

MAX_UPLOAD_BYTES: Final = 5 * 1024 * 1024
"""The largest decoded upload. The owner's whole history, zipped, is under a megabyte."""

MAX_ZIP_FILES: Final = 100
"""The most members a zip may hold. Bitget's download, the largest seen, holds 41."""

MAX_ZIP_BYTES: Final = 50 * 1024 * 1024
"""The most a zip may hold uncompressed, so a small upload cannot expand without bound."""

_ZIP_MAGIC: Final = b"PK\x03\x04"

_VENUES: Final = {
    Source.BITGET: "Bitget",
    Source.BINGX: "BingX",
    Source.BINANCE: "Binance",
    Source.NEXO: "Nexo",
    Source.BUENBIT: "Buenbit",
}
"""The venue an imported operation is filed under, for the table to show."""


def utc_now() -> datetime:
    """The production clock, aware and in UTC."""
    return datetime.now(UTC)


def resolve_zone(name: str) -> tzinfo:
    """A zone name from a header, from the host's time-zone database.

    Raises:
        KeyError: the database has no such zone (`ZoneInfoNotFoundError` is one).
        ValueError: the name is not a zone name at all.
    """
    return ZoneInfo(name)


class UploadRefusedError(Exception):
    """The upload cannot be read; nothing was stored. The message says why, for the owner."""


class OperationNotFoundError(Exception):
    """No operation with that id belongs to the caller."""


class OperationNotManualError(Exception):
    """The operation came from an export, and only a manual entry can be deleted (R11)."""


@dataclass(frozen=True, slots=True)
class FileReport:
    """What one file of an upload held and what of it was new."""

    name: str
    format: ExportFormat | None
    rows: int
    stored: int
    already_stored: int
    skipped_reason: str | None


@dataclass(frozen=True, slots=True)
class ImportReport:
    """What an upload stored, file by file."""

    filename: str
    files: tuple[FileReport, ...]
    stored: int
    already_stored: int


@dataclass(frozen=True, slots=True)
class OperationView:
    """One stored operation, as the API serves it."""

    id: int
    source: str
    venue: str
    external_id: str
    executed_at: datetime
    kind: str
    asset: str
    quantity: Decimal
    quote_currency: str | None
    quote_amount: Decimal | None
    fee_asset: str | None
    fee_amount: Decimal | None
    description: str

    @property
    def manual(self) -> bool:
        """Whether the owner entered it, and so whether it can be deleted."""
        return self.source == Source.MANUAL


@dataclass(frozen=True, slots=True)
class OperationPage:
    """One page of operations, newest first, how many the filter keeps, and every asset and
    venue stored, filtered or not."""

    total: int
    operations: tuple[OperationView, ...]
    assets: tuple[str, ...]
    venues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManualOperation:
    """An operation the exports do not cover (R11): a buy or sell, such as a swap in a wallet
    app, or a reward or network fee, which has no counterpart (spec 043)."""

    venue: str
    executed_at: datetime
    kind: OperationKind
    asset: str
    quantity: Decimal
    quote_currency: str | None
    quote_amount: Decimal | None
    fee_asset: str | None
    fee_amount: Decimal | None
    description: str


def view_of(row: ExchangeOperation) -> OperationView:
    return OperationView(
        id=row.id,
        source=row.source,
        venue=row.venue,
        external_id=row.external_id,
        executed_at=row.executed_at,
        kind=row.kind,
        asset=row.asset,
        quantity=row.quantity,
        quote_currency=row.quote_currency,
        quote_amount=row.quote_amount,
        fee_asset=row.fee_asset,
        fee_amount=row.fee_amount,
        description=row.description,
    )


@dataclass(frozen=True, slots=True)
class _ParsedUpload:
    name: str
    format: ExportFormat | None
    rows: int
    operations: tuple[ParsedOperation, ...]
    skipped_reason: str | None


class ExchangeOperationService:
    """Owns the transaction: the repository flushes and this class commits."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        operations: ExchangeOperationRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._operations = operations
        self._clock = clock

    async def import_upload(self, user_id: int, filename: str, content_base64: str) -> ImportReport:
        """Store every operation of the upload that is not stored yet.

        Raises:
            UploadRefusedError: the upload is not base64, is too large, is a broken zip, or has
                a row that cannot be read. Nothing is stored.
        """
        content = _decode(content_base64)
        parsed = [_parse(name, data) for name, data in _files(filename, content)]

        now = self._clock()
        seen: dict[str, set[str]] = {}
        new: list[NewOperation] = []
        reports = []
        for file in parsed:
            stored = 0
            for operation in file.operations:
                if operation.source not in seen:
                    seen[operation.source] = await self._operations.stored_ids(
                        user_id,
                        operation.source,
                        (
                            other.external_id
                            for each in parsed
                            for other in each.operations
                            if other.source == operation.source
                        ),
                    )
                if operation.external_id in seen[operation.source]:
                    continue
                seen[operation.source].add(operation.external_id)
                new.append(_new(operation))
                stored += 1
            reports.append(
                FileReport(
                    name=file.name,
                    format=file.format,
                    rows=file.rows,
                    stored=stored,
                    already_stored=len(file.operations) - stored,
                    skipped_reason=file.skipped_reason,
                )
            )

        total_stored = sum(report.stored for report in reports)
        total_already = sum(report.already_stored for report in reports)
        import_id = await self._operations.add_import(
            user_id=user_id,
            filename=filename,
            sha256=hashlib.sha256(content).hexdigest(),
            stored=total_stored,
            already_stored=total_already,
            imported_at=now,
        )
        await self._operations.add(
            user_id=user_id, operations=new, import_id=import_id, created_at=now
        )
        await self._session.commit()
        return ImportReport(
            filename=filename,
            files=tuple(reports),
            stored=total_stored,
            already_stored=total_already,
        )

    async def list_operations(
        self,
        user_id: int,
        *,
        limit: int,
        offset: int,
        filters: OperationFilter | None = None,
    ) -> OperationPage:
        """One page of the owner's operations that `filters` keeps, newest first, with every
        asset and venue the owner has, for choosing the next filter."""
        total = await self._operations.count(user_id, filters)
        rows = await self._operations.page(user_id, limit=limit, offset=offset, filters=filters)
        return OperationPage(
            total=total,
            operations=tuple(view_of(row) for row in rows),
            assets=tuple(await self._operations.assets(user_id)),
            venues=tuple(await self._operations.venues(user_id)),
        )

    async def add_manual(self, user_id: int, entry: ManualOperation) -> OperationView:
        """Store a buy or a sell entered by hand, under an id of its own."""
        now = self._clock()
        operation = NewOperation(
            source=Source.MANUAL,
            venue=entry.venue,
            external_id=f"manual:{uuid.uuid4().hex}",
            executed_at=entry.executed_at,
            kind=entry.kind,
            asset=entry.asset,
            quantity=entry.quantity,
            quote_currency=entry.quote_currency,
            quote_amount=entry.quote_amount,
            fee_asset=entry.fee_asset,
            fee_amount=entry.fee_amount,
            description=entry.description,
        )
        (row,) = await self._operations.add(
            user_id=user_id, operations=[operation], import_id=None, created_at=now
        )
        await self._session.commit()
        return view_of(row)

    async def delete_manual(self, user_id: int, operation_id: int) -> None:
        """Delete one manual entry.

        Raises:
            OperationNotFoundError: no operation with that id belongs to the caller.
            OperationNotManualError: it came from an export.
        """
        row = await self._operations.get(user_id, operation_id)
        if row is None:
            raise OperationNotFoundError
        if row.source != Source.MANUAL:
            raise OperationNotManualError
        await self._operations.delete(row)
        await self._session.commit()


def _new(operation: ParsedOperation) -> NewOperation:
    return NewOperation(
        source=operation.source,
        venue=_VENUES[operation.source],
        external_id=operation.external_id,
        executed_at=operation.executed_at,
        kind=operation.kind,
        asset=operation.asset,
        quantity=operation.quantity,
        quote_currency=operation.quote_currency,
        quote_amount=operation.quote_amount,
        fee_asset=operation.fee_asset,
        fee_amount=operation.fee_amount,
        description=operation.description,
    )


def _decode(content_base64: str) -> bytes:
    # The decoded size is known from the encoded one, so an oversized upload is refused
    # before it is decoded at all.
    if len(content_base64) > (MAX_UPLOAD_BYTES * 4) // 3 + 4:
        message = f"the file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB"
        raise UploadRefusedError(message)
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        message = "the upload is not valid base64"
        raise UploadRefusedError(message) from error
    if len(content) > MAX_UPLOAD_BYTES:
        message = f"the file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB"
        raise UploadRefusedError(message)
    return content


def _files(filename: str, content: bytes) -> list[tuple[str, bytes | None]]:
    """The upload's files: itself, or a zip's members. `None` for a member that is itself a
    zip, which is reported rather than opened."""
    if not content.startswith(_ZIP_MAGIC):
        return [(filename, content)]
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        members = [
            info
            for info in archive.infolist()
            if not info.is_dir() and not info.filename.startswith("__MACOSX/")
        ]
        if len(members) > MAX_ZIP_FILES:
            message = f"the zip holds more than {MAX_ZIP_FILES} files"
            raise UploadRefusedError(message)
        # `zipfile` never returns more than a member's recorded size, and fails a member whose
        # bytes do not match its checksum, so the directory's sizes are a bound that holds.
        if sum(info.file_size for info in members) > MAX_ZIP_BYTES:
            message = f"the zip holds more than {MAX_ZIP_BYTES // (1024 * 1024)} MiB uncompressed"
            raise UploadRefusedError(message)
        files: list[tuple[str, bytes | None]] = []
        for info in members:
            data = archive.read(info)
            files.append((info.filename, None if data.startswith(_ZIP_MAGIC) else data))
    # `RuntimeError` is what `zipfile` raises for an encrypted member, `NotImplementedError` for
    # a compression method it lacks, and the decompressors their own errors for garbled bytes:
    # `zlib.error`, `lzma.LZMAError`, and `OSError` from bz2.
    except (
        zipfile.BadZipFile,
        zlib.error,
        lzma.LZMAError,
        OSError,
        RuntimeError,
        NotImplementedError,
        EOFError,
    ) as error:
        message = "the zip cannot be opened"
        raise UploadRefusedError(message) from error
    return files


def _parse(name: str, data: bytes | None) -> _ParsedUpload:
    """One file, parsed; refused whole on a row it cannot read (R4)."""
    if data is None:
        return _ParsedUpload(name, None, 0, (), "a zip inside the zip is not opened")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _ParsedUpload(name, None, 0, (), "not a UTF-8 text file")
    try:
        parsed = parse_export(name.rsplit("/", 1)[-1], text, resolve_zone)
    except ExportError as error:
        message = f"{name}, {error}"
        raise UploadRefusedError(message) from error
    return _ParsedUpload(name, parsed.format, parsed.rows, parsed.operations, parsed.skipped_reason)


def build_exchange_operation_service(
    session: AsyncSession, *, clock: Callable[[], datetime] = utc_now
) -> ExchangeOperationService:
    """Assemble the service over one database session."""
    return ExchangeOperationService(
        session=session, operations=ExchangeOperationRepository(session), clock=clock
    )
