"""Criteria 1, 5, 6, 7 and 9 of #18, at the service: `AdjustmentService` over a real database.

`AdjustmentService` validates an adjustment by building the engine's own `EventKey` and
`Adjustment`, stores it, **commits**, logs the id, and then awaits `after_change` -- the
recompute trigger, which the request dependency binds (spec 023, *Triggering the recompute*).
Everything runs against a real SQLite file migrated to head; the trigger used here is a
recorder that reads the table over a **second** session when it is called, so "after the
commit" is observed rather than assumed: a trigger called before the commit would find the
row missing.

## The refusal matrix

Each refusal is asserted by its field and its exact rule text, the texts `backend-dev-18`
fixed for the API, and each is checked to carry nothing of the value refused. The accepted
boundary next to every refusal is asserted too, so a rule that refused too much fails as
loudly as one that refused too little.

## The property

`test_every_accepted_draft_converts_once_stored` draws drafts from every region -- valid,
refused by each rule, and on each boundary -- and holds two things for all of them: a draft the
service accepts is stored and **converts** to the engine's `Adjustment` from the stored row,
with the values it was entered with; and a draft it refuses is an `InvalidAdjustmentError`
naming one of the five fields and one of a fixed set of rules, with nothing written. It is
spec 023's "a property test holds that every adjustment the API accepts converts".
"""

from __future__ import annotations

import asyncio
import pickle
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from portfolio.domain.accounting import Adjustment, EventKey
from portfolio.repositories.adjustments import AdjustmentRecord, ManualAdjustmentRepository
from portfolio.services import accounting as accounting_module
from portfolio.services.adjustments import (
    ADJUSTMENT_NOT_FOUND_DETAIL,
    ADJUSTMENT_SOURCE,
    NOTE_MAX_LENGTH,
    AdjustmentDraft,
    AdjustmentNotFoundError,
    InvalidAdjustmentError,
    UnconvertibleAdjustmentError,
    adjustment_of,
    build_adjustment_service,
    external_id_of,
)
from tests.accounting_harness import plant_owner, rows
from tests.adjustments_harness import ADJUSTMENTS_SQL
from tests.exchange_sync_harness import SettableClock
from tests.sqlite_harness import migrated_sessionmaker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW: Final = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
ACQUIRED: Final = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
FIELDS: Final = frozenset({"asset", "quantity", "unit_cost", "occurred_at", "note"})

ASSET_RULE: Final = (
    "asset must be the symbol exactly as the exchange spells it: 1 to 20 upper-case letters "
    "or digits, such as BTC"
)
CASH_RULE: Final = (
    "asset must not be a cash asset (USDC, USDT): cash is the unit of account, and an "
    "adjustment of it changes nothing"
)
NAIVE_RULE: Final = "occurred_at must be a timezone-aware datetime"
RANGE_RULE: Final = "occurred_at is outside the range a UTC datetime can represent"
FUTURE_RULE: Final = "occurred_at must not be later than now"
BLANK_NOTE_RULE: Final = "note must not be blank"
LONG_NOTE_RULE: Final = "note must be at most 500 characters"
ENCODING_RULE: Final = "note must be text that encodes as UTF-8"
TOTAL_COST_RULE: Final = "unit_cost times quantity has more than 20 digits before the decimal point"


def amount_rules(field: str) -> set[str]:
    """The engine's amount rules, as they name a request field."""
    return {
        f"{field} must be a finite number",
        f"{field} has more than 18 decimal places",
        f"{field} has more than 20 digits before the decimal point",
    }


#: Every rule a refusal may state. A refusal outside this set is a message nobody reviewed,
#: and one that could be quoting what it refused.
KNOWN_RULES: Final = frozenset(
    {
        ASSET_RULE,
        CASH_RULE,
        NAIVE_RULE,
        RANGE_RULE,
        FUTURE_RULE,
        BLANK_NOTE_RULE,
        LONG_NOTE_RULE,
        ENCODING_RULE,
        TOTAL_COST_RULE,
        "quantity must be greater than zero",
        "unit_cost must not be negative",
        *amount_rules("quantity"),
        *amount_rules("unit_cost"),
    }
)


