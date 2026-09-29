"""A second opinion on spec 019, in exact rational arithmetic.

The engine under test (`portfolio.domain.accounting.replay`) keeps its pools in `Decimal`
through `money.add`, `money.subtract`, `money.multiply` and `money.divide`. This module keeps
them in `fractions.Fraction` and rounds only where the spec says rounding happens:

* `divide`, once, half to even, to 18 places -- the spec's *Arithmetic* section;
* an `Adjustment`'s cost, `quantize(unit_cost * quantity, 18)`, the one exception it names.

Everything else is exact. Complements are subtractions of exact rationals, so the oracle's
answer differs from the engine's only if one of them rounds somewhere the spec does not, or
fails to round somewhere it does -- which is exactly the class of bug a second implementation
written from the same arithmetic would share.

**Independent on purpose.** Nothing here imports `portfolio.domain.accounting`, and the event
types are this module's own: the golden files are generated from `golden/scenario.json` by
this code alone (`python -m tests.domain.accounting.oracle`), before and without the engine.
Where the spec leaves a choice open, the choice is written down at the point it is made, so a
disagreement with the engine can be settled against the spec text rather than against
whichever implementation happened to be written first.

Where the spec's first draft was silent, this oracle surfaced the question and the spec's
"Rulings during implementation" answered it. The ones encoded here:

* **R1** `average_cost` is `None` at `Qk == 0` and when the quotient has 21 or more integer
  digits. Replay never raises for it.
* **R3** `UnattributedFee.charged_to` is the received asset for a buy or a swap, the given
  asset for a sale, and `None` for a conversion.
* **R4** Inside one trade: given leg, then third-asset fee leg, then received leg. That fixes
  the order of the warnings and lots a single event emits.
* **R5** `Lot.quantity` is the whole quantity acquired, known and unknown together.
* **R6** `A` in I8 counts adjustments of non-cash assets only.
* **R8** A zero fee creates no leg and touches no position, whatever asset it names.
* **R11** A swap splits its fee as a sale does: the known share, `fee x known_out / given`,
  joins the received cost, and the complement goes to `unallocated_costs`.

Not computed: the input fingerprint. The spec fixes what it covers but not the JSON key
names, so there is nothing independent to compute it from. `test_fingerprint.py` tests its
properties instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

SCALE: Final = 18
"""`QUANTITY_SCALE`, `BASIS_SCALE` and `AVERAGE_COST_SCALE` are all 18 in the spec."""

UNIT: Final = 10**SCALE
MAX_INTEGER_DIGITS: Final = 20
"""`MONEY_PRECISION - 18`: a quotient at 18 places with more integer digits than this raises."""

TRADE: Final = "trade"
ADJUSTMENT: Final = "adjustment"
TRANSFER: Final = "transfer"

UNKNOWN_BASIS: Final = "UNKNOWN_BASIS"
HISTORY_INCOMPLETE: Final = "HISTORY_INCOMPLETE"
UNATTRIBUTED_FEE: Final = "UNATTRIBUTED_FEE"

GOLDEN_DIRECTORY: Final = Path(__file__).resolve().parent / "golden"
SCENARIO_PATH: Final = GOLDEN_DIRECTORY / "scenario.json"
EXPECTED_PATH: Final = GOLDEN_DIRECTORY / "expected.json"

Side = Literal["buy", "sell"]
ZERO: Final = Fraction(0)


class OracleOverflowError(ArithmeticError):
    """A value the engine's 38-digit money cannot hold, so the spec says it raises."""


class OracleConflictError(ValueError):
    """Two events share an identity and differ in content: the spec's `ConflictingEventError`."""


# --------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Key:
    occurred_at: datetime
    source: str
    external_id: str

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            message = "the oracle only takes aware datetimes"
            raise ValueError(message)
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))


