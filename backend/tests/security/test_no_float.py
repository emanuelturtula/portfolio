"""Criteria 7 and 8: the float ban is mechanical, and the reasoning is written down.

`CLAUDE.md` rule 2 says `float` is banned in `domain/`, `services/` and `providers/`. Until
this module existed that was documentation, and documentation is not enforcement. Here it
is an AST walk that fails the build and names the file and the line.

Two things make the difference between this and a test that passes forever:

* it asserts it actually **found** the modules it claims to have scanned, against the
  literal tuple of package names, because a guard written in terms of `PURE_PACKAGES`
  shrinks along with `PURE_PACKAGES` and cannot fail;
* it is proven able to **fail**, by being fed a synthetic module containing every banned
  form and asserting each one comes back with its file and its line.

There is no allowlist, deliberately. A genuine exception should be argued in a pull request,
not added to a list that grows one quiet entry at a time.

## What this catches, and what it does not

Caught: a float literal; the name `float` in any position, so `float(x)`, `x: float`,
`-> float` and `isinstance(x, float)` all fail; `builtins.float`; an aliased import, so
`from builtins import float as f` fails at the import *and* at every use of `f`; and true
division of two integer literals, `1 / 3`, which produces a float with no literal and no
`float` anywhere in the file.

**Not caught: a float produced at runtime from names.** `a / b` cannot be decided
statically -- it is a float for two ints and a `Decimal` for two `Decimal`s -- and banning
every `/` in these layers would be unusable. `math.pi`, a float returned by a dependency,
and `json.loads` handing back a number are all invisible here too.

That residual is deliberate, and it is why this test is defence in depth rather than the
whole defence. The backstop for a float that only exists at runtime is the boundary
refusing it: `require_amount` rejects a non-`Decimal` before any conversion can hide the
damage, `NumericText` and `BaseUnits` reject one on the way into the database and
`BaseUnits` rejects one on the way back out, and `MoneyStr` rejects a JSON number. This
test stops a float being *written*; those stop one being *stored or served*. A reader who
assumes this file is the only thing standing between the codebase and a float will
eventually be wrong in an expensive way.
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


def _binding_names(tree: ast.Module, target: str) -> set[str]:
    """Every local name in this module that refers to `target`.

    Always contains `target` itself. `from builtins import float as f` adds `f`, so a
    module cannot rename its way past a ban -- the import is reported, and so is every
    later use of the new name, which is what points at the line that does the damage.
    """
    names = {target}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name.split(".")[-1] == target:
                names.add(alias.asname or alias.name)
    return names


def _is_integer_literal(node: ast.expr) -> bool:
    """True for `1` and for `-1`, false for `True`, a name, or anything computed."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd | ast.USub):
        node = node.operand
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    )


def find_float_usage(path: Path, source: str) -> list[Violation]:
    """Every statically decidable way this module produces or names a float.

    Four forms, and the module docstring says which fifth form is out of reach:

    * a float literal;
    * the name `float` in any position, so `float(x)`, `x: float` and
      `isinstance(x, float)` all count -- narrowing this to `ast.Call` would let an
      annotation declare a money field as a float while the ban stayed green;
    * an import that binds the builtin under another name, reported at the import and at
      every use of the alias;
    * `1 / 3`, true division of two integer literals. This is the accident rather than the
      evasion: a share computed that way is a float with no literal and no `float` name
      anywhere in the file. Only literals, because `a / b` on two `Decimal`s is correct and
      indistinguishable from here.
    """
    tree = ast.parse(source, filename=str(path))
    bindings = _binding_names(tree, "float")
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            violations.append(Violation(path, node.lineno, f"float literal {node.value!r}"))
        elif isinstance(node, ast.Name) and node.id in bindings:
            violations.append(Violation(path, node.lineno, "reference to the builtin float"))
        elif isinstance(node, ast.Attribute) and node.attr == "float":
            violations.append(Violation(path, node.lineno, f"attribute access .{node.attr}"))
        elif isinstance(node, ast.alias) and node.name.split(".")[-1] == "float":
            binding = node.asname or node.name
            violations.append(
                Violation(path, node.lineno, f"import of the builtin float as {binding}")
            )
        elif (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and _is_integer_literal(node.left)
            and _is_integer_literal(node.right)
        ):
            violations.append(
                Violation(path, node.lineno, "true division of integer literals yields a float")
            )
    return violations


