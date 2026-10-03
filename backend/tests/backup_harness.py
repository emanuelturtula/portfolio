"""What the backup suites share: a live database, copies on disk, and ways to read them back.

Spec 029 (#22). The suites under `tests/db`, `tests/services`, `tests/api`, `tests/cli` and
`tests/security` each need a migrated SQLite file to copy, a directory of copies, and a way
to read a file back that is not the code under test. One copy of those helpers, here, for
the reason `tests/sqlite_harness.py` gives for its own: two copies of a fixture's decisions
drift.

Everything here reads and writes with the standard library's `sqlite3`, never through
`portfolio.db.backup`: a check of what a copy holds that went through the code that wrote it
would be that code agreeing with itself.

Every database is a real file under the test's `tmp_path`, never `:memory:`.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from portfolio.db.alembic_config import upgrade_to_head
from portfolio.domain.backups import backup_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

#: A fixed instant every suite names its copies from, so a name in an assertion is a literal.
T0: Final = datetime(2026, 10, 2, 3, 0, 0, 123456, tzinfo=UTC)

#: The SQLite file header's write and read format versions: 1 for a rollback journal, 2 for
#: WAL. A copy that says 1/1 needs no `-wal` file beside it to be read.
HEADER_WRITE_VERSION: Final = 18
HEADER_READ_VERSION: Final = 19
SIDECARS: Final = ("-wal", "-shm", "-journal")

#: A string no table holds unless a test put it there, so its absence from a log or a
#: response is a statement about that log or response rather than about an empty database.
ROW_SENTINEL: Final = "row-sentinel-7f3a9c41-never-logged"

_INSERT: Final = re.compile(r'^INSERT INTO "((?:[^"]|"")+)" VALUES')


def sqlite_url(database: Path) -> str:
    """The async URL the application is configured with for `database`."""
    return f"sqlite+aiosqlite:///{database.as_posix()}"


def migrated_database(database: Path) -> Path:
    """A database file at `database`, migrated to head by the application's own migrations.

    The engine the migrations run on switches the file to WAL, as the application's does,
    so the file is the shape a live database has on the Pi.
    """
    database.parent.mkdir(parents=True, exist_ok=True)
    upgrade_to_head(sqlite_url(database))
    return database


def execute(database: Path, *statements: str | tuple[str, tuple[Any, ...]]) -> None:
    """Run statements on `database` over one ordinary connection, committed, then closed."""
    with closing(sqlite3.connect(database)) as connection:
        for statement in statements:
            if isinstance(statement, tuple):
                connection.execute(*statement)
            else:
                connection.execute(statement)
        connection.commit()


def add_notes(database: Path, *notes: str) -> None:
    """A table of the test's own, `notes`, holding these strings. Created if missing."""
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, body TEXT)")
        connection.executemany("INSERT INTO notes (body) VALUES (?)", [(note,) for note in notes])
        connection.commit()


def read_only(path: Path) -> sqlite3.Connection:
    """A `mode=ro` connection. **On a WAL database it leaves `-wal` and `-shm` behind**.

    That is ruling R1's premise, and the reason the readers below do not use it: a test that
    read the live database this way would make the next restore refuse as if the
    application were running.
    """
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def reader(path: Path) -> sqlite3.Connection:
    """A connection that never creates `path` and, closing last, leaves nothing beside it.

    `mode=rw` rather than `mode=ro`: a read-write connection that closes last checkpoints
    and removes a WAL database's `-wal` and `-shm`, as the application's own engine does,
    and on a copy, which is one file, a read writes nothing. Only reads are issued on it.
    """
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True)


def table_contents(path: Path) -> dict[str, list[str]]:
    """Every row of every table but SQLite's own, by table name, as `iterdump` writes it.

    One `INSERT` statement per row, in the table's own order, so two files are equal row for
    row exactly when these are equal. Read through `reader`, so that reading the live
    database leaves nothing beside it that a restore would take for an open application.
    """
    with closing(reader(path)) as connection:
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        contents: dict[str, list[str]] = {name: [] for name in names}
        for line in connection.iterdump():
            match = _INSERT.match(line)
            if match is not None and match.group(1).replace('""', '"') in contents:
                contents[match.group(1).replace('""', '"')].append(line)
        return contents


def row_counts(path: Path) -> dict[str, int]:
    """The number of rows in each table, as `table_contents` reads them."""
    return {name: len(rows) for name, rows in table_contents(path).items()}


def integrity(path: Path) -> list[tuple[Any, ...]]:
    """What `PRAGMA integrity_check` answers for `path`, through `reader`."""
    with closing(reader(path)) as connection:
        return list(connection.execute("PRAGMA integrity_check"))


