"""#17 criterion 1, the half an import contract cannot see: `domain/` never reads the clock.

`replay` must be a function of its events and nothing else, and the `domain-is-pure`
contract forbids every module that does I/O or introduces nondeterminism -- but not
`datetime`, because an event's key needs it. And `datetime.now()` needs nothing more than
`datetime`. The clock is a *call*, so it is forbidden here by walking the syntax tree of
every module under `src/portfolio/domain/`.

**What is caught**: any attribute access named `now`, `utcnow`, `today`, `fromtimestamp` or
`utcfromtimestamp` -- called or not, so `clock = datetime.now` is caught at the line that
takes it -- and any import of those names, so `from datetime import datetime as d; d.now()`
is caught twice. `utcfromtimestamp` is not in the spec's list; it is `fromtimestamp` under
another name, and leaving it out would leave the spec's own rule with a hole in it.

**What is not**: `time.time()` and `time.monotonic()`, which the import contract forbids by
forbidding `time`; and reaching a method without naming it, such as
`getattr(datetime, "no" + "w")`, which is deliberate circumvention rather than accident.

Proven able to fail by a planted module with every banned form at a known line.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
DOMAIN_ROOT: Final = REPO_ROOT / "backend" / "src" / "portfolio" / "domain"

CLOCK_READS: Final = frozenset({"now", "utcnow", "today", "fromtimestamp", "utcfromtimestamp"})


@dataclass(frozen=True)
class ClockRead:
    path: Path
    line: int
    name: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: reads the clock through {self.name}"


def find_clock_reads(path: Path, source: str) -> list[ClockRead]:
    tree = ast.parse(source, filename=str(path))
    found: list[ClockRead] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in CLOCK_READS:
            found.append(ClockRead(path, node.lineno, f".{node.attr}"))
        elif isinstance(node, ast.ImportFrom):
            found.extend(
                ClockRead(path, node.lineno, f"import of {alias.name}")
                for alias in node.names
                if alias.name in CLOCK_READS
            )
    return found


def domain_modules() -> list[Path]:
    return sorted(path for path in DOMAIN_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def test_the_banned_names_are_the_spec_list() -> None:
    """Pinned as a literal: the spec's four, plus `utcfromtimestamp` for the reason above."""
    assert frozenset({"now", "utcnow", "today", "fromtimestamp"}) <= CLOCK_READS
    assert {"now", "utcnow", "today", "fromtimestamp", "utcfromtimestamp"} == CLOCK_READS


def test_the_scan_reaches_the_accounting_engine() -> None:
    """The walk is only worth what it reaches: the engine's modules, by name."""
    scanned = {path.relative_to(DOMAIN_ROOT).as_posix() for path in domain_modules()}

    assert {
        "money.py",
        "accounting/events.py",
        "accounting/replay.py",
        "accounting/fingerprint.py",
        "accounting/results.py",
    } <= scanned


def test_the_domain_never_reads_the_clock() -> None:
    reads = [
        read
        for path in domain_modules()
        for read in find_clock_reads(path, path.read_text(encoding="utf-8"))
    ]

    assert reads == [], "\n".join(str(read) for read in reads)


def test_the_scan_reports_a_planted_clock_read(tmp_path: Path) -> None:
    """Every banned form, each at a known line, so a scanner that misses one is visible."""
    module = tmp_path / "clocky.py"
    module.write_text(
        "\n".join(
            [
                "from datetime import UTC, date, datetime",  # 1
                "from datetime import datetime as moment",  # 2
                "",  # 3
                "STAMP = datetime.now(UTC)",  # 4
                "LEGACY = datetime.utcnow()",  # 5
                "DAY = date.today()",  # 6
                "EPOCH = datetime.fromtimestamp(0, UTC)",  # 7
                "OLD = datetime.utcfromtimestamp(0)",  # 8
                "clock = moment.now",  # 9: taken, not called
                "from time import time as now",  # 10: `time` is the import contract's to catch
            ]
        ),
        encoding="utf-8",
    )

    reads = find_clock_reads(module, module.read_text(encoding="utf-8"))

    assert {read.line for read in reads} == {4, 5, 6, 7, 8, 9}
    assert all(read.path == module for read in reads)


def test_the_scan_reports_an_imported_clock_function(tmp_path: Path) -> None:
    """`from datetime import datetime` is fine; importing a clock function by name is not."""
    module = tmp_path / "imported.py"
    module.write_text("from somewhere import utcnow\n\nSTAMP = utcnow()\n", encoding="utf-8")

    reads = find_clock_reads(module, module.read_text(encoding="utf-8"))

    assert [(read.line, read.name) for read in reads] == [(1, "import of utcnow")]


def test_the_scan_passes_the_datetime_use_the_domain_needs(tmp_path: Path) -> None:
    """The control: building, normalising and parsing instants is not reading the clock."""
    module = tmp_path / "pure.py"
    module.write_text(
        "\n".join(
            [
                '"""Never call datetime.now() in here."""',
                "from datetime import UTC, datetime, timedelta",
                "",
                "START = datetime(2026, 1, 1, tzinfo=UTC)",
                "LATER = START + timedelta(days=1)",
                "PARSED = datetime.fromisoformat('2026-01-01T00:00:00+00:00')",
                "NORMAL = LATER.astimezone(UTC)",
                "TEXT = NORMAL.isoformat(timespec='microseconds')",
            ]
        ),
        encoding="utf-8",
    )

    assert find_clock_reads(module, module.read_text(encoding="utf-8")) == []
