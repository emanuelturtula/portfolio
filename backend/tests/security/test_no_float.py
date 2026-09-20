"""Criteria 7 and 8: the float ban is mechanical, and the reasoning is written down.

`CLAUDE.md` rule 2 says `float` is banned in `domain/`, `services/` and `providers/`. Until
this module existed that was documentation, and documentation is not enforcement. Here it
is an AST walk that fails the build and names the file and the line.

Two things make the difference between this and a test that passes forever:

* it asserts it actually **found** the modules it claims to have scanned, because a ban
  pointed at a directory that no longer exists is green and worthless;
* it is proven able to **fail**, by being fed a synthetic module containing every banned
  form and asserting each one comes back with its file and its line.

There is no allowlist, deliberately. A genuine exception should be argued in a pull request,
not added to a list that grows one quiet entry at a time.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
SOURCE_ROOT: Final = REPO_ROOT / "backend" / "src" / "portfolio"
ARCHITECTURE: Final = REPO_ROOT / "docs" / "architecture.md"

PURE_PACKAGES: Final = ("domain", "services", "providers")
"""The three layers rule 2 names. `db/` and `api/` legitimately mention `float` to reject
one at the boundary, which is the opposite of the thing being banned."""

# SUM, AVG and TOTAL apply SQLite's numeric affinity, which is the `double` that
# `NumericText` exists to keep money away from -- applied to every row at once. COUNT is
# fine and is deliberately not listed.
SQL_MONEY_AGGREGATES: Final = re.compile(r"\b(SUM|AVG|TOTAL)\s*\(", re.IGNORECASE)


@dataclass(frozen=True)
class Violation:
    """One banned construct, at one place, with the reason it is banned."""

    path: Path
    line: int
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}"


def _docstring_constants(tree: ast.Module) -> set[int]:
    """The `id()` of every string constant that is a docstring.

    A docstring that explains why `SUM()` is forbidden must not itself be reported as a
    `SUM()`. Explaining the rule is not breaking it.
    """
    holders: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            holders.add(id(first.value))
    return holders


def find_float_usage(path: Path, source: str) -> list[Violation]:
    """Every float literal and every reference to the builtin `float` in one module.

    A reference is reported as well as a call, so `float(x)`, `x: float` and
    `isinstance(x, float)` are all caught. `float` has no legitimate use in these three
    layers in any of those positions, and narrowing this to `ast.Call` would let an
    annotation declare a money field as a float while the ban stayed green.
    """
    tree = ast.parse(source, filename=str(path))
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            violations.append(Violation(path, node.lineno, f"float literal {node.value!r}"))
        elif isinstance(node, ast.Name) and node.id == "float":
            violations.append(Violation(path, node.lineno, "reference to the builtin float"))
        elif isinstance(node, ast.Attribute) and node.attr == "float":
            violations.append(Violation(path, node.lineno, f"attribute access .{node.attr}"))
    return violations


def find_numeric_usage(path: Path, source: str) -> list[Violation]:
    """Every mention of `Numeric`, the SQLAlchemy type that round-trips through a double."""
    tree = ast.parse(source, filename=str(path))
    violations: list[Violation] = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.alias):
            name = node.asname or node.name
        if name == "Numeric":
            violations.append(
                Violation(path, getattr(node, "lineno", 0), "sqlalchemy.Numeric is forbidden")
            )
    return violations


def find_sql_money_aggregates(path: Path, source: str) -> list[Violation]:
    """Every `SUM(`, `AVG(` or `TOTAL(` in a SQL string, and every `func.sum` equivalent.

    Docstrings are exempt: this file and `db/types.py` both explain the rule in prose.
    """
    tree = ast.parse(source, filename=str(path))
    docstrings = _docstring_constants(tree)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and SQL_MONEY_AGGREGATES.search(node.value)
        ):
            violations.append(Violation(path, node.lineno, "SQL aggregate over a column"))
        elif isinstance(node, ast.Attribute) and node.attr in {"sum", "avg", "total"}:
            base = node.value
            if isinstance(base, ast.Name) and base.id == "func":
                violations.append(Violation(path, node.lineno, f"func.{node.attr}"))
    return violations


def pure_layer_modules() -> list[Path]:
    """Every Python module under `domain/`, `services/` and `providers/`."""
    return sorted(
        path
        for package in PURE_PACKAGES
        for path in (SOURCE_ROOT / package).rglob("*.py")
        if "__pycache__" not in path.parts
    )


def all_source_modules() -> list[Path]:
    """Every Python module in the backend package, migrations included."""
    return sorted(path for path in SOURCE_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


# --------------------------------------------------------------------------------------
# Criterion 7: the ban.
# --------------------------------------------------------------------------------------


def test_the_scan_actually_finds_the_modules_it_claims_to_check() -> None:
    """A ban walking the wrong directory is green and proves nothing.

    Pinned by name rather than by count, so adding a module does not fail this, and
    renaming a package away from the ban does.
    """
    assert SOURCE_ROOT.is_dir(), SOURCE_ROOT
    for package in PURE_PACKAGES:
        assert (SOURCE_ROOT / package).is_dir(), f"{package} is not where the ban looks"

    modules = pure_layer_modules()
    names = {path.relative_to(SOURCE_ROOT).as_posix() for path in modules}

    assert "domain/money.py" in names
    assert len(modules) >= len(PURE_PACKAGES)


def test_no_float_in_the_pure_layers() -> None:
    """Criterion 7, over the real tree."""
    violations = [
        violation
        for path in pure_layer_modules()
        for violation in find_float_usage(path, path.read_text(encoding="utf-8"))
    ]

    assert violations == [], "\n".join(str(violation) for violation in violations)


def test_the_float_ban_reports_a_synthetic_violation(tmp_path: Path) -> None:
    """The ban is proven able to fail, with the file and the line in the report.

    Every banned form at a known line, so a report that loses the location, or that only
    catches the literal and not the call, is visible here rather than in six months.
    """
    module = tmp_path / "offender.py"
    module.write_text(
        "\n".join(
            [
                "RATE = 0.1",  # line 1: a float literal
                "",  # 2
                "",  # 3
                "def convert(value: str) -> float:",  # 4: an annotation, twice over
                "    return float(value)",  # 5: a call to the builtin
                "",  # 6
                "",  # 7
                "def is_money(value: object) -> bool:",  # 8
                "    return isinstance(value, float)",  # 9: a reference
            ]
        ),
        encoding="utf-8",
    )

    violations = find_float_usage(module, module.read_text(encoding="utf-8"))
    located = {(violation.line, violation.reason) for violation in violations}

    assert (1, "float literal 0.1") in located
    assert (4, "reference to the builtin float") in located
    assert (5, "reference to the builtin float") in located
    assert (9, "reference to the builtin float") in located
    assert {violation.path for violation in violations} == {module}
    # The report a failing build would print: file, line, reason, on one line.
    assert f"{module}:1: float literal 0.1" in {str(violation) for violation in violations}


def test_the_float_ban_passes_a_module_that_only_talks_about_floats(tmp_path: Path) -> None:
    """A docstring explaining the ban is not a violation of it.

    Without this, the honest thing -- documenting why `float` is banned, in the module that
    bans it -- would be the thing that fails the build, and the fix would be to delete the
    explanation.
    """
    module = tmp_path / "innocent.py"
    module.write_text(
        '"""Money is never a float, because 0.1 is not representable."""\n\nAMOUNT = 1\n',
        encoding="utf-8",
    )

    assert find_float_usage(module, module.read_text(encoding="utf-8")) == []


def test_an_integer_literal_is_not_a_float(tmp_path: Path) -> None:
    """`isinstance(True, float)` is `False` and `1` is not `1.0`; neither may be reported."""
    module = tmp_path / "integers.py"
    module.write_text("SCALE = 8\nFLAG = True\nNOTHING = None\n", encoding="utf-8")

    assert find_float_usage(module, module.read_text(encoding="utf-8")) == []


# --------------------------------------------------------------------------------------
# The other two halves of rule 2: no `Numeric`, no aggregation in SQL.
# --------------------------------------------------------------------------------------


def test_sqlalchemy_numeric_is_used_nowhere() -> None:
    """It round-trips through a C double on SQLite and says nothing about it."""
    violations = [
        violation
        for path in all_source_modules()
        for violation in find_numeric_usage(path, path.read_text(encoding="utf-8"))
    ]

    assert violations == [], "\n".join(str(violation) for violation in violations)


def test_the_numeric_ban_reports_a_synthetic_violation(tmp_path: Path) -> None:
    """Proven able to fail, in both the imported and the qualified spelling."""
    module = tmp_path / "numeric.py"
    module.write_text(
        "import sqlalchemy\nfrom sqlalchemy import Numeric\n\nPRICE = sqlalchemy.Numeric(38, 20)\n",
        encoding="utf-8",
    )

    lines = {violation.line for violation in find_numeric_usage(module, module.read_text("utf-8"))}

    assert lines == {2, 4}


def test_money_is_not_aggregated_in_sql() -> None:
    """`SUM()` on a `TEXT` money column coerces it to a float, one row at a time."""
    violations = [
        violation
        for path in all_source_modules()
        for violation in find_sql_money_aggregates(path, path.read_text(encoding="utf-8"))
    ]

    assert violations == [], "\n".join(str(violation) for violation in violations)


def test_the_aggregate_ban_reports_a_synthetic_violation(tmp_path: Path) -> None:
    """Both spellings: raw SQL, and the SQLAlchemy function namespace."""
    module = tmp_path / "aggregates.py"
    module.write_text(
        '"""Never write SUM(amount) over money."""\n'
        "\n"
        'QUERY = "SELECT SUM(amount) FROM holdings"\n'
        "TOTAL = func.sum(holdings.c.amount)\n",
        encoding="utf-8",
    )

    lines = {
        violation.line for violation in find_sql_money_aggregates(module, module.read_text("utf-8"))
    }

    # Line 1 is the docstring that explains the rule, and must not be reported.
    assert lines == {3, 4}


# --------------------------------------------------------------------------------------
# Criterion 8: the reasoning is written down where the next person will look.
# --------------------------------------------------------------------------------------


def test_architecture_documents_the_money_rules() -> None:
    """`docs/architecture.md` covers both halves of rule 2, by heading and by substance."""
    assert ARCHITECTURE.is_file(), ARCHITECTURE
    document = ARCHITECTURE.read_text(encoding="utf-8")

    assert "### Why `sqlalchemy.Numeric` is forbidden" in document
    assert "### Why money is never aggregated in SQL" in document


@pytest.mark.parametrize(
    "phrase",
    [
        # Why Numeric is forbidden: the mechanism, not just the verdict.
        "IEEE-754",
        "numeric affinity",
        "NumericText",
        # Why aggregation moves to Python: the three operations that coerce, and the fix.
        "SUM(",
        "ORDER BY",
        "aggregate in Python",
        # The representation table and the context, so the document is the whole rule.
        "decimal.Decimal",
        "ROUND_HALF_EVEN",
        "BaseUnits",
    ],
)
def test_architecture_explains_rather_than_asserts(phrase: str) -> None:
    """Each phrase is a mechanism the document would be useless without.

    Checked as substance rather than as prose: the wording around them can change freely,
    and a rewrite that drops the reasoning cannot.
    """
    document = ARCHITECTURE.read_text(encoding="utf-8")

    assert phrase in document