def header_versions(path: Path) -> tuple[int, int]:
    """Bytes 18 and 19 of the file header: (2, 2) is WAL, (1, 1) a self-contained file."""
    header = path.read_bytes()[:100]
    return header[HEADER_WRITE_VERSION], header[HEADER_READ_VERSION]


def sidecars_of(path: Path) -> list[str]:
    """The names of whatever SQLite keeps beside `path` that exists right now."""
    return [
        f"{path.name}{suffix}"
        for suffix in SIDECARS
        if (path.parent / f"{path.name}{suffix}").exists()
    ]


def corrupt_with_orphan_pages(database: Path) -> None:
    """Leave `database` readable but failing `integrity_check`, with no row lost.

    An index is built and then deleted from the schema by hand, so its pages are never
    used. `integrity_check` answers "Page N: never used" rows rather than raising, which is
    the case a check that only caught exceptions would let through.
    """
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE padding (body TEXT)")
        connection.execute("CREATE INDEX padding_body ON padding (body)")
        connection.executemany(
            "INSERT INTO padding (body) VALUES (?)",
            [(f"padding-{index:06d}" * 3,) for index in range(2_000)],
        )
        connection.commit()
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute("DELETE FROM sqlite_master WHERE name = 'padding_body'")
        connection.commit()


#: SQLite's page size sits at offset 16 of the header, big-endian, in two bytes.
_PAGE_SIZE_AT: Final = slice(16, 18)


def scribble_over_a_table(database: Path, table: str) -> None:
    """Overwrite the page header of `table`'s root page: the reviewer's "scribbled page".

    The file still opens, and its header is intact, but reading `table` -- which both a
    copy's check and `integrity_check` do -- finds a page of no known type. The database must
    be closed, with nothing in a `-wal`, so the root page is where the main file says it is.
    """
    with closing(sqlite3.connect(database)) as connection:
        (root,) = connection.execute(
            "SELECT rootpage FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
    raw = bytearray(database.read_bytes())
    page_size = int.from_bytes(raw[_PAGE_SIZE_AT], "big")
    start = (int(root) - 1) * page_size
    raw[start : start + 12] = b"\xff" * 12
    database.write_bytes(bytes(raw))


def damage_the_header(database: Path) -> None:
    """Replace the 16-byte magic string: SQLite answers `SQLITE_NOTADB` for the whole file."""
    raw = bytearray(database.read_bytes())
    raw[0:16] = b"not a database!!"
    database.write_bytes(bytes(raw))


def truncate_to_nothing(database: Path) -> None:
    """A 0-byte file: SQLite opens it as an empty database with no `alembic_version`."""
    database.write_bytes(b"")


def plant_copies(directory: Path, instants: Iterable[datetime], content: bytes = b"") -> list[str]:
    """Files named as copies taken at `instants`, holding `content`. Returns their names.

    Rotation and listing read names and sizes only, so a planted copy need not be a database.
    """
    directory.mkdir(parents=True, exist_ok=True)
    names = []
    for instant in instants:
        name = backup_name(instant)
        (directory / name).write_bytes(content)
        names.append(name)
    return names


def copy_names(directory: Path) -> set[str]:
    """Every file name in `directory`, copies or not. Empty when it does not exist."""
    if not directory.exists():
        return set()
    return {entry.name for entry in directory.iterdir()}


def directory_state(directory: Path) -> dict[str, str]:
    """Every file in `directory` by name, with the SHA-256 of its bytes."""
    if not directory.exists():
        return {}
    return {
        entry.name: hashlib.sha256(entry.read_bytes()).hexdigest()
        for entry in sorted(directory.iterdir())
        if entry.is_file()
    }


def daily(start: datetime, days: int, *, hours: Iterable[int] = (3,)) -> list[datetime]:
    """Instants on `days` consecutive days from `start`'s date, at each of `hours` UTC."""
    first = start.replace(hour=0, minute=0, second=0, microsecond=0)
    return [first + timedelta(days=day, hours=hour) for day in range(days) for hour in hours]


class SteppingClock:
    """A clock that answers each of `moments` in turn, then repeats the last. Counts reads."""

    def __init__(self, *moments: datetime) -> None:
        self._moments = list(moments)
        self.reads = 0

    def __call__(self) -> datetime:
        index = min(self.reads, len(self._moments) - 1)
        self.reads += 1
        return self._moments[index]


def fixed(moment: datetime) -> Callable[[], datetime]:
    """A clock that always answers `moment`."""
    return lambda: moment