@dataclass(frozen=True)
class Trade:
    key: Key
    base_asset: str
    quote_asset: str
    side: Side
    quantity: Fraction
    quote_quantity: Fraction
    fee_amount: Fraction
    fee_asset: str | None

    @property
    def kind(self) -> str:
        return TRADE


@dataclass(frozen=True)
class Adjustment:
    key: Key
    asset: str
    quantity: Fraction
    unit_cost: Fraction | None

    @property
    def kind(self) -> str:
        return ADJUSTMENT


@dataclass(frozen=True)
class Transfer:
    key: Key
    asset: str
    quantity: Fraction
    from_location: str
    to_location: str

    @property
    def kind(self) -> str:
        return TRANSFER


Event = Trade | Adjustment | Transfer


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Position:
    asset: str
    quantity: Fraction
    unknown_basis_quantity: Fraction
    cost_basis: Fraction
    average_cost: Fraction | None
    realized_pnl: Fraction
    unmatched_proceeds: Fraction
    flags: frozenset[str]


@dataclass(frozen=True)
class NegativeInventory:
    key: Key
    asset: str
    shortfall: Fraction


@dataclass(frozen=True)
class UnattributedFee:
    key: Key
    fee_asset: str
    quantity: Fraction
    charged_to: str | None


ReplayWarning = NegativeInventory | UnattributedFee


@dataclass(frozen=True)
class Lot:
    asset: str
    key: Key
    quantity: Fraction
    cost_basis: Fraction
    unknown_basis_quantity: Fraction


@dataclass(frozen=True)
class Result:
    positions: tuple[Position, ...]
    warnings: tuple[ReplayWarning, ...]
    lots: tuple[Lot, ...]
    unallocated_costs: Fraction
    event_count: int

    def position(self, asset: str) -> Position:
        matches = [position for position in self.positions if position.asset == asset]
        assert len(matches) == 1, f"{asset} has {len(matches)} positions"
        return matches[0]


# --------------------------------------------------------------------------------------
# The one rounding
# --------------------------------------------------------------------------------------


def round_half_even(value: Fraction, scale: int = SCALE) -> Fraction:
    """`value` rounded once, half to even, to `scale` places. Exact, context free."""
    scaled = value * 10**scale
    floor = scaled.numerator // scaled.denominator
    remainder = scaled - floor
    half = Fraction(1, 2)
    if remainder > half or (remainder == half and floor % 2 == 1):
        floor += 1
    return Fraction(floor, 10**scale)


def require_representable(value: Fraction) -> Fraction:
    """Refuse a value with more than 20 integer digits, as the engine's `divide` does."""
    if abs(value) >= 10**MAX_INTEGER_DIGITS:
        message = "a quotient beyond MONEY_PRECISION"
        raise OracleOverflowError(message)
    return value


def divide(dividend: Fraction, divisor: Fraction) -> Fraction:
    """The spec's `money.divide(dividend, divisor, 18)`."""
    if divisor == 0:
        message = "divide by zero"
        raise ZeroDivisionError(message)
    return require_representable(round_half_even(dividend / divisor))


# --------------------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------------------


def identity(event: Event) -> tuple[str, str, str]:
    """`(kind, source, external_id)`: kind is part of it, per *Ordering and identity*."""
    return (event.kind, event.key.source, event.key.external_id)


def replay_order(event: Event) -> tuple[datetime, str, str, str]:
    """`(occurred_at, source, external_id, kind)`, with the id compared as a plain string."""
    return (event.key.occurred_at, event.key.source, event.key.external_id, event.kind)


def deduplicate(events: Iterable[Event]) -> list[Event]:
    """I6: a repeated identity with equal content counts once; unequal content raises."""
    seen: dict[tuple[str, str, str], Event] = {}
    for event in events:
        known = seen.get(identity(event))
        if known is None:
            seen[identity(event)] = event
        elif known != event:
            message = "one identity, two contents"
            raise OracleConflictError(message)
    return list(seen.values())


