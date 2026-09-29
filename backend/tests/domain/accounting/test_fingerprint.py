"""I3's fingerprint: equal inputs digest equally, and any single change digests differently.

#19 skips a recompute when the fingerprint is unchanged, so the two failure modes cost
differently. A fingerprint that changes for an equal input costs a recompute. One that
**stays the same for a different input leaves a wrong snapshot in place for good**, which
is why most of this module is field sensitivity.

Per R10 the JSON key names are the engine's, so nothing here recomputes a digest: every
test compares two fingerprints the engine produced. The property tests in
`test_invariants.py` add permutations and duplicates over generated histories.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest

from portfolio.domain.accounting import (
    ENGINE_VERSION,
    METHOD,
    AccountingConfig,
    replay,
)
from tests.domain.accounting.support import CONFIG, adjust, buy, key, move, sell

if TYPE_CHECKING:
    from collections.abc import Sequence

    from portfolio.domain.accounting.events import AccountingEvent

BASE_KEY: Final = key(10, "e1")


def fingerprint(*events: AccountingEvent, config: AccountingConfig = CONFIG) -> str:
    return replay(list(events), config).input_fingerprint


# The base events, and one variant per field. Every variant is a valid event of the same
# kind that differs from the base in exactly that field.
BASE_TRADE: Final = buy(BASE_KEY, "BTC", "USDT", "1", "30000", "30", "USDT")
TRADE_VARIANTS: Final[dict[str, AccountingEvent]] = {
    "occurred_at": buy(
        key(datetime(2026, 1, 1, 10, 0, 0, 1, tzinfo=UTC), "e1"),
        "BTC",
        "USDT",
        "1",
        "30000",
        "30",
        "USDT",
    ),
    "source": buy(key(10, "e1", "bingx"), "BTC", "USDT", "1", "30000", "30", "USDT"),
    "external_id": buy(key(10, "e2"), "BTC", "USDT", "1", "30000", "30", "USDT"),
    "base_asset": buy(BASE_KEY, "ETH", "USDT", "1", "30000", "30", "USDT"),
    "quote_asset": buy(BASE_KEY, "BTC", "USDC", "1", "30000", "30", "USDT"),
    "side": sell(BASE_KEY, "BTC", "USDT", "1", "30000", "30", "USDT"),
    "quantity": buy(BASE_KEY, "BTC", "USDT", "1.000000000000000001", "30000", "30", "USDT"),
    "quote_quantity": buy(BASE_KEY, "BTC", "USDT", "1", "30000.000000000000000001", "30", "USDT"),
    "fee_amount": buy(BASE_KEY, "BTC", "USDT", "1", "30000", "-30", "USDT"),
    "fee_asset": buy(BASE_KEY, "BTC", "USDT", "1", "30000", "30", "USDC"),
}

BASE_ADJUSTMENT: Final = adjust(key(9, "m1", "manual"), "ETH", "2", "2500")
ADJUSTMENT_VARIANTS: Final[dict[str, AccountingEvent]] = {
    "occurred_at": adjust(key(8, "m1", "manual"), "ETH", "2", "2500"),
    "source": adjust(key(9, "m1", "import"), "ETH", "2", "2500"),
    "external_id": adjust(key(9, "m2", "manual"), "ETH", "2", "2500"),
    "asset": adjust(key(9, "m1", "manual"), "KAS", "2", "2500"),
    "quantity": adjust(key(9, "m1", "manual"), "ETH", "2.5", "2500"),
    "unit_cost": adjust(key(9, "m1", "manual"), "ETH", "2", "2500.000000000000000001"),
    "unit_cost unknown": adjust(key(9, "m1", "manual"), "ETH", "2", None),
}

BASE_TRANSFER: Final = move(key(11, "w1"), "BTC", "0.5", "bitget", "cold-storage")
TRANSFER_VARIANTS: Final[dict[str, AccountingEvent]] = {
    "occurred_at": move(key(12, "w1"), "BTC", "0.5", "bitget", "cold-storage"),
    "source": move(key(11, "w1", "bingx"), "BTC", "0.5", "bitget", "cold-storage"),
    "external_id": move(key(11, "w2"), "BTC", "0.5", "bitget", "cold-storage"),
    "asset": move(key(11, "w1"), "KAS", "0.5", "bitget", "cold-storage"),
    "quantity": move(key(11, "w1"), "BTC", "0.6", "bitget", "cold-storage"),
    "from_location": move(key(11, "w1"), "BTC", "0.5", "bingx", "cold-storage"),
    "to_location": move(key(11, "w1"), "BTC", "0.5", "bitget", "hardware-wallet"),
}


def variant_cases() -> list[object]:
    cases: list[object] = []
    for kind, base, variants in (
        ("trade", BASE_TRADE, TRADE_VARIANTS),
        ("adjustment", BASE_ADJUSTMENT, ADJUSTMENT_VARIANTS),
        ("transfer", BASE_TRANSFER, TRANSFER_VARIANTS),
    ):
        cases.extend(
            pytest.param(base, variant, id=f"{kind}.{field}") for field, variant in variants.items()
        )
    return cases


@pytest.mark.parametrize(("base", "variant"), variant_cases())
def test_changing_any_single_field_changes_the_fingerprint(
    base: AccountingEvent, variant: AccountingEvent
) -> None:
    """Criterion 4: "changing any single field of any event changes the fingerprint".

    Alone, and beside an unrelated event, so a change in one event of a history is seen
    rather than only a change in a history of one.
    """
    neighbour = buy(key(7, "n1"), "KAS", "USDT", "100", "5")

    assert fingerprint(base) != fingerprint(variant)
    assert fingerprint(neighbour, base) != fingerprint(neighbour, variant)


def test_every_variant_digests_differently_from_every_other() -> None:
    """Pairwise distinct: no two single-field changes collapse onto one rendering.

    This is what catches two fields rendered under one name, or a field rendered as its
    neighbour: each variant would differ from the base and still equal another variant.
    """
    events: list[AccountingEvent] = [
        BASE_TRADE,
        BASE_ADJUSTMENT,
        BASE_TRANSFER,
        *TRADE_VARIANTS.values(),
        *ADJUSTMENT_VARIANTS.values(),
        *TRANSFER_VARIANTS.values(),
    ]

    digests = [fingerprint(event) for event in events]

    assert len(set(digests)) == len(digests)


def test_the_fields_varied_are_every_field_the_events_have() -> None:
    """The variant tables are pinned to the dataclasses, so a new field needs a new variant."""
    trade_fields = {*type(BASE_TRADE).__dataclass_fields__} - {"key"}
    adjustment_fields = {*type(BASE_ADJUSTMENT).__dataclass_fields__} - {"key"}
    transfer_fields = {*type(BASE_TRANSFER).__dataclass_fields__} - {"key"}
    key_fields = {*type(BASE_KEY).__dataclass_fields__}

    assert set(TRADE_VARIANTS) == trade_fields | key_fields
    assert set(ADJUSTMENT_VARIANTS) - {"unit_cost unknown"} == adjustment_fields | key_fields
    assert set(TRANSFER_VARIANTS) == transfer_fields | key_fields


def test_the_kind_is_in_the_fingerprint() -> None:
    """A trade and a transfer can share a key; the kind is what tells the histories apart."""
    shared = key(10, "5001")

    assert fingerprint(move(shared, "BTC", "1")) != fingerprint(adjust(shared, "BTC", "1", None))


def test_r8_a_named_zero_fee_changes_the_fingerprint_but_not_the_answer() -> None:
    """`fee_asset` is a field, so it is fingerprinted, even where R8 makes it inert."""
    bare = buy(BASE_KEY, "BTC", "USDT", "1", "30000")
    named = buy(BASE_KEY, "BTC", "USDT", "1", "30000", "0", "BGB")

    assert fingerprint(bare) != fingerprint(named)
    assert replay([bare], CONFIG).positions == replay([named], CONFIG).positions


# --------------------------------------------------------------------------------------
# Equal inputs digest equally
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spellings",
    [
        pytest.param(("1", "1.0", "1.000", "1.000000000000000000", "1E+0", "0.1E+1"), id="one"),
        pytest.param(("30000", "3E+4", "30000.00"), id="thirty thousand"),
        pytest.param(("0.000000000000000001", "1E-18", "0.0000000000000000010"), id="one unit"),
    ],
)
def test_numerically_equal_amounts_fingerprint_identically(spellings: Sequence[str]) -> None:
    """`1`, `1.0` and `1.000` are one amount, in every amount field of every kind."""
    digests: dict[str, set[str]] = {"trade": set(), "adjustment": set(), "transfer": set()}
    for spelling in spellings:
        digests["trade"].add(fingerprint(buy(BASE_KEY, "BTC", "USDT", spelling, spelling)))
        digests["adjustment"].add(
            fingerprint(adjust(key(9, "m1", "manual"), "ETH", spelling, spelling))
        )
        digests["transfer"].add(fingerprint(move(key(11, "w1"), "BTC", spelling)))

    assert {kind: len(found) for kind, found in digests.items()} == {
        "trade": 1,
        "adjustment": 1,
        "transfer": 1,
    }


@pytest.mark.parametrize("zero", ["0", "-0", "0.000", "-0.000000000000000000", "0E-18", "-0E+3"])
def test_negative_zero_fingerprints_as_zero(zero: str) -> None:
    """A fee of `-0` is no fee, and so is a unit cost of `-0` a unit cost of zero."""
    base_fee = fingerprint(buy(BASE_KEY, "BTC", "USDT", "1", "30000", "0", "USDT"))
    base_cost = fingerprint(adjust(key(9, "m1", "manual"), "ETH", "2", "0"))

    assert fingerprint(buy(BASE_KEY, "BTC", "USDT", "1", "30000", zero, "USDT")) == base_fee
    assert fingerprint(adjust(key(9, "m1", "manual"), "ETH", "2", zero)) == base_cost


def test_a_non_utc_instant_fingerprints_as_the_same_utc_instant() -> None:
    """07:00 at -03:00 is 10:00 UTC, and 12:30 at +02:30 is 10:00 UTC too."""
    utc = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    buenos_aires = datetime(2026, 1, 1, 7, 0, tzinfo=timezone(timedelta(hours=-3)))
    odd_offset = datetime(2026, 1, 1, 12, 30, tzinfo=timezone(timedelta(hours=2, minutes=30)))

    digests = {
        fingerprint(buy(key(moment, "e1"), "BTC", "USDT", "1", "30000"))
        for moment in (utc, buenos_aires, odd_offset)
    }

    assert len(digests) == 1


def test_a_microsecond_is_a_different_instant() -> None:
    """The instant is written with microseconds, so a microsecond apart is a different input."""
    whole = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)

    assert fingerprint(buy(key(whole, "e1"), "BTC", "USDT", "1", "1")) != fingerprint(
        buy(key(whole + timedelta(microseconds=1), "e1"), "BTC", "USDT", "1", "1")
    )


def test_replaying_the_same_events_twice_gives_the_same_fingerprint() -> None:
    events: list[AccountingEvent] = [BASE_TRADE, BASE_ADJUSTMENT, BASE_TRANSFER]

    assert replay(events, CONFIG).input_fingerprint == replay(events, CONFIG).input_fingerprint


def test_input_order_and_duplicates_do_not_change_the_fingerprint() -> None:
    """The fingerprint covers events after deduplication, in replay order."""
    forward = fingerprint(BASE_ADJUSTMENT, BASE_TRADE, BASE_TRANSFER)

    assert fingerprint(BASE_TRANSFER, BASE_TRADE, BASE_ADJUSTMENT) == forward
    assert fingerprint(BASE_TRADE, BASE_TRANSFER, BASE_TRADE, BASE_ADJUSTMENT) == forward


# --------------------------------------------------------------------------------------
# What else it covers: the configuration, the method and the engine version
# --------------------------------------------------------------------------------------


def test_the_cash_assets_are_in_the_fingerprint() -> None:
    """Even a cash asset no event mentions: the configuration is part of the input."""
    events = [BASE_TRADE]

    default = fingerprint(*events, config=AccountingConfig(frozenset({"USDC", "USDT"})))
    wider = fingerprint(*events, config=AccountingConfig(frozenset({"USDC", "USDT", "DAI"})))
    narrower = fingerprint(*events, config=AccountingConfig(frozenset({"USDT"})))

    assert len({default, wider, narrower}) == 3


def test_the_cash_assets_are_fingerprinted_as_a_set() -> None:
    """Built in either order, one set: the fingerprint sorts them."""
    one = AccountingConfig(frozenset(["USDT", "USDC", "DAI"]))
    other = AccountingConfig(frozenset(["DAI", "USDC", "USDT"]))

    assert fingerprint(BASE_TRADE, config=one) == fingerprint(BASE_TRADE, config=other)


def _patch_everywhere(monkeypatch: pytest.MonkeyPatch, name: str, value: object) -> list[str]:
    """Rebind `name` in every loaded accounting module that holds it.

    The constant is imported by name into whichever module renders the fingerprint, so
    patching `constants` alone would change nothing. Patching every holder means this does
    not depend on which module that is, only on the constant being read at call time.
    """
    patched = []
    for module_name, module in list(sys.modules.items()):
        if module_name.startswith("portfolio.domain.accounting") and hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            patched.append(module_name)
    return patched


def test_the_engine_version_is_in_the_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed engine bug must change the fingerprint, or #19 keeps the wrong snapshot."""
    before = replay([BASE_TRADE], CONFIG)

    assert _patch_everywhere(monkeypatch, "ENGINE_VERSION", ENGINE_VERSION + 1)
    after = replay([BASE_TRADE], CONFIG)

    assert after.input_fingerprint != before.input_fingerprint
    assert after.engine_version == ENGINE_VERSION + 1
    assert after.positions == before.positions