def find_numeric_usage(path: Path, source: str) -> list[Violation]:
    """Every mention of `Numeric`, the SQLAlchemy type that round-trips through a double.

    Aliases resolve the same way they do for `float`: this used to read
    `node.asname or node.name`, which meant `from sqlalchemy import Numeric as N` bound
    the name `N`, matched nothing, and let `N(38, 20)` through.
    """
    tree = ast.parse(source, filename=str(path))
    bindings = _binding_names(tree, "Numeric")
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.alias):
            name = node.name.split(".")[-1]
        else:
            continue
        if name in bindings:
            violations.append(Violation(path, node.lineno, "sqlalchemy.Numeric is forbidden"))
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


def pure_layer_modules(packages: tuple[str, ...] = PURE_PACKAGES) -> list[Path]:
    """Every Python module under `domain/`, `services/` and `providers/`.

    `packages` is an argument only so a test can call the walk with a deliberately wrong
    tuple and show what that loses. Production callers pass nothing.
    """
    return sorted(
        path
        for package in packages
        for path in (SOURCE_ROOT / package).rglob("*.py")
        if "__pycache__" not in path.parts
    )


def all_source_modules() -> list[Path]:
    """Every Python module in the backend package, migrations included."""
    return sorted(path for path in SOURCE_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


# --------------------------------------------------------------------------------------
# Criterion 7: the ban.
# --------------------------------------------------------------------------------------


def test_the_scan_covers_exactly_the_three_layers_rule_2_names() -> None:
    """Pinned against the literal names, because the previous version could not fail.

    It asserted `len(modules) >= len(PURE_PACKAGES)` and looped over `PURE_PACKAGES` to
    check each entry was a directory. Both sides shrank together: with
    `PURE_PACKAGES = ("domain",)` it passed, and so did `("domain", "domain")`. The ban
    could have stopped scanning `services/` and `providers/` entirely without one test
    going red -- and since both currently hold nothing but an empty `__init__.py`, not a
    single assertion elsewhere would have noticed either.
    """
    assert PURE_PACKAGES == ("domain", "services", "providers")
    assert len(set(PURE_PACKAGES)) == len(PURE_PACKAGES)
    assert SOURCE_ROOT.is_dir(), SOURCE_ROOT


def test_every_named_package_contributes_at_least_one_scanned_module() -> None:
    """Each layer is reached, checked per package rather than by a total count.

    A total count is satisfied by three modules from one package. This is not.
    """
    scanned: dict[str, set[str]] = {package: set() for package in PURE_PACKAGES}
    for path in pure_layer_modules():
        relative = path.relative_to(SOURCE_ROOT)
        scanned[relative.parts[0]].add(relative.as_posix())

    for package, modules in scanned.items():
        assert modules, f"{package} contributed no module to the float ban"
    assert "domain/money.py" in scanned["domain"]


def test_dropping_a_package_from_the_walk_is_visible() -> None:
    """The control. Without it the two tests above are claims about themselves.

    Calling the walk with the tuple the reviewer used shows exactly what it stops
    reaching, which is what the per-package assertion is there to catch.
    """
    wrong_tuple = ("domain",)
    shrunken = pure_layer_modules(wrong_tuple)
    reached = {path.relative_to(SOURCE_ROOT).parts[0] for path in shrunken}

    assert reached == {"domain"}
    assert set(PURE_PACKAGES) - reached == {"services", "providers"}
    # The old assertion, evaluated against the tuple it was written in terms of: still
    # true, which is precisely why it never caught this.
    assert len(shrunken) >= len(wrong_tuple)


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


def test_the_float_ban_catches_an_aliased_import(tmp_path: Path) -> None:
    """`from builtins import float as f` renamed its way straight past the old ban.

    Reported twice on purpose: at the import, which is the line to delete, and at the use,
    which is the line that produces the float.
    """
    module = tmp_path / "aliased.py"
    module.write_text(
        "\n".join(
            [
                "from builtins import float as f",  # 1: the import
                "",  # 2
                "",  # 3
                "def convert(value: str) -> object:",  # 4
                "    return f(value)",  # 5: the use, under the new name
            ]
        ),
        encoding="utf-8",
    )

    violations = find_float_usage(module, module.read_text(encoding="utf-8"))
    located = {(violation.line, violation.reason) for violation in violations}

    assert (1, "import of the builtin float as f") in located
    assert (5, "reference to the builtin float") in located


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param("from builtins import float", id="plain"),
        pytest.param("from builtins import float as f", id="renamed"),
        pytest.param("import builtins.float as f", id="dotted"),
    ],
)
def test_every_import_spelling_of_the_builtin_is_caught(tmp_path: Path, spelling: str) -> None:
    module = tmp_path / "imports.py"
    module.write_text(f"{spelling}\n", encoding="utf-8")

    assert find_float_usage(module, module.read_text(encoding="utf-8")) != []


