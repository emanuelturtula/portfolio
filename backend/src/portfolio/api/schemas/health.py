"""Response models for `GET /api/health/detail`: how the application's own sources stand.

`backup` is the only source today (#22, spec 029). #23 adds the others beside it, which is
why the payload is an object keyed by source rather than the backup's fields at the top.

## No configuration value is served

Not the backup directory, not the interval, not the retention. They are the operator's, they
are in the operator's environment file, and an endpoint is not where anybody needs to read
them back. What is served is what the owner can act on: whether the copies are current,
when the newest is from, how many there are, and how the last attempt went.

The two enums are the service's, and their members are the wire form, so the generated
TypeScript types are unions of exactly these strings and a page's wording table can be total.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

# Runtime imports, not `TYPE_CHECKING` ones: Pydantic resolves a field's type when the model
# class is created.
from portfolio.services.backup import BackupErrorKind, BackupState, BackupStatus


class BackupStatusResponse(BaseModel):
    """How the scheduled copies of the database stand.

    `latest_at` is the newest copy's instant, `null` with none; `count` is how many copies
    there are. Both are `null` when `state` is `unreadable`: the backup directory cannot be
    listed, so they are unknown, which is not the same as none. `last_attempt_at` and
    `last_error_kind` describe the timer's most recent attempt in this process -- both `null`
    before one, and `last_error_kind` `null` after a success. They are held in memory, so a
    restart clears them.
    """

    state: BackupState
    latest_at: datetime | None
    count: int | None
    last_attempt_at: datetime | None
    last_error_kind: BackupErrorKind | None

    @classmethod
    def of(cls, status: BackupStatus) -> BackupStatusResponse:
        """Render the service's status."""
        return cls(
            state=status.state,
            latest_at=status.latest_at,
            count=status.count,
            last_attempt_at=status.last_attempt_at,
            last_error_kind=status.last_error_kind,
        )


class HealthDetailResponse(BaseModel):
    """The state of each source the application owns. Only `backup` so far."""

    backup: BackupStatusResponse
