"""`hash-benchmark`: the command that turns "tuned on the Pi" into something you can do.

The parameters cannot be measured on CI -- there is no Raspberry Pi here and no staging
environment -- so what is tested is the instrument, not the reading: that it hashes at the
configured cost, reports a duration and the floor to compare it against, and does its
arithmetic in whole nanoseconds rather than through a float.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from portfolio import cli
from portfolio.domain.passwords import OWASP_MINIMUM_MEMORY_COST

if TYPE_CHECKING:
    from pathlib import Path


def test_hash_benchmark_reports_the_configured_parameters(
    cli_database: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The output names what it measured, or the number it prints means nothing."""
    del cli_database

    assert cli.main(["hash-benchmark", "--rounds", "2"]) == 0

    written = capsys.readouterr().out
    assert "argon2id" in written
    assert "time_cost=1" in written
    assert "memory_cost=64 KiB" in written
    assert "median of 2 hashes" in written
    assert " ms" in written
    # The floor is printed beside the measurement, because a fast number is only good news
    # if the parameters that produced it are still above the minimum.
    assert str(OWASP_MINIMUM_MEMORY_COST) in written


def test_hash_benchmark_needs_no_database(
    cli_database: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """It hashes, it does not store: this has to be runnable on a machine with no volume."""
    del cli_database

    assert cli.main(["hash-benchmark", "--rounds", "1"]) == 0

    assert "median of 1 hashes" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        ([5], 5),
        ([3, 1, 2], 2),
        ([4, 2], 3),
        ([10, 20, 30, 40], 25),
    ],
)
def test_the_median_is_an_integer_number_of_nanoseconds(
    samples: list[int],
    expected: int,
) -> None:
    """Whole nanoseconds, including for an even-length sample.

    `statistics.median` returns a float for an even-length input, and this code base does
    not produce a float where it can avoid one -- the ban in `services/` is the rule, and
    the habit is the reason the ban holds.
    """
    result = cli.median_nanoseconds(samples)

    assert result == expected
    assert isinstance(result, int)
