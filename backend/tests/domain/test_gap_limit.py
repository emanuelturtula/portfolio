"""Spec 031, criterion 3 (arithmetic): how many addresses a branch still needs (R5).

`addresses_to_extend(used_by_index)` takes a branch's used flags in index order, skipped
indices absent, and answers how many more addresses the scan must read before the branch has
twenty unused ones after its highest used index. The properties restate R5 independently --
as a count of trailing unused flags, and as a whole scan simulated against a set of used
indices -- and ask Hypothesis for inputs on which the two disagree.
"""

from __future__ import annotations

from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from portfolio.domain.extended_keys import GAP_LIMIT, MAX_ADDRESSES_PER_BRANCH, addresses_to_extend

FLAGS: Final = st.lists(st.booleans(), max_size=120)


def trailing_unused(flags: list[bool]) -> int:
    count = 0
    for used in reversed(flags):
        if used:
            break
        count += 1
    return count


def test_the_constants() -> None:
    assert GAP_LIMIT == 20
    assert MAX_ADDRESSES_PER_BRANCH == 1000


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        pytest.param([], 20, id="nothing scanned: twenty to read"),
        pytest.param([False] * 19, 1, id="nineteen unused: one more"),
        pytest.param([False] * 20, 0, id="twenty unused: complete"),
        pytest.param([False] * 35, 0, id="more than twenty unused: complete"),
        pytest.param([True], 20, id="one used: twenty after it"),
        pytest.param([True] + [False] * 5, 15, id="used then five unused"),
        pytest.param([True] + [False] * 20, 0, id="used then twenty unused"),
        pytest.param([False] * 30 + [True], 20, id="a late use reopens the branch"),
        pytest.param([True, False, True], 20, id="the last used index counts"),
        pytest.param([False] * 10 + [True] + [False] * 19, 1, id="one short after a use"),
    ],
)
def test_addresses_to_extend(flags: list[bool], expected: int) -> None:
    assert addresses_to_extend(flags) == expected


def test_criterion_three_receive_branch() -> None:
    """Used at 0, 5 and 24: the branch is complete at index 44, so 45 addresses in all."""
    flags = [index in {0, 5, 24} for index in range(45)]
    assert addresses_to_extend(flags) == 0
    assert addresses_to_extend(flags[:44]) == 1


def test_criterion_three_change_branch() -> None:
    """Used at 0 and 3: complete at index 23, so 24 addresses in all."""
    flags = [index in {0, 3} for index in range(24)]
    assert addresses_to_extend(flags) == 0
    assert addresses_to_extend(flags[:23]) == 1


def test_a_tuple_is_accepted() -> None:
    assert addresses_to_extend((True, False)) == 19


@given(flags=FLAGS)
def test_the_answer_is_twenty_minus_the_trailing_unused_count(flags: list[bool]) -> None:
    assert addresses_to_extend(flags) == max(0, GAP_LIMIT - trailing_unused(flags))


@given(flags=FLAGS)
def test_reading_that_many_unused_addresses_completes_the_branch(flags: list[bool]) -> None:
    """Minimal and sufficient: n unused more completes it, n - 1 does not."""
    needed = addresses_to_extend(flags)
    assert 0 <= needed <= GAP_LIMIT
    assert addresses_to_extend(flags + [False] * needed) == 0
    if needed:
        assert addresses_to_extend(flags + [False] * (needed - 1)) == 1


@given(flags=FLAGS.filter(bool))
def test_marking_the_last_address_used_restarts_the_gap(flags: list[bool]) -> None:
    """R5: once used, always used, and a newly used last address needs twenty after it."""
    as_used = [*flags[:-1], True]
    assert addresses_to_extend(as_used) == GAP_LIMIT
    assert addresses_to_extend(as_used) >= addresses_to_extend(flags)


def simulate_scan(used: set[int]) -> int:
    """The scan loop as the spec describes it: extend until the function says 0."""
    flags: list[bool] = []
    while True:
        more = addresses_to_extend(flags)
        if more == 0:
            return len(flags)
        for _ in range(more):
            flags.append(len(flags) in used)


def reachable_end(used: set[int]) -> int:
    """R5, restated: the highest used index reachable by gaps of at most twenty, plus 21."""
    last = -1
    for index in sorted(used):
        if index - last > GAP_LIMIT:
            break
        last = index
    return last + 1 + GAP_LIMIT


@settings(max_examples=200)
@given(used=st.sets(st.integers(min_value=0, max_value=200), max_size=12))
def test_a_simulated_scan_reads_exactly_up_to_the_gap(used: set[int]) -> None:
    """Every used address within twenty of the previous one is reached, and nothing past the gap."""
    assert simulate_scan(used) == reachable_end(used)


def test_criterion_three_by_simulation() -> None:
    """Receive 0, 5, 24 and 46: index 46 is more than twenty past 24 and is never reached."""
    assert simulate_scan({0, 5, 24, 46}) == 45
    assert simulate_scan({0, 3}) == 24
    assert simulate_scan(set()) == 20