def test_the_method_is_in_the_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A FIFO result over the same events must never share a weighted-average fingerprint."""
    before = replay([BASE_TRADE], CONFIG)

    assert _patch_everywhere(monkeypatch, "METHOD", "fifo")
    after = replay([BASE_TRADE], CONFIG)

    assert after.input_fingerprint != before.input_fingerprint
    assert before.method == METHOD


def test_the_fingerprint_is_lowercase_sha256_hex() -> None:
    digest = fingerprint(BASE_TRADE)

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_text_fields_with_non_ascii_fingerprint_without_error() -> None:
    """UTF-8 text that is not ASCII is valid input and must digest, distinctly."""
    plain = fingerprint(move(key(11, "w1"), "BTC", "0.5", "bitget", "wallet"))
    accented = fingerprint(move(key(11, "w1"), "BTC", "0.5", "bitget", "wallét"))

    assert plain != accented


def test_amounts_at_the_edges_of_the_grid_fingerprint_distinctly() -> None:
    """The widest amount and the smallest unit, which are where a rendering would truncate."""
    widest = Decimal("99999999999999999999.999999999999999999")

    assert fingerprint(move(key(11, "w1"), "BTC", str(widest))) != fingerprint(
        move(key(11, "w1"), "BTC", "99999999999999999999.999999999999999998")
    )
    assert fingerprint(move(key(11, "w1"), "BTC", "0.000000000000000001")) != fingerprint(
        move(key(11, "w1"), "BTC", "0.000000000000000002")
    )