def test_the_float_ban_catches_dividing_two_integer_literals(tmp_path: Path) -> None:
    """`1 / 3` is a float with no literal and no `float` anywhere in the file.

    The accident rather than the evasion: a future `services/allocation.py` computing a
    share this way would have passed the old ban and produced `0.3333333333333333`.
    """
    module = tmp_path / "division.py"
    module.write_text(
        "\n".join(
            [
                "RATE = 1 / 3",  # 1: the plain case
                "NEGATIVE = -1 / 3",  # 2: a unary minus in front of the literal
                "FLOOR = 1 // 3",  # 3: an int, and not a violation
                "WHOLE = 4 / 2",  # 4: still a float, 2.0
            ]
        ),
        encoding="utf-8",
    )

    lines = {
        violation.line for violation in find_float_usage(module, module.read_text(encoding="utf-8"))
    }

    assert lines == {1, 2, 4}


def test_the_division_rule_does_not_fire_on_values_it_cannot_decide(tmp_path: Path) -> None:
    """The documented limit, asserted so it is a decision and not an oversight.

    `a / b` is a float for two ints and a `Decimal` for two `Decimal`s, and nothing in the
    AST says which. Flagging it would ban correct `Decimal` arithmetic in the one package
    that exists to do `Decimal` arithmetic, so the ban stops here and the boundary guards
    take over. If this test ever starts failing because the rule got broader, read the
    module docstring before widening it further.
    """
    module = tmp_path / "undecidable.py"
    module.write_text(
        "\n".join(
            [
                "from decimal import Decimal",
                "",
                "",
                "def share(part: Decimal, whole: Decimal) -> Decimal:",
                "    return part / whole",
                "",
                "",
                "def ratio(part: int, whole: int) -> object:",
                "    return part / whole",
            ]
        ),
        encoding="utf-8",
    )

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


def test_the_numeric_ban_catches_an_aliased_import(tmp_path: Path) -> None:
    """The same hole the float ban had, in the sibling that was supposed to be the model.

    `find_numeric_usage` resolved an alias as `node.asname or node.name`, so
    `from sqlalchemy import Numeric as N` bound the name `N`, matched nothing, and let
    `N(38, 20)` build a column that round-trips money through a C double.
    """
    module = tmp_path / "aliased_numeric.py"
    module.write_text(
        "from sqlalchemy import Numeric as N\n\nPRICE = N(38, 20)\n",
        encoding="utf-8",
    )

    lines = {violation.line for violation in find_numeric_usage(module, module.read_text("utf-8"))}

    assert lines == {1, 3}


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