def ordered(events: Iterable[Event]) -> list[Event]:
    """The events replay sees, in the order it sees them."""
    return sorted(deduplicate(events), key=replay_order)


@dataclass
class _Pool:
    known_quantity: Fraction = ZERO
    basis: Fraction = ZERO
    unknown_quantity: Fraction = ZERO
    realized: Fraction = ZERO
    unmatched: Fraction = ZERO
    sticky_flags: set[str] = field(default_factory=set)


class _Book:
    def __init__(self, cash_assets: frozenset[str]) -> None:
        self.cash = cash_assets
        self.pools: dict[str, _Pool] = {}
        self.warnings: list[ReplayWarning] = []
        self.lots: list[Lot] = []
        self.unallocated = ZERO

    def pool(self, asset: str) -> _Pool:
        assert asset not in self.cash, f"{asset} is cash and has no pool"
        return self.pools.setdefault(asset, _Pool())

    def acquire(self, asset: str, known: Fraction, basis: Fraction, unknown: Fraction) -> None:
        pool = self.pool(asset)
        pool.known_quantity += known
        pool.basis += basis
        pool.unknown_quantity += unknown

    def dispose(
        self, asset: str, amount: Fraction, key: Key
    ) -> tuple[Fraction, Fraction, Fraction]:
        """`(known_out, basis_out, uncovered)`, exactly as *Pool operations* defines it."""
        assert amount > 0
        pool = self.pool(asset)
        total = pool.known_quantity + pool.unknown_quantity
        if amount >= total:
            known_out, basis_out = pool.known_quantity, pool.basis
            pool.known_quantity = pool.basis = pool.unknown_quantity = ZERO
            shortfall = amount - total
            if shortfall > 0:
                self.warnings.append(NegativeInventory(key, asset, shortfall))
                pool.sticky_flags.add(HISTORY_INCOMPLETE)
        else:
            known_out = (
                amount
                if pool.unknown_quantity == 0
                else divide(amount * pool.known_quantity, total)
            )
            basis_out = (
                pool.basis
                if known_out == pool.known_quantity
                else divide(pool.basis * known_out, pool.known_quantity)
            )
            pool.known_quantity -= known_out
            pool.basis -= basis_out
            pool.unknown_quantity -= amount - known_out
        return known_out, basis_out, amount - known_out

    def lot(
        self, asset: str, key: Key, quantity: Fraction, cost: Fraction, unknown: Fraction
    ) -> None:
        self.lots.append(Lot(asset, key, quantity, cost, unknown))

    def apply(self, event: Event) -> None:
        if isinstance(event, Trade):
            self.trade(event)
        elif isinstance(event, Adjustment):
            self.adjustment(event)
        # A Transfer changes no position and creates none.

    def adjustment(self, event: Adjustment) -> None:
        if event.asset in self.cash:
            return  # "An Adjustment of a cash asset is accepted and changes nothing."
        self.pool(event.asset)
        if event.unit_cost is None:
            self.acquire(event.asset, ZERO, ZERO, event.quantity)
            self.lot(event.asset, event.key, event.quantity, ZERO, event.quantity)
            return
        cost = require_representable(round_half_even(event.unit_cost * event.quantity))
        self.acquire(event.asset, event.quantity, cost, ZERO)
        self.lot(event.asset, event.key, event.quantity, cost, ZERO)

    def trade(self, event: Trade) -> None:
        key = event.key
        if event.side == "buy":
            received_asset, received = event.base_asset, event.quantity
            given_asset, given = event.quote_asset, event.quote_quantity
        else:
            received_asset, received = event.quote_asset, event.quote_quantity
            given_asset, given = event.base_asset, event.quantity
        fee_leg: tuple[str, Fraction] | None = None
        # R8: a fee asset named beside a zero fee creates no leg and touches no position.
        if event.fee_asset is not None and event.fee_amount != 0:
            if event.fee_asset == received_asset:
                received -= event.fee_amount
            elif event.fee_asset == given_asset:
                given += event.fee_amount
            else:
                fee_leg = (event.fee_asset, event.fee_amount)
        assert received > 0
        assert given > 0

        given_is_cash = given_asset in self.cash
        received_is_cash = received_asset in self.cash
        for asset in (given_asset, received_asset, *(fee_leg[:1] if fee_leg else ())):
            if asset not in self.cash:
                self.pool(asset)  # "touched by a Trade leg": the position exists from here
        charged_to: str | None
        if given_is_cash and received_is_cash:
            charged_to = None
        else:
            charged_to = received_asset if not received_is_cash else given_asset

        # 1. The given leg.
        known_out = basis_out = uncovered = ZERO
        if not given_is_cash:
            known_out, basis_out, uncovered = self.dispose(given_asset, given, key)

        # 2. The third-asset fee leg.
        fee_value = ZERO
        if fee_leg is not None:
            fee_asset, fee = fee_leg
            if fee_asset in self.cash:
                fee_value = fee
            elif fee > 0:
                _, fee_basis, fee_uncovered = self.dispose(fee_asset, fee, key)
                fee_value = fee_basis
                if fee_uncovered > 0:
                    self.warnings.append(UnattributedFee(key, fee_asset, fee_uncovered, charged_to))
                    if charged_to is not None:
                        self.pool(charged_to).sticky_flags.add(UNATTRIBUTED_FEE)
            else:
                self.acquire(fee_asset, ZERO, ZERO, -fee)
                self.lot(fee_asset, key, -fee, ZERO, -fee)

        # 3. The shape.
        if given_is_cash and received_is_cash:
            fee_in_cash = event.fee_asset is not None and event.fee_asset in self.cash
            self.unallocated += event.fee_amount if fee_in_cash else fee_value
        elif given_is_cash:
            value = given + fee_value
            self.acquire(received_asset, received, value, ZERO)
            self.lot(received_asset, key, received, value, ZERO)
        elif received_is_cash:
            proceeds = received - fee_value
            proceeds_known = proceeds if uncovered == 0 else divide(proceeds * known_out, given)
            pool = self.pool(given_asset)
            pool.realized += proceeds_known - basis_out
            pool.unmatched += proceeds - proceeds_known
        else:
            known_in = received if uncovered == 0 else divide(received * known_out, given)
            unknown_in = received - known_in
            if known_in > 0:
                # R11: the fee splits as a sale's proceeds do. The known share joins the
                # received cost; the rest belongs to units of unknown cost, so it is known
                # value with no known quantity to attach to.
                fee_known = fee_value if uncovered == 0 else divide(fee_value * known_out, given)
                cost = basis_out + fee_known
                self.acquire(received_asset, known_in, cost, unknown_in)
                self.lot(received_asset, key, received, cost, unknown_in)
                self.unallocated += fee_value - fee_known
            else:
                # The limit of the same rule: nothing known arrives, so all of it is unallocated.
                self.acquire(received_asset, ZERO, ZERO, received)
                self.unallocated += basis_out + fee_value
                self.lot(received_asset, key, received, ZERO, received)

    def result(self, event_count: int) -> Result:
        positions = []
        for asset in sorted(self.pools):
            pool = self.pools[asset]
            flags = set(pool.sticky_flags)
            if pool.unknown_quantity > 0:
                flags.add(UNKNOWN_BASIS)
            average = _average(pool)
            positions.append(
                Position(
                    asset=asset,
                    quantity=pool.known_quantity + pool.unknown_quantity,
                    unknown_basis_quantity=pool.unknown_quantity,
                    cost_basis=pool.basis,
                    average_cost=average,
                    realized_pnl=pool.realized,
                    unmatched_proceeds=pool.unmatched,
                    flags=frozenset(flags),
                )
            )
        return Result(
            positions=tuple(positions),
            warnings=tuple(self.warnings),
            lots=tuple(self.lots),
            unallocated_costs=self.unallocated,
            event_count=event_count,
        )


