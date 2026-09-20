"""Guards that make a migration run safe on SQLite.

These live in an importable module rather than inside `env.py` on purpose. Alembic loads
`env.py` by path and its module body runs a migration on import, so nothing can import it
to test it; anything put there is also invisible to coverage. The behaviour here is the
difference between a migration that silently destroys rows and one that does not, so it
has to be reachable by a test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


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


def assert_no_dangling_foreign_keys(connection: Connection) -> None:
    """Refuse to commit a migration run that left an orphaned reference behind.

    Enforcement is off for the duration of the run, so SQLite will not object on its own.
    `PRAGMA foreign_key_check` is the deferred equivalent: it walks every table and
    returns one row per broken reference, as `(table, rowid, parent, fkid)`. Call this
    inside the migration transaction and before it commits, so that raising rolls the
    whole run back rather than leaving a corrupt database that nothing will notice.
    """
    if connection.dialect.name != "sqlite":
        return

    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if not violations:
        return

    tables = sorted({str(row[0]) for row in violations})
    message = (
        f"Migrations left {len(violations)} dangling foreign key reference(s) in "
        f"{', '.join(tables)}. Rolling back rather than committing a database whose "
        f"references are broken in a way runtime enforcement can no longer detect."
    )
    raise MigrationIntegrityError(message)
