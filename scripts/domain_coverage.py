"""The domain coverage floor: 95% of lines and 90% of branches under `src/portfolio/domain/`.

Criterion 13 of #17. The repository-wide floor in `backend/pyproject.toml` is one number over
everything, and at 99.7% it would let the whole accounting engine slip below it as long as
the rest of the code base made up the difference. `domain/` is where the numbers the product
reports are computed, so it gets a floor of its own, on lines and on branches separately --
a module can execute every line and still never take the branch where a pool runs short.

Usage:
    python scripts/domain_coverage.py REPORT

`REPORT` is a coverage.py JSON report (format 3), the file `pytest --cov
--cov-report=json:REPORT` writes. `scripts/check.py` and the `Backend tests` job in CI each
point pytest at a fresh path and then run this over it, so the figures are always the run
that just happened.

**Standard library only**, so it runs under whichever Python runs `scripts/check.py`, and
under the runner's own `python3` in CI, without the backend environment.

**It fails closed.** A report it cannot read, a report without branch data, a report in
which no file is under `src/portfolio/domain/`, or one with no statements or no branches
there, is exit status 2 -- never a pass. A floor whose subject has gone missing reports
success, and that is the failure this repository keeps rediscovering.

**The comparison is exact**: integers, `covered * 100 >= floor * total`. Percentages are
printed for a reader, rounded down, and never compared.

Exit status: 0 when both floors hold, 1 when either does not, 2 when the report is unusable.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

LINE_FLOOR_PERCENT: Final = 95
BRANCH_FLOOR_PERCENT: Final = 90
DOMAIN_PATH: Final = "src/portfolio/domain/"
"""Matched against each file's path with its separators made `/`, anywhere in the path, so a
relative `src/portfolio/domain/money.py` and an absolute path ending the same way both count.
coverage.py writes a Windows path with backslashes."""

PASSED: Final = 0
BELOW_FLOOR: Final = 1
UNUSABLE: Final = 2


class UnusableReportError(Exception):
    """The report cannot say what the domain's coverage is."""


@dataclass(frozen=True)
class DomainCoverage:
    """Summed over every file under `DOMAIN_PATH`."""

    files: int
    covered_lines: int
    statements: int
    covered_branches: int
    branches: int

    @property
    def lines_hold(self) -> bool:
        return self.covered_lines * 100 >= LINE_FLOOR_PERCENT * self.statements

    @property
    def branches_hold(self) -> bool:
        return self.covered_branches * 100 >= BRANCH_FLOOR_PERCENT * self.branches


def measure(report: object) -> DomainCoverage:
    """Sum the domain's figures out of a parsed JSON report, or refuse the report."""
    if not isinstance(report, dict):
        raise UnusableReportError("the report is not a JSON object")
    meta = report.get("meta")
    if not isinstance(meta, dict) or meta.get("branch_coverage") is not True:
        raise UnusableReportError(
            "the report was not measured with branch coverage, so the branch floor cannot "
            "be checked"
        )
    files = report.get("files")
    if not isinstance(files, dict):
        raise UnusableReportError("the report has no 'files' object")
    totals = {"covered_lines": 0, "num_statements": 0, "covered_branches": 0, "num_branches": 0}
    matched = 0
    for path, entry in files.items():
        if DOMAIN_PATH not in "/" + str(path).replace("\\", "/"):
            continue
        summary = entry.get("summary") if isinstance(entry, dict) else None
        if not isinstance(summary, dict):
            raise UnusableReportError(f"{path} has no summary")
        for name in totals:
            value = summary.get(name)
            if type(value) is not int or value < 0:
                raise UnusableReportError(f"{path} has no whole number for {name}")
            totals[name] += value
        matched += 1
    if matched == 0:
        raise UnusableReportError(f"no file under {DOMAIN_PATH} is in the report")
    if totals["num_statements"] == 0 or totals["num_branches"] == 0:
        raise UnusableReportError(f"the files under {DOMAIN_PATH} have no statements or no branches")
    return DomainCoverage(
        files=matched,
        covered_lines=totals["covered_lines"],
        statements=totals["num_statements"],
        covered_branches=totals["covered_branches"],
        branches=totals["num_branches"],
    )


def percent(covered: int, total: int) -> str:
    """`covered / total` as a percentage to two places, rounded down, for display only."""
    basis_points = covered * 10_000 // total
    return f"{basis_points // 100}.{basis_points % 100:02d}%"


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: python scripts/domain_coverage.py REPORT", file=sys.stderr)
        return UNUSABLE
    path = Path(arguments[0])
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        coverage = measure(report)
    except (OSError, ValueError, UnusableReportError) as error:
        # `ValueError` covers `json.JSONDecodeError` and a report that is not UTF-8.
        print(f"domain coverage: unusable report {path}: {error}", file=sys.stderr)
        return UNUSABLE
    lines = f"lines {percent(coverage.covered_lines, coverage.statements)}"
    lines += f" ({coverage.covered_lines}/{coverage.statements}, floor {LINE_FLOOR_PERCENT}%)"
    branches = f"branches {percent(coverage.covered_branches, coverage.branches)}"
    branches += f" ({coverage.covered_branches}/{coverage.branches}, floor {BRANCH_FLOOR_PERCENT}%)"
    print(f"domain coverage over {coverage.files} files: {lines}; {branches}")
    if coverage.lines_hold and coverage.branches_hold:
        return PASSED
    below = [
        name
        for name, holds in (("lines", coverage.lines_hold), ("branches", coverage.branches_hold))
        if not holds
    ]
    print(f"FAILED: domain coverage is below the floor for {' and '.join(below)}", file=sys.stderr)
    return BELOW_FLOOR


if __name__ == "__main__":
    sys.exit(main())