def _average(pool: _Pool) -> Fraction | None:
    """R1: `None` at `Qk == 0`, and `None` when the rounded quotient has 21+ integer digits."""
    if pool.known_quantity == 0:
        return None
    try:
        return divide(pool.basis, pool.known_quantity)
    except OracleOverflowError:
        return None


def replay(events: Iterable[Event], cash_assets: Iterable[str]) -> Result:
    """The spec's `replay`, independently."""
    cash = frozenset(cash_assets)
    sequence = ordered(events)
    book = _Book(cash)
    for event in sequence:
        book.apply(event)
    return book.result(len(sequence))


# --------------------------------------------------------------------------------------
# Conservation: N + A, from the events alone
# --------------------------------------------------------------------------------------


def invested(events: Iterable[Event], cash_assets: Iterable[str]) -> Fraction:
    """Criterion 9's `N + A`, computed from the events and never from a pool.

    `N` is the net cash the trades put in. For a trade with a non-cash leg: the cash given
    after the fee fold, plus a third-asset fee paid in cash (signed), minus the cash received
    after the fee fold. For a conversion: only a fee paid in any cash asset. `A` is the known
    cost of the `Adjustment`s of non-cash assets -- an adjustment of cash changes nothing.
    """
    cash = frozenset(cash_assets)
    total = ZERO
    for event in ordered(events):
        if isinstance(event, Adjustment):
            if event.asset not in cash and event.unit_cost is not None:
                total += round_half_even(event.unit_cost * event.quantity)
            continue
        if isinstance(event, Transfer):
            continue
        if event.side == "buy":
            received_asset, received = event.base_asset, event.quantity
            given_asset, given = event.quote_asset, event.quote_quantity
        else:
            received_asset, received = event.quote_asset, event.quote_quantity
            given_asset, given = event.base_asset, event.quantity
        third_cash_fee = ZERO
        if event.fee_asset is not None:
            if event.fee_asset == received_asset:
                received -= event.fee_amount
            elif event.fee_asset == given_asset:
                given += event.fee_amount
            elif event.fee_asset in cash:
                third_cash_fee = event.fee_amount
        if given_asset in cash and received_asset in cash:
            if event.fee_asset is not None and event.fee_asset in cash:
                total += event.fee_amount
            continue
        if given_asset in cash:
            total += given
        total += third_cash_fee
        if received_asset in cash:
            total -= received
    return total


