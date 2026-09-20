"""Guards that make a migration run safe on SQLite.

These live in an importable module rather than inside `env.py` on purpose. Alembic loads
`env.py` by path and its module body runs a migration on import, so nothing can import it
to test it; anything put there is also invisible to coverage. The behaviour here is the
difference between a migration that silently destroys rows and one that does not, so it
has to be reachable by a test.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import column as sql_column
from sqlalchemy import literal_column, select
from sqlalchemy import table as sql_table

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

_logger = structlog.get_logger(__name__)

# A dangling reference, identified by something that survives a table rebuild:
# (child table, parent table, foreign key index, the child row's foreign key values).
ViolationIdentity = tuple[str, str, int, tuple[object, ...]]
ViolationCounts = Counter[ViolationIdentity]


class MigrationIntegrityError(RuntimeError):
    """A migration run cannot be made safe, or has left the database inconsistent."""


def disable_foreign_key_enforcement(connection: Connection) -> None:
    """Turn foreign key enforcement off on one migration connection, and prove it took.

    SQLite has no `ALTER COLUMN`, so Alembic changes a column by rebuilding the table:
    create `_alembic_tmp_users`, `INSERT ... SELECT` into it, **`DROP TABLE users`**,
    rename. With `foreign_keys=ON` that `DROP TABLE` performs an implicit `DELETE FROM`,
    which fires foreign key actions on every referencing table. A rebuild of `users`
    therefore cascades through `fk_sessions_user_id_users` and empties `sessions`, while
    the migration reports success and the container comes up healthy. Alembic's
    `prep_table_for_batch` is a no-op on SQLite, so nothing else protects the referencing
    side. A `NO ACTION` reference instead aborts the run with `FOREIGN KEY constraint
    failed`, which is loud but still a failed deploy.

    This cannot be pushed down into the revision that needs it. `PRAGMA foreign_keys` is
    documented as a no-op inside a transaction, and the whole migration run happens inside
    one, so `op.execute("PRAGMA foreign_keys=OFF")` in a revision is a statement that
    succeeds and changes nothing -- the worst available failure mode. Hence AUTOCOMMIT,
    before any transaction opens, and hence the read-back: the pragma is verified, not
    assumed, because whether it took effect depends on driver internals this code should
    not have to trust.

    The application's own engine is deliberately untouched: `foreign_keys=ON` at runtime
    is the point of the schema's foreign keys. Only this one connection differs, and only
    while migrations are running. Do not "fix" this back.
    """
    if connection.dialect.name != "sqlite":
        return

    previous_isolation_level = connection.get_isolation_level()
    connection.execution_options(isolation_level="AUTOCOMMIT")
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    still_enforced = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
    # SQLAlchemy autobegins a logical transaction on the first execute even under
    # AUTOCOMMIT, and the isolation level may not be changed while one is open.
    connection.commit()

    if still_enforced:
        message = (
            "Refusing to run migrations: PRAGMA foreign_keys still reports enforcement "
            "is on after being switched off under AUTOCOMMIT. A batch rebuild would "
            "silently delete every row referencing the table being rebuilt."
        )
        raise MigrationIntegrityError(message)

    connection.execution_options(isolation_level=previous_isolation_level)


def _foreign_key_columns(
    connection: Connection,
    child_table: str,
    cache: dict[str, dict[int, tuple[str, ...]]],
) -> dict[int, tuple[str, ...]]:
    """The child-side columns of every foreign key on a table, keyed by its index.

    `PRAGMA foreign_key_list` answers `(id, seq, table, from, to, on_update, on_delete,
    match)`, and its `id` is the same number `foreign_key_check` reports as `fkid`. `seq`
    orders the columns of a composite key. Cached because one damaged table usually means
    many damaged rows.
    """
    if child_table not in cache:
        columns_by_key: dict[int, list[tuple[int, str]]] = defaultdict(list)
        quoted = child_table.replace('"', '""')
        rows = connection.exec_driver_sql(f'PRAGMA foreign_key_list("{quoted}")').fetchall()
        for row in rows:
            columns_by_key[int(row[0])].append((int(row[1]), str(row[3])))
        cache[child_table] = {
            key: tuple(name for _seq, name in sorted(pairs))
            for key, pairs in columns_by_key.items()
        }
    return cache[child_table]


def _referencing_values(
    connection: Connection,
    child_table: str,
    columns: tuple[str, ...],
    rowid: object,
) -> tuple[object, ...]:
    """Read the offending row's foreign key values -- the part of it that identifies it.

    Built with SQLAlchemy constructs rather than an f-string so the identifiers are quoted
    by the dialect. The values are used only for comparison and are never logged: this
    reads from `sessions`, whose `token_hash` must not reach a log record.
    """
    if not columns or rowid is None:
        return ()
    target = sql_table(child_table, *(sql_column(name) for name in columns))
    row = connection.execute(select(*target.c).where(literal_column("rowid") == rowid)).fetchone()
    return tuple(row) if row is not None else ()


def foreign_key_violations(connection: Connection) -> ViolationCounts:
    """Count every orphaned reference in the database, by a rebuild-stable identity.

    `PRAGMA foreign_key_check` answers `(table, rowid, parent, fkid)`, and the rowid is
    deliberately **not** part of the identity. Alembic's batch mode rebuilds a table by
    copying its rows into a new one, and unless the primary key happens to be an alias for
    the rowid -- an `INTEGER PRIMARY KEY` -- the copy comes out renumbered. Identifying a
    violation by rowid would therefore make every pre-existing orphan in a rebuilt table
    look brand new, which is the bug this identity exists to avoid.

    The identity is the child row's own foreign key values instead, because those travel
    with the row through a rebuild: the same orphan before and after is still "a `sessions`
    row pointing at `users` with `user_id = 4242`". Counting rather than collecting into a
    set is what lets two identical orphans be told apart from one.

    A `WITHOUT ROWID` table reports a NULL rowid and so contributes no values; its
    violations collapse to one identity per foreign key and are compared by count alone.
    """
    counts: ViolationCounts = Counter()
    if connection.dialect.name != "sqlite":
        return counts

    column_cache: dict[str, dict[int, tuple[str, ...]]] = {}
    for row in connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        child_table, rowid, parent_table, key_index = str(row[0]), row[1], str(row[2]), int(row[3])
        columns = _foreign_key_columns(connection, child_table, column_cache).get(key_index, ())
        values = _referencing_values(connection, child_table, columns, rowid)
        counts[(child_table, parent_table, key_index, values)] += 1
    return counts


def _totals_by_table(counts: ViolationCounts) -> dict[str, int]:
    """Collapse the identities to a count per child table, which is all a message needs."""
    totals: dict[str, int] = defaultdict(int)
    for (child_table, _parent, _key_index, _values), count in counts.items():
        totals[child_table] += count
    return totals


def snapshot_foreign_key_violations(connection: Connection) -> ViolationCounts:
    """Record what was already broken before a migration run, and say so out loud.

    Without this baseline the integrity check cannot tell a reference this run introduced
    from one that arrived in the database file -- a restored backup, a torn WAL copy, a
    hand-edit on the host. Every startup runs the check, and the overwhelmingly common
    startup applies no migrations at all, so without a baseline a damaged file makes the
    container refuse to start, the deploy roll back, and the previous image refuse
    identically. That is unrecoverable without hand-editing SQL on the only copy of the
    trade history.

    Pre-existing damage is therefore reported rather than fatal. Only the table and the
    count are logged: `sessions` holds `token_hash`, and no column value belongs in a log
    record.
    """
    already_present = foreign_key_violations(connection)
    for child_table, count in sorted(_totals_by_table(already_present).items()):
        _logger.warning(
            "pre_existing_foreign_key_violations",
            table=child_table,
            count=count,
            detail="present before this migration run; not introduced by it",
        )
    return already_present


def assert_no_dangling_foreign_keys(
    connection: Connection,
    pre_existing: ViolationCounts | None = None,
) -> None:
    """Refuse to commit orphaned references that *this run* introduced.

    Enforcement is off for the duration of a run, so SQLite will not object on its own and
    this deferred check is the only thing looking. Call it inside the migration transaction
    and before it commits, so that raising rolls the whole run back.

    `pre_existing` is the baseline from `snapshot_foreign_key_violations`, taken before any
    revision ran. Subtracting whole identities rather than comparing totals is what makes a
    run that repairs one orphan and introduces another fail instead of looking clean.
    Omitting the baseline means "assume the database started clean", so every violation
    found counts as introduced.
    """
    already_present = pre_existing if pre_existing is not None else Counter()
    introduced = foreign_key_violations(connection) - already_present
    if not introduced:
        return

    total = sum(introduced.values())
    tables = ", ".join(sorted(_totals_by_table(introduced)))
    message = (
        f"Migrations introduced {total} dangling foreign key reference(s) in {tables}. "
        f"Rolling back rather than committing them: enforcement is off while migrations "
        f"run, so nothing downstream would notice."
    )
    if already_present:
        message += (
            f" The database also arrived with {sum(already_present.values())} dangling "
            f"reference(s), which this run neither introduced nor touched."
        )
    raise MigrationIntegrityError(message)
