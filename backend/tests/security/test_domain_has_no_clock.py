"""#17 criterion 1, the half an import contract cannot see: `domain/` never reads the clock.

It does not reach the host either: the second half of this module bans the I/O builtins
and a conversion to the local time zone, which need no import at all.

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


def test_the_scan_reaches_the_modules_that_handle_time() -> None:
    """The walk is only worth what it reaches: the modules that take an instant, by name."""
    scanned = {path.relative_to(DOMAIN_ROOT).as_posix() for path in domain_modules()}

    assert {
        "money.py",
        "auth.py",
        "backups.py",
        "health.py",
        "portfolio.py",
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


# --------------------------------------------------------------------------------------
# The host, reached without an import: I/O builtins and the local time zone
# --------------------------------------------------------------------------------------
#
# Added after review. Two more ways to depend on the machine that no import contract sees,
# because neither needs an import:
#
# * **`open`, `print` and `input`** are builtins: file I/O, a write to stdout (which in this
#   process is the structured log stream), and a read from stdin. Caught as any reference to
#   the name -- called or taken -- and as `builtins.open` and an import from `builtins`.
#   Ruff's `A` rules already stop a local variable shadowing these names, so a reference is
#   always the builtin.
# * **An argument-less `.astimezone()`** converts to the *host's* local time zone, so the same
#   event would normalise differently on the Pi and on a laptop. `.astimezone(UTC)` is the
#   form the domain uses, and is fine; it is the missing argument that is banned.

IO_BUILTINS: Final = frozenset({"open", "print", "input"})


def find_host_reads(path: Path, source: str) -> list[ClockRead]:
    """I/O builtins referenced in any way, and `.astimezone()` called with no argument."""
    tree = ast.parse(source, filename=str(path))
    found: list[ClockRead] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in IO_BUILTINS:
            found.append(ClockRead(path, node.lineno, f"the builtin {node.id}"))
        elif isinstance(node, ast.Attribute) and node.attr in IO_BUILTINS:
            base = node.value
            if isinstance(base, ast.Name) and base.id in {"builtins", "__builtins__"}:
                found.append(ClockRead(path, node.lineno, f"builtins.{node.attr}"))
        elif isinstance(node, ast.ImportFrom) and node.module == "builtins":
            found.extend(
                ClockRead(path, node.lineno, f"import of the builtin {alias.name}")
                for alias in node.names
                if alias.name in IO_BUILTINS
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "astimezone"
            and not node.args
            and not node.keywords
        ):
            found.append(ClockRead(path, node.lineno, "astimezone() to the host's time zone"))
    return found


def test_the_banned_builtins_are_open_print_and_input() -> None:
    assert frozenset({"open", "print", "input"}) == IO_BUILTINS


def test_the_domain_never_touches_the_host() -> None:
    reads = [
        read
        for path in domain_modules()
        for read in find_host_reads(path, path.read_text(encoding="utf-8"))
    ]

    assert reads == [], "\n".join(str(read) for read in reads)


def test_the_host_scan_reports_a_planted_violation(tmp_path: Path) -> None:
    """Every banned form at a known line, and the allowed `.astimezone(UTC)` beside them."""
    module = tmp_path / "hosty.py"
    module.write_text(
        "\n".join(
            [
                "import builtins",  # 1
                "from builtins import open as fetch",  # 2: an import of the builtin
                "from datetime import UTC, datetime",  # 3
                "",  # 4
                "HANDLE = open('ledger.csv')",  # 5: called
                "print('replayed')",  # 6: called
                "ANSWER = input()",  # 7: called
                "SINK = print",  # 8: taken, not called
                "OTHER = builtins.open",  # 9: through the module
                "LOCAL = datetime(2026, 1, 1, tzinfo=UTC).astimezone()",  # 10: host time zone
                "FINE = datetime(2026, 1, 1, tzinfo=UTC).astimezone(UTC)",  # 11: allowed
                "ALSO_FINE = datetime(2026, 1, 1, tzinfo=UTC).astimezone(tz=UTC)",  # 12: allowed
            ]
        ),
        encoding="utf-8",
    )

    reads = find_host_reads(module, module.read_text(encoding="utf-8"))

    assert sorted({read.line for read in reads}) == [2, 5, 6, 7, 8, 9, 10]


def test_the_host_scan_passes_a_docstring_that_names_the_builtins(tmp_path: Path) -> None:
    """Explaining the rule is not breaking it: text in a string is not a reference."""
    module = tmp_path / "explains.py"
    module.write_text(
        '"""Never print, open a file or read input here; never call astimezone()."""\n'
        "LABEL = 'print'\n",
        encoding="utf-8",
    )

    assert find_host_reads(module, module.read_text(encoding="utf-8")) == []