# --------------------------------------------------------------------------------------
# Decimal and JSON
# --------------------------------------------------------------------------------------


def fraction(value: Decimal | str) -> Fraction:
    """An exact rational from a decimal amount."""
    return Fraction(Decimal(value) if isinstance(value, str) else value)


def to_decimal(value: Fraction) -> Decimal:
    """The exact decimal an oracle value spells. Every value here is on the 18-place grid.

    Asserted rather than assumed: a value off the grid means the oracle divided somewhere
    without rounding, which is a bug in the oracle, not a property of the engine.
    """
    assert UNIT % value.denominator == 0, f"{value} is not on the 18-place grid"
    units = value.numerator * (UNIT // value.denominator)
    sign = 1 if units < 0 else 0
    digits = tuple(int(character) for character in str(abs(units)))
    return Decimal((sign, digits, -SCALE))


def render(value: Fraction | None) -> str | None:
    """Fixed notation at 18 places, `None` kept as `None`."""
    return None if value is None else format(to_decimal(value), "f")


def parse_time(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    assert moment.tzinfo is not None, text
    return moment.astimezone(UTC)


def render_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def event_from_json(document: Mapping[str, object]) -> Event:
    """One scenario row. Amounts are JSON strings, never numbers, as over the wire."""

    def text(name: str) -> str:
        value = document[name]
        assert isinstance(value, str), name
        return value

    def amount(name: str) -> Fraction:
        return fraction(text(name))

    key = Key(parse_time(text("occurred_at")), text("source"), text("external_id"))
    kind = text("kind")
    if kind == TRADE:
        side = text("side")
        assert side in ("buy", "sell"), side
        fee_asset = document.get("fee_asset")
        assert fee_asset is None or isinstance(fee_asset, str)
        return Trade(
            key=key,
            base_asset=text("base_asset"),
            quote_asset=text("quote_asset"),
            side="buy" if side == "buy" else "sell",
            quantity=amount("quantity"),
            quote_quantity=amount("quote_quantity"),
            fee_amount=amount("fee_amount"),
            fee_asset=fee_asset,
        )
    if kind == ADJUSTMENT:
        unit_cost = document.get("unit_cost")
        assert unit_cost is None or isinstance(unit_cost, str)
        return Adjustment(
            key=key,
            asset=text("asset"),
            quantity=amount("quantity"),
            unit_cost=None if unit_cost is None else fraction(unit_cost),
        )
    assert kind == TRANSFER, kind
    return Transfer(
        key=key,
        asset=text("asset"),
        quantity=amount("quantity"),
        from_location=text("from_location"),
        to_location=text("to_location"),
    )


def load_scenario(path: Path = SCENARIO_PATH) -> tuple[frozenset[str], list[Event]]:
    """`(cash_assets, events)` from a scenario file, rows in file order."""
    document = json.loads(path.read_text(encoding="utf-8"))
    cash = frozenset(document["cash_assets"])
    return cash, [event_from_json(row) for row in document["events"]]


def key_to_json(key: Key) -> dict[str, str]:
    return {
        "occurred_at": render_time(key.occurred_at),
        "source": key.source,
        "external_id": key.external_id,
    }


def warning_to_json(warning: ReplayWarning) -> dict[str, object]:
    if isinstance(warning, NegativeInventory):
        return {
            "type": "negative_inventory",
            "key": key_to_json(warning.key),
            "asset": warning.asset,
            "shortfall": render(warning.shortfall),
        }
    return {
        "type": "unattributed_fee",
        "key": key_to_json(warning.key),
        "fee_asset": warning.fee_asset,
        "quantity": render(warning.quantity),
        "charged_to": warning.charged_to,
    }


def result_to_json(result: Result) -> dict[str, object]:
    """The golden file's shape: every field but the fingerprint, amounts as strings."""
    return {
        "event_count": result.event_count,
        "unallocated_costs": render(result.unallocated_costs),
        "positions": [
            {
                "asset": position.asset,
                "quantity": render(position.quantity),
                "unknown_basis_quantity": render(position.unknown_basis_quantity),
                "cost_basis": render(position.cost_basis),
                "average_cost": render(position.average_cost),
                "realized_pnl": render(position.realized_pnl),
                "unmatched_proceeds": render(position.unmatched_proceeds),
                "flags": sorted(position.flags),
            }
            for position in result.positions
        ],
        "warnings": [warning_to_json(warning) for warning in result.warnings],
        "lots": [
            {
                "asset": lot.asset,
                "key": key_to_json(lot.key),
                "quantity": render(lot.quantity),
                "cost_basis": render(lot.cost_basis),
                "unknown_basis_quantity": render(lot.unknown_basis_quantity),
            }
            for lot in result.lots
        ],
    }


def expected_document() -> str:
    """`golden/expected.json` as the oracle computes it from `golden/scenario.json`."""
    cash, events = load_scenario()
    return json.dumps(result_to_json(replay(events, cash)), indent=2, sort_keys=False) + "\n"


if __name__ == "__main__":
    # Regenerates the golden expectation from the scenario, with the oracle only.
    EXPECTED_PATH.write_text(expected_document(), encoding="utf-8")