class RecordingTrigger:
    """`after_change`, recording what had been **committed** each time it was called.

    It reads the table over a session of its own, which sees committed rows only. `error`
    makes it raise after recording, as a trigger that failed would.
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        error: BaseException | None = None,
    ) -> None:
        self.factory = factory
        self.error = error
        self.committed: list[list[dict[str, Any]]] = []
        self.logged_before: list[list[str]] = []
        self.captured: Sequence[Mapping[str, Any]] | None = None

    async def __call__(self) -> object:
        self.committed.append(await rows(self.factory, ADJUSTMENTS_SQL))
        if self.captured is not None:
            self.logged_before.append([str(entry["event"]) for entry in self.captured])
        if self.error is not None:
            raise self.error
        return None

    @property
    def calls(self) -> int:
        return len(self.committed)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with migrated_sessionmaker(tmp_path) as built:
        yield built


@pytest.fixture
def clock() -> SettableClock:
    return SettableClock(NOW)


@pytest.fixture
def trigger(factory: async_sessionmaker[AsyncSession]) -> RecordingTrigger:
    return RecordingTrigger(factory)


@pytest.fixture
async def owner(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return await plant_owner(session)


@pytest.fixture
async def stranger(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return await plant_owner(session, "stranger")


def draft(**changes: Any) -> AdjustmentDraft:
    """A valid draft -- 0.5 BTC at 30000, a year before `NOW` -- with `changes` applied."""
    fields: dict[str, Any] = {
        "asset": "BTC",
        "quantity": Decimal("0.5"),
        "unit_cost": Decimal(30000),
        "occurred_at": ACQUIRED,
        "note": "Opening balance before the imported history",
    }
    fields.update(changes)
    return AdjustmentDraft(**fields)


async def create(
    factory: async_sessionmaker[AsyncSession],
    user_id: int,
    entered: AdjustmentDraft,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> Any:
    async with factory() as session:
        service = build_adjustment_service(session, after_change=trigger, clock=clock)
        return await service.create(user_id, entered)


async def update(
    factory: async_sessionmaker[AsyncSession],
    user_id: int,
    adjustment_id: int,
    entered: AdjustmentDraft,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> Any:
    async with factory() as session:
        service = build_adjustment_service(session, after_change=trigger, clock=clock)
        return await service.update(user_id, adjustment_id, entered)


async def delete(
    factory: async_sessionmaker[AsyncSession],
    user_id: int,
    adjustment_id: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> None:
    async with factory() as session:
        service = build_adjustment_service(session, after_change=trigger, clock=clock)
        await service.delete(user_id, adjustment_id)


async def listed(
    factory: async_sessionmaker[AsyncSession],
    user_id: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> Any:
    async with factory() as session:
        service = build_adjustment_service(session, after_change=trigger, clock=clock)
        return await service.list(user_id)


def text_of(value: Decimal) -> str:
    return format(value, "f")


# --------------------------------------------------------------------------------------
# Criterion 1: create, list, replace, delete
# --------------------------------------------------------------------------------------


async def test_create_stores_and_returns_the_adjustment_as_stored(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> None:
    """Amounts at eighteen places, the instant in UTC, the note exactly as typed."""
    entered = draft(
        occurred_at=datetime(2025, 6, 1, 14, 0, tzinfo=timezone(timedelta(hours=2))),
        note="  Bought on a platform no longer imported  ",
    )

    view = await create(factory, owner, entered, trigger, clock)

    assert view.asset == "BTC"
    assert text_of(view.quantity) == "0.500000000000000000"
    assert view.unit_cost is not None
    assert text_of(view.unit_cost) == "30000.000000000000000000"
    assert view.occurred_at == ACQUIRED
    assert view.occurred_at.utcoffset() == timedelta(0)
    assert view.note == "  Bought on a platform no longer imported  "
    assert view.created_at == view.updated_at == NOW
    (row,) = await rows(factory, ADJUSTMENTS_SQL)
    assert row["id"] == view.id
    assert row["user_id"] == owner
    assert row["occurred_at"] == "2025-06-01 12:00:00.000000"
    assert row["note"] == "  Bought on a platform no longer imported  "


async def test_create_without_a_cost_stores_null_never_zero(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> None:
    view = await create(factory, owner, draft(unit_cost=None), trigger, clock)

    assert view.unit_cost is None
    (row,) = await rows(factory, ADJUSTMENTS_SQL)
    assert row["unit_cost"] is None


async def test_a_negative_zero_cost_is_stored_and_shown_as_zero(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> None:
    """`-0` is not below zero, so it is accepted; it is a cost of nothing, spelled once."""
    view = await create(factory, owner, draft(unit_cost=Decimal("-0")), trigger, clock)

    assert view.unit_cost is not None
    assert text_of(view.unit_cost) == "0.000000000000000000"
    (row,) = await rows(factory, ADJUSTMENTS_SQL)
    assert row["unit_cost"] == "0.000000000000000000"


async def test_list_orders_by_the_instant_then_the_id_and_shows_only_the_owners(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    stranger: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> None:
    """In Python, on the instants: 10:00 at +02:00 is 08:00Z, before 09:00Z, whatever the text.

    Two at one instant list by id, the order the engine replays them in. Listing is a read, so
    it never calls the trigger.
    """
    nine = await create(
        factory, owner, draft(occurred_at=datetime(2025, 6, 1, 9, 0, tzinfo=UTC)), trigger, clock
    )
    await create(
        factory, stranger, draft(occurred_at=datetime(2025, 1, 1, tzinfo=UTC)), trigger, clock
    )
    eight = await create(
        factory,
        owner,
        draft(occurred_at=datetime(2025, 6, 1, 10, 0, tzinfo=timezone(timedelta(hours=2)))),
        trigger,
        clock,
    )
    tie = await create(
        factory, owner, draft(occurred_at=datetime(2025, 6, 1, 9, 0, tzinfo=UTC)), trigger, clock
    )
    calls = trigger.calls

    found = await listed(factory, owner, trigger, clock)

    assert [view.id for view in found] == [eight.id, nine.id, tie.id]
    assert trigger.calls == calls


async def test_update_replaces_all_five_fields_and_keeps_the_creation_time(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> None:
    """A full replacement: `unit_cost=None` makes a known cost unknown."""
    created = await create(factory, owner, draft(), trigger, clock)
    clock.advance(timedelta(hours=1))
    replacement = draft(
        asset="KAS",
        quantity=Decimal(1000),
        unit_cost=None,
        occurred_at=ACQUIRED - timedelta(days=10),
        note="Corrected: it was KAS",
    )

    view = await update(factory, owner, created.id, replacement, trigger, clock)

    assert (view.id, view.asset, view.quantity, view.unit_cost) == (
        created.id,
        "KAS",
        Decimal(1000),
        None,
    )
    assert view.occurred_at == ACQUIRED - timedelta(days=10)
    assert view.note == "Corrected: it was KAS"
    assert view.created_at == NOW
    assert view.updated_at == NOW + timedelta(hours=1)
    (row,) = await rows(factory, ADJUSTMENTS_SQL)
    assert (row["asset"], row["unit_cost"], row["note"]) == ("KAS", None, "Corrected: it was KAS")


async def test_delete_removes_the_row_and_a_repeat_is_not_found(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> None:
    created = await create(factory, owner, draft(), trigger, clock)

    await delete(factory, owner, created.id, trigger, clock)

    assert await rows(factory, ADJUSTMENTS_SQL) == []
    calls = trigger.calls
    with pytest.raises(AdjustmentNotFoundError):
        await delete(factory, owner, created.id, trigger, clock)
    assert trigger.calls == calls, "a refused delete recomputes nothing"


@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_another_owners_adjustment_is_not_found_and_untouched(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    stranger: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
    operation: str,
) -> None:
    """The same error as a missing id, with the same fixed text, and the row stays as it was."""
    theirs = await create(factory, stranger, draft(), trigger, clock)
    before = await rows(factory, ADJUSTMENTS_SQL)
    calls = trigger.calls

    async def attempt(adjustment_id: int) -> None:
        if operation == "update":
            await update(factory, owner, adjustment_id, draft(asset="KAS"), trigger, clock)
        else:
            await delete(factory, owner, adjustment_id, trigger, clock)

    errors = []
    for adjustment_id in (theirs.id, theirs.id + 1000):
        with pytest.raises(AdjustmentNotFoundError) as raised:
            await attempt(adjustment_id)
        errors.append(str(raised.value))

    assert errors == [ADJUSTMENT_NOT_FOUND_DETAIL] * 2
    assert ADJUSTMENT_NOT_FOUND_DETAIL == "No adjustment with that id."
    assert await rows(factory, ADJUSTMENTS_SQL) == before
    assert trigger.calls == calls


async def test_a_refused_replacement_is_refused_before_the_id_is_looked_up(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> None:
    """A bad body on a missing id is a 422, never a 404: the body is judged first."""
    with pytest.raises(InvalidAdjustmentError) as raised:
        await update(factory, owner, 999_999, draft(note=" "), trigger, clock)

    assert raised.value.field == "note"


# --------------------------------------------------------------------------------------
# Criterion 5: the trigger runs once, after the commit; its failure never loses the change
# --------------------------------------------------------------------------------------


async def test_create_update_and_delete_each_trigger_once_after_the_commit(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> None:
    """What the trigger saw when it ran is what had been committed: the change, every time."""
    created = await create(factory, owner, draft(), trigger, clock)
    assert trigger.calls == 1
    assert [row["id"] for row in trigger.committed[0]] == [created.id]

    await update(factory, owner, created.id, draft(asset="KAS"), trigger, clock)
    assert trigger.calls == 2
    assert [row["asset"] for row in trigger.committed[1]] == ["KAS"]

    await delete(factory, owner, created.id, trigger, clock)
    assert trigger.calls == 3
    assert trigger.committed[2] == []


async def test_the_change_is_logged_before_the_trigger_runs(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> None:
    with capture_logs() as captured:
        trigger.captured = captured
        created = await create(factory, owner, draft(), trigger, clock)
        await update(factory, owner, created.id, draft(), trigger, clock)
        await delete(factory, owner, created.id, trigger, clock)

    assert [names[-1] for names in trigger.logged_before] == [
        "adjustment_created",
        "adjustment_updated",
        "adjustment_deleted",
    ]


async def test_a_refused_draft_writes_nothing_and_triggers_nothing(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> None:
    with pytest.raises(InvalidAdjustmentError):
        await create(factory, owner, draft(asset="usdt"), trigger, clock)

    assert await rows(factory, ADJUSTMENTS_SQL) == []
    assert trigger.calls == 0


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
async def test_a_failing_trigger_keeps_the_change_and_logs_the_class_only(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    clock: SettableClock,
    operation: str,
) -> None:
    """The change is saved and answered; the failure is logged by class name, with the id."""
    leaky = "trigger-message-" + "Wq8" * 4
    working = RecordingTrigger(factory)
    failing = RecordingTrigger(factory, error=RuntimeError(leaky))
    existing = await create(factory, owner, draft(), working, clock)

    with capture_logs() as captured:
        if operation == "create":
            result = await create(factory, owner, draft(asset="KAS"), failing, clock)
            touched = result.id
        elif operation == "update":
            result = await update(factory, owner, existing.id, draft(asset="KAS"), failing, clock)
            touched = existing.id
        else:
            await delete(factory, owner, existing.id, failing, clock)
            result = None
            touched = existing.id

    assert failing.calls == 1
    stored = await rows(factory, ADJUSTMENTS_SQL)
    if operation == "delete":
        assert result is None
        assert stored == []
    else:
        assert result.asset == "KAS"
        assert any(row["id"] == touched and row["asset"] == "KAS" for row in stored)
    (failure,) = [entry for entry in captured if entry["event"] == "adjustment_after_change_failed"]
    assert failure["log_level"] == "error"
    assert failure["adjustment_id"] == touched
    assert failure["error"] == "RuntimeError"
    assert leaky not in repr(captured)


async def test_a_cancelled_trigger_propagates_and_the_change_is_already_saved(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    clock: SettableClock,
) -> None:
    """A cancellation is not a failure to swallow: shutdown has to be able to stop a request."""
    cancelled = RecordingTrigger(factory, error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await create(factory, owner, draft(), cancelled, clock)

    assert len(await rows(factory, ADJUSTMENTS_SQL)) == 1


# --------------------------------------------------------------------------------------
# Criteria 6 and 7: the refusal matrix, and the accepted boundary beside each refusal
# --------------------------------------------------------------------------------------

#: The instant `datetime.min` names at +05:00 is five hours before any UTC `datetime`.
UNREPRESENTABLE: Final = datetime.min.replace(tzinfo=timezone(timedelta(hours=5)))

REFUSED: Final[tuple[tuple[str, dict[str, Any], str, str], ...]] = (
    ("lower case", {"asset": "btc"}, "asset", ASSET_RULE),
    ("mixed case", {"asset": "Btc"}, "asset", ASSET_RULE),
    ("empty asset", {"asset": ""}, "asset", ASSET_RULE),
    ("trailing space", {"asset": "BTC "}, "asset", ASSET_RULE),
    ("trailing newline", {"asset": "BTC\n"}, "asset", ASSET_RULE),
    ("twenty-one characters", {"asset": "A" * 21}, "asset", ASSET_RULE),
    ("a hyphen", {"asset": "BT-C"}, "asset", ASSET_RULE),
    ("a non-ASCII letter", {"asset": chr(0xC4) + "BC"}, "asset", ASSET_RULE),
    ("a lone surrogate asset", {"asset": "BTC\ud800"}, "asset", ASSET_RULE),
    ("cash USDT", {"asset": "USDT"}, "asset", CASH_RULE),
    ("cash USDC", {"asset": "USDC"}, "asset", CASH_RULE),
    ("naive", {"occurred_at": ACQUIRED.replace(tzinfo=None)}, "occurred_at", NAIVE_RULE),
    ("not representable in UTC", {"occurred_at": UNREPRESENTABLE}, "occurred_at", RANGE_RULE),
    (
        "a microsecond ahead",
        {"occurred_at": NOW + timedelta(microseconds=1)},
        "occurred_at",
        FUTURE_RULE,
    ),
    (
        "ahead at another offset",
        {"occurred_at": datetime(2026, 9, 29, 12, 1, tzinfo=timezone(timedelta(hours=2)))},
        "occurred_at",
        FUTURE_RULE,
    ),
    ("zero quantity", {"quantity": Decimal(0)}, "quantity", "quantity must be greater than zero"),
    (
        "negative quantity",
        {"quantity": Decimal(-1)},
        "quantity",
        "quantity must be greater than zero",
    ),
    ("quantity NaN", {"quantity": Decimal("NaN")}, "quantity", "quantity must be a finite number"),
    (
        "quantity past 18 places",
        {"quantity": Decimal("0.0000000000000000001")},
        "quantity",
        "quantity has more than 18 decimal places",
    ),
    (
        "quantity of 21 digits",
        {"quantity": Decimal("100000000000000000000")},
        "quantity",
        "quantity has more than 20 digits before the decimal point",
    ),
    ("negative cost", {"unit_cost": Decimal(-1)}, "unit_cost", "unit_cost must not be negative"),
    (
        "cost past 18 places",
        {"unit_cost": Decimal("0.0000000000000000001")},
        "unit_cost",
        "unit_cost has more than 18 decimal places",
    ),
    (
        "cost of 21 digits",
        {"unit_cost": Decimal("100000000000000000000")},
        "unit_cost",
        "unit_cost has more than 20 digits before the decimal point",
    ),
    (
        "total cost past the range",
        {"quantity": Decimal("10000000000"), "unit_cost": Decimal("10000000000")},
        "unit_cost",
        TOTAL_COST_RULE,
    ),
    ("empty note", {"note": ""}, "note", BLANK_NOTE_RULE),
    ("spaces", {"note": "     "}, "note", BLANK_NOTE_RULE),
    ("other whitespace", {"note": "\t\n" + chr(0xA0) + chr(0x3000)}, "note", BLANK_NOTE_RULE),
    ("501 characters", {"note": "n" * 501}, "note", LONG_NOTE_RULE),
    ("a lone surrogate note", {"note": "Opening \ud800 balance"}, "note", ENCODING_RULE),
)


@pytest.mark.parametrize(
    ("changes", "field", "rule"),
    [(changes, field, rule) for _name, changes, field, rule in REFUSED],
    ids=[name for name, *_rest in REFUSED],
)
async def test_each_rule_refuses_with_its_field_and_its_text_and_stores_nothing(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
    changes: dict[str, Any],
    field: str,
    rule: str,
) -> None:
    with pytest.raises(InvalidAdjustmentError) as raised:
        await create(factory, owner, draft(**changes), trigger, clock)

    error = raised.value
    assert (error.field, error.rule, str(error)) == (field, rule, rule)
    assert rule in KNOWN_RULES
    assert await rows(factory, ADJUSTMENTS_SQL) == []
    assert trigger.calls == 0
    for value in changes.values():
        spelled = value if isinstance(value, str) else str(value)
        if len(spelled.strip()) >= 3 and spelled not in {"BTC", "USDT", "USDC"}:
            assert spelled not in rule, "the refusal quotes the value it refused"


ACCEPTED: Final[tuple[tuple[str, dict[str, Any]], ...]] = (
    ("twenty characters", {"asset": "A" * 20}),
    ("letters and digits", {"asset": "X2Y2"}),
    ("exactly now", {"occurred_at": NOW}),
    (
        "now at another offset",
        {"occurred_at": datetime(2026, 9, 29, 12, 0, tzinfo=timezone(timedelta(hours=2)))},
    ),
    ("the smallest quantity", {"quantity": Decimal("0.000000000000000001")}),
    (
        "twenty integer digits",
        {"quantity": Decimal("99999999999999999999.999999999999999999"), "unit_cost": None},
    ),
    ("trailing zeros past 18 places", {"quantity": Decimal("1.50000000000000000000")}),
    ("a zero cost", {"unit_cost": Decimal(0)}),
    ("an unknown cost", {"unit_cost": None}),
    (
        "a total cost just inside",
        {"quantity": Decimal("9999999999"), "unit_cost": Decimal("10000000000")},
    ),
    ("500 characters", {"note": "n" * NOTE_MAX_LENGTH}),
    ("500 code points in 2000 bytes", {"note": "\U0001f4b0" * NOTE_MAX_LENGTH}),
    ("padded", {"note": "  a reason  "}),
)


@pytest.mark.parametrize(
    "changes", [changes for _name, changes in ACCEPTED], ids=[name for name, _ in ACCEPTED]
)
async def test_the_boundary_beside_each_refusal_is_accepted_and_converts(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
    changes: dict[str, Any],
) -> None:
    entered = draft(**changes)

    view = await create(factory, owner, entered, trigger, clock)

    assert view.note == entered.note
    async with factory() as session:
        records = await ManualAdjustmentRepository(session).list_adjustments_for_accounting(owner)
    (record,) = records
    event = adjustment_of(record)
    assert event.quantity == entered.quantity
    assert event.unit_cost == entered.unit_cost
    assert event.key.occurred_at == entered.occurred_at


async def test_the_checks_run_in_the_documented_order(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> None:
    """Asset, occurred_at, quantity, unit_cost, note: the first failure is the one reported."""
    everything_wrong: dict[str, Any] = {
        "asset": "btc",
        "occurred_at": NOW + timedelta(days=1),
        "quantity": Decimal(0),
        "unit_cost": Decimal(-1),
        "note": " ",
    }
    reported = []
    for fixed_field, good_value in (
        ("asset", "BTC"),
        ("occurred_at", ACQUIRED),
        ("quantity", Decimal(1)),
        ("unit_cost", Decimal(1)),
    ):
        with pytest.raises(InvalidAdjustmentError) as raised:
            await create(factory, owner, draft(**everything_wrong), trigger, clock)
        reported.append(raised.value.field)
        everything_wrong[fixed_field] = good_value
    with pytest.raises(InvalidAdjustmentError) as raised:
        await create(factory, owner, draft(**everything_wrong), trigger, clock)
    reported.append(raised.value.field)

    assert reported == ["asset", "occurred_at", "quantity", "unit_cost", "note"]


async def test_the_future_rule_reads_the_injected_clock(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> None:
    """Refused at `NOW`, accepted once the clock has passed it: the rule is not the wall clock."""
    later = NOW + timedelta(hours=1)
    with pytest.raises(InvalidAdjustmentError):
        await create(factory, owner, draft(occurred_at=later), trigger, clock)

    clock.advance(timedelta(hours=1))
    view = await create(factory, owner, draft(occurred_at=later), trigger, clock)

    assert view.occurred_at == later


def test_the_invalid_adjustment_error_pickles_with_both_fields() -> None:
    error = InvalidAdjustmentError("note", BLANK_NOTE_RULE)
    twin = pickle.loads(pickle.dumps(error))  # noqa: S301

    assert type(twin) is InvalidAdjustmentError
    assert (twin.field, twin.rule, str(twin)) == ("note", BLANK_NOTE_RULE, BLANK_NOTE_RULE)


# --------------------------------------------------------------------------------------
# Criterion 9: the log lines carry the id and nothing else
# --------------------------------------------------------------------------------------


async def test_each_change_logs_its_id_and_nothing_the_owner_typed(
    factory: async_sessionmaker[AsyncSession],
    owner: int,
    trigger: RecordingTrigger,
    clock: SettableClock,
) -> None:
    """The structured fields, exactly. `tests/security/test_adjustment_logging.py` reads stdout."""
    with capture_logs() as captured:
        created = await create(factory, owner, draft(), trigger, clock)
        await update(factory, owner, created.id, draft(note="Edited"), trigger, clock)
        await delete(factory, owner, created.id, trigger, clock)

    changes = [entry for entry in captured if str(entry["event"]).startswith("adjustment_")]
    assert changes == [
        {"event": "adjustment_created", "log_level": "info", "adjustment_id": created.id},
        {"event": "adjustment_updated", "log_level": "info", "adjustment_id": created.id},
        {"event": "adjustment_deleted", "log_level": "info", "adjustment_id": created.id},
    ]


# --------------------------------------------------------------------------------------
# The event: `manual`, and the id padded to twenty digits
# --------------------------------------------------------------------------------------


def test_the_event_source_and_the_padding_are_the_specs() -> None:
    assert ADJUSTMENT_SOURCE == "manual"
    assert external_id_of(9) == "00000000000000000009"
    assert external_id_of(10) == "00000000000000000010"
    assert external_id_of(9) < external_id_of(10), "text order is numeric order"
    assert external_id_of(2**63 - 1) == "09223372036854775807", "SQLite's largest id fits"


@given(first=st.integers(0, 2**63 - 1), second=st.integers(0, 2**63 - 1))
def test_the_padded_id_sorts_as_the_id_does(first: int, second: int) -> None:
    """For every id SQLite can assign, text order and numeric order agree."""
    assert len(external_id_of(first)) == 20
    assert (external_id_of(first) < external_id_of(second)) == (first < second)


def test_both_import_paths_name_the_same_objects() -> None:
    """`services.adjustments` re-exports what `services.accounting` defines, not a copy."""
    assert adjustment_of is accounting_module.adjustment_of
    assert external_id_of is accounting_module.external_id_of
    assert ADJUSTMENT_SOURCE == accounting_module.ADJUSTMENT_SOURCE
    assert UnconvertibleAdjustmentError is accounting_module.UnconvertibleAdjustmentError


def test_a_record_converts_to_the_manual_event() -> None:
    record = AdjustmentRecord(
        id=42, asset="KAS", quantity=Decimal(1000), unit_cost=None, occurred_at=ACQUIRED
    )

    assert adjustment_of(record) == Adjustment(
        key=EventKey(ACQUIRED, "manual", "00000000000000000042"),
        asset="KAS",
        quantity=Decimal(1000),
        unit_cost=None,
    )


def test_a_record_that_does_not_convert_raises_with_its_id_only() -> None:
    record = AdjustmentRecord(
        id=77, asset="KAS", quantity=Decimal(0), unit_cost=None, occurred_at=ACQUIRED
    )

    with pytest.raises(UnconvertibleAdjustmentError) as raised:
        adjustment_of(record)

    assert raised.value.adjustment_id == 77
    assert "77" not in str(raised.value)
    assert "KAS" not in str(raised.value)


# --------------------------------------------------------------------------------------
# Criterion 7's property: every accepted draft converts once stored
# --------------------------------------------------------------------------------------

VALID_ASSETS: Final = ("BTC", "KAS", "ETH", "X2Y2", "A" * 20)
INVALID_ASSETS: Final = ("btc", "Btc", "USDT", "USDC", "", "BTC ", "A" * 21, "BTC\n", "BT-C")
OFFSETS: Final = (UTC, timezone(timedelta(hours=5)), timezone(-timedelta(hours=3, minutes=30)))


def scaled(units: int, exponent: int) -> Decimal:
    return Decimal(units).scaleb(exponent)


#: Amounts the rule accepts: up to nine digits, at 0, 2, 8 or 18 places.
VALID_AMOUNTS: Final = st.builds(scaled, st.integers(1, 10**9), st.sampled_from([0, -2, -8, -18]))

#: Every other region: zero and negatives, too many places, too many digits, the specials.
EDGE_AMOUNTS: Final = st.one_of(
    st.builds(
        scaled,
        st.one_of(st.integers(-10, 10**6), st.integers(0, 10**22)),
        st.sampled_from([0, -2, -8, -18, -19, -20, 2]),
    ),
    st.sampled_from(
        [
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-0"),
            Decimal("1.50000000000000000000"),
            Decimal("0.000000000000000001"),
            Decimal("99999999999999999999.999999999999999999"),
        ]
    ),
)

#: Weighted two to one towards what is accepted, so the accepted half is not a handful.
AMOUNTS: Final = st.one_of(VALID_AMOUNTS, VALID_AMOUNTS, EDGE_AMOUNTS)

PAST: Final = st.datetimes(
    min_value=datetime(2000, 1, 1, tzinfo=UTC).replace(tzinfo=None),
    max_value=datetime(2026, 9, 28, tzinfo=UTC).replace(tzinfo=None),
    timezones=st.sampled_from(OFFSETS),
)

INSTANTS: Final = st.one_of(
    PAST,
    PAST,
    st.datetimes(
        min_value=datetime(2026, 9, 28, tzinfo=UTC).replace(tzinfo=None),
        max_value=datetime(2026, 10, 2, tzinfo=UTC).replace(tzinfo=None),
        timezones=st.sampled_from(OFFSETS),
    ),
    st.datetimes(
        min_value=datetime(2020, 1, 1, tzinfo=UTC).replace(tzinfo=None),
        max_value=datetime(2026, 9, 28, tzinfo=UTC).replace(tzinfo=None),
    ),
    st.just(UNREPRESENTABLE),
    st.just(NOW),
)

#: Notes the rule accepts: letters, digits, punctuation and spaces, not only spaces.
REASONS: Final = st.text(
    alphabet=st.characters(categories=["L", "N", "P", "Zs"]), min_size=1, max_size=60
).filter(str.strip)

NOTES: Final = st.one_of(
    REASONS,
    REASONS,
    st.text(max_size=40),
    st.sampled_from(
        [
            "",
            "   ",
            chr(9) + chr(10),
            "n" * 500,
            "n" * 501,
            "Opening " + chr(0xD800),
            " kept as typed ",
        ]
    ),
)

DRAFTS: Final = st.builds(
    AdjustmentDraft,
    asset=st.one_of(
        st.sampled_from(VALID_ASSETS),
        st.sampled_from(VALID_ASSETS),
        st.sampled_from(INVALID_ASSETS),
    ),
    quantity=AMOUNTS,
    unit_cost=st.one_of(st.none(), AMOUNTS),
    occurred_at=INSTANTS,
    note=NOTES,
)


def test_every_accepted_draft_converts_once_stored(tmp_path: Path) -> None:
    """Spec 023: a property test holds that every adjustment the API accepts converts.

    One database and one event loop for every example, so that the run is about the drafts
    and not about opening files; each example goes through the real service and the real
    table. After the run, both outcomes must have been reached, or the property held of
    nothing.
    """
    outcomes = {"accepted": 0, "refused": 0}

    with asyncio.Runner() as runner:
        opened = migrated_sessionmaker(tmp_path)
        built = runner.run(opened.__aenter__())
        try:
            user_id = runner.run(_owner_of(built))

            @settings(
                max_examples=300,
                deadline=None,
                suppress_health_check=[HealthCheck.too_slow],
            )
            @given(entered=DRAFTS)
            def holds(entered: AdjustmentDraft) -> None:
                outcome = runner.run(_accepted_converts(built, user_id, entered))
                event(outcome)
                outcomes[outcome] += 1

            holds()
        finally:
            runner.run(opened.__aexit__(None, None, None))

    assert outcomes["accepted"] >= 10, outcomes
    assert outcomes["refused"] >= 10, outcomes


async def _owner_of(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return await plant_owner(session)


async def _accepted_converts(
    factory: async_sessionmaker[AsyncSession], user_id: int, entered: AdjustmentDraft
) -> str:
    """One example: accepted and converting, or refused by a known rule with nothing written."""

    async def nothing() -> object:
        return None

    stored_before = len(await rows(factory, ADJUSTMENTS_SQL))
    async with factory() as session:
        service = build_adjustment_service(session, after_change=nothing, clock=lambda: NOW)
        refusal: InvalidAdjustmentError | None = None
        try:
            view = await service.create(user_id, entered)
        except InvalidAdjustmentError as error:
            refusal = error
    if refusal is not None:
        assert refusal.field in FIELDS
        assert refusal.rule in KNOWN_RULES, refusal.rule
        assert len(await rows(factory, ADJUSTMENTS_SQL)) == stored_before
        return "refused"
    async with factory() as session:
        records = await ManualAdjustmentRepository(session).list_adjustments_for_accounting(user_id)
    (record,) = [record for record in records if record.id == view.id]
    event = adjustment_of(record)
    assert event.key == EventKey(entered.occurred_at, "manual", external_id_of(view.id))
    assert event.asset == entered.asset
    assert event.quantity == entered.quantity
    assert event.unit_cost == entered.unit_cost
    return "accepted"
