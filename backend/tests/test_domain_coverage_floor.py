"""Criterion 13 of #17: 95% lines and 90% branches on `src/portfolio/domain/`, enforced.

Three joins, because a floor can be defeated at any of them without a single red test:

* **the script** must compute the right thing -- sum over the domain's files only, compare
  exactly, and refuse (not pass) a report it cannot use. It is run here as a subprocess
  against synthetic coverage.py JSON reports, including ones built to sit just below a floor
  where a rounded comparison would pass;
* **the gate** (`scripts/check.py`, full mode) must run it straight after the pytest run
  that wrote the report it reads;
* **CI** must do the same in the `Backend tests` job.

The repository-wide floor in `backend/pyproject.toml` is asserted not to have dropped, since
criterion 13 adds a floor rather than trading one for another.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO_ROOT / "scripts" / "domain_coverage.py"
CHECK_SCRIPT: Final = REPO_ROOT / "scripts" / "check.py"
CI_WORKFLOW: Final = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT: Final = REPO_ROOT / "backend" / "pyproject.toml"

PASSED: Final = 0
BELOW_FLOOR: Final = 1
UNUSABLE: Final = 2


def load_script(path: Path, name: str) -> ModuleType:
    """Import a script from `scripts/` by path, without running its `__main__` block."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def summary(covered: int, statements: int, covered_branches: int, branches: int) -> dict[str, int]:
    return {
        "covered_lines": covered,
        "num_statements": statements,
        "covered_branches": covered_branches,
        "num_branches": branches,
    }


def report(files: dict[str, dict[str, int]], *, branch_coverage: object = True) -> dict[str, Any]:
    """A coverage.py JSON report of format 3, with only the fields the floor reads."""
    return {
        "meta": {"format": 3, "branch_coverage": branch_coverage},
        "files": {path: {"summary": figures} for path, figures in files.items()},
    }


def run_floor(tmp_path: Path, document: object, *, raw: str | None = None) -> tuple[int, str]:
    path = tmp_path / "coverage.json"
    path.write_text(raw if raw is not None else json.dumps(document), encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [sys.executable, str(SCRIPT), str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


DOMAIN_FILE: Final = "src/portfolio/domain/portfolio.py"


# --------------------------------------------------------------------------------------
# The script
# --------------------------------------------------------------------------------------


def test_the_floors_are_95_lines_and_90_branches_on_the_domain() -> None:
    module = load_script(SCRIPT, "domain_coverage_under_test")

    assert module.LINE_FLOOR_PERCENT == 95
    assert module.BRANCH_FLOOR_PERCENT == 90
    assert module.DOMAIN_PATH == "src/portfolio/domain/"


def test_exactly_at_both_floors_passes(tmp_path: Path) -> None:
    code, output = run_floor(tmp_path, report({DOMAIN_FILE: summary(95, 100, 90, 100)}))

    assert code == PASSED, output


@pytest.mark.parametrize(
    ("figures", "below"),
    [
        pytest.param(summary(94, 100, 100, 100), "lines", id="lines one point under"),
        pytest.param(summary(100, 100, 89, 100), "branches", id="branches one point under"),
        # 94.995% and 89.995%: both print as the floor when rounded to two places, and a
        # comparison of rounded percentages would pass them. The integer one does not.
        pytest.param(summary(18999, 20000, 100, 100), "lines", id="lines 94.995"),
        pytest.param(summary(100, 100, 17999, 20000), "branches", id="branches 89.995"),
        pytest.param(summary(0, 100, 0, 100), "lines and branches", id="both"),
    ],
)
def test_below_either_floor_fails(tmp_path: Path, figures: dict[str, int], below: str) -> None:
    code, output = run_floor(tmp_path, report({DOMAIN_FILE: figures}))

    assert code == BELOW_FLOOR, output
    assert below in output, output


def test_the_domain_is_summed_across_its_files(tmp_path: Path) -> None:
    """One file at 100% and one at 80%, equal in size: 90% of lines, under the line floor."""
    document = report(
        {
            "src/portfolio/domain/money.py": summary(100, 100, 50, 50),
            "src/portfolio/domain/portfolio.py": summary(80, 100, 50, 50),
        }
    )

    code, output = run_floor(tmp_path, document)

    assert code == BELOW_FLOOR, output


def test_files_outside_the_domain_do_not_count(tmp_path: Path) -> None:
    """Uncovered services, and a sibling package whose name merely starts with `domain`."""
    document = report(
        {
            DOMAIN_FILE: summary(100, 100, 100, 100),
            "src/portfolio/services/prices.py": summary(0, 500, 0, 500),
            "src/portfolio/domain_extras/helpers.py": summary(0, 500, 0, 500),
        }
    )

    code, output = run_floor(tmp_path, document)

    assert code == PASSED, output


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("src\\portfolio\\domain\\portfolio.py", id="windows separators"),
        pytest.param("/home/runner/work/backend/src/portfolio/domain/money.py", id="absolute"),
    ],
)
def test_every_spelling_of_a_domain_path_counts(tmp_path: Path, path: str) -> None:
    """Shown by making the only domain file uncovered: if it were skipped, the report would
    be unusable (no domain file) rather than below the floor."""
    code, output = run_floor(tmp_path, report({path: summary(0, 100, 0, 100)}))

    assert code == BELOW_FLOOR, output


@pytest.mark.parametrize(
    ("document", "raw"),
    [
        pytest.param(None, "{not json", id="not JSON"),
        pytest.param(None, "", id="empty file"),
        pytest.param([1, 2, 3], None, id="not an object"),
        pytest.param(
            report({DOMAIN_FILE: summary(100, 100, 100, 100)}, branch_coverage=False),
            None,
            id="no branch coverage",
        ),
        pytest.param(
            {"files": {DOMAIN_FILE: {"summary": summary(1, 1, 1, 1)}}}, None, id="no meta"
        ),
        pytest.param(
            report({"src/portfolio/services/x.py": summary(1, 1, 1, 1)}), None, id="no domain file"
        ),
        pytest.param(report({DOMAIN_FILE: summary(0, 0, 1, 1)}), None, id="no statements"),
        pytest.param(report({DOMAIN_FILE: summary(1, 1, 0, 0)}), None, id="no branches"),
        pytest.param(
            {"meta": {"branch_coverage": True}, "files": {DOMAIN_FILE: {}}}, None, id="no summary"
        ),
        pytest.param(
            {"meta": {"branch_coverage": True}, "files": {DOMAIN_FILE: {"summary": {"x": 1}}}},
            None,
            id="summary without figures",
        ),
    ],
)
def test_an_unusable_report_fails_closed(tmp_path: Path, document: object, raw: str | None) -> None:
    """Exit 2, never 0: a floor whose subject has gone missing must not report success."""
    code, output = run_floor(tmp_path, document, raw=raw)

    assert code == UNUSABLE, output


def test_a_missing_report_fails_closed(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [sys.executable, str(SCRIPT), str(tmp_path / "never-written.json")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == UNUSABLE, result.stdout + result.stderr


@pytest.mark.parametrize("arguments", [[], ["a.json", "b.json"]], ids=["none", "two"])
def test_the_wrong_number_of_arguments_fails_closed(arguments: list[str]) -> None:
    result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [sys.executable, str(SCRIPT), *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == UNUSABLE


# --------------------------------------------------------------------------------------
# The gate and CI run it, over the report the coverage run just wrote
# --------------------------------------------------------------------------------------


def test_the_full_gate_runs_the_floor_right_after_pytest(tmp_path: Path) -> None:
    check = load_script(CHECK_SCRIPT, "check_under_test")

    steps = check.backend_steps(False, tmp_path)
    names = [name for name, _, _ in steps]
    report_path = tmp_path / "coverage.json"

    assert names.index("domain coverage") == names.index("pytest") + 1
    _, pytest_command, _ = steps[names.index("pytest")]
    assert "--cov" in pytest_command
    assert f"--cov-report=json:{report_path}" in pytest_command
    _, floor_command, floor_cwd = steps[names.index("domain coverage")]
    assert floor_command == [sys.executable, "scripts/domain_coverage.py", str(report_path)]
    assert floor_cwd == REPO_ROOT


def test_the_fast_gate_skips_the_floor(tmp_path: Path) -> None:
    """The fast gate runs pytest without coverage, so it has no report to hold a floor to."""
    check = load_script(CHECK_SCRIPT, "check_under_test_fast")

    steps = check.backend_steps(True, tmp_path)

    assert "domain coverage" not in [name for name, _, _ in steps]


def backend_test_steps() -> list[dict[str, str]]:
    """The `test-backend` job's steps, each as its single-line `name`, `run` and so on.

    Read from the text rather than through a YAML parser: PyYAML is only a transitive
    dependency here, and the steps this checks are flat. A step is a line opening with
    `- ` at the steps' indentation; its keys are the `key: value` lines under it.
    """
    lines = CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = lines.index("  test-backend:")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("  ")
            and not lines[index].startswith("   ")
            and lines[index].strip().endswith(":")
        ),
        len(lines),
    )
    steps: list[dict[str, str]] = []
    for line in lines[start:end]:
        stripped = line.strip()
        if line.startswith("      - "):
            steps.append({})
            stripped = stripped.removeprefix("- ")
        if not steps or line.lstrip().startswith("#") or ": " not in stripped:
            continue
        if line.startswith("        ") or line.startswith("      - "):
            field, _, value = stripped.partition(": ")
            steps[-1].setdefault(field, value)
    return steps


def test_ci_runs_the_floor_right_after_the_coverage_run() -> None:
    steps = backend_test_steps()
    names = [step.get("name") for step in steps]

    coverage_step = steps[names.index("pytest with coverage")]
    floor_step = steps[names.index("Domain coverage floor")]

    assert names.index("Domain coverage floor") == names.index("pytest with coverage") + 1
    assert '--cov-report="json:$RUNNER_TEMP/coverage.json"' in coverage_step["run"]
    assert "--cov " in coverage_step["run"] + " "
    assert (
        floor_step["run"].strip()
        == 'python3 scripts/domain_coverage.py "$RUNNER_TEMP/coverage.json"'
    )
    # From the repository root, where `scripts/` is: no working directory set.
    assert "working-directory" not in floor_step
    assert coverage_step["working-directory"] == "backend"


def test_the_repository_wide_floor_has_not_dropped() -> None:
    """Criterion 13 adds a domain floor; the global 99.7 only ever ratchets upward."""
    configuration = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

    assert configuration["tool"]["coverage"]["report"]["fail_under"] >= 99.7


# --------------------------------------------------------------------------------------
# N5: the floor is skipped only when pytest was skipped
# --------------------------------------------------------------------------------------
#
# `run_steps` is what `main` runs the gate through. The pytest step is replaced by a stand-in
# whose outcome is chosen -- an executable that does not exist (SKIPPED), a Python that exits
# 1 (FAILED) or 0 (PASSED) -- and the floor step is the real one, over a report path.

MISSING_TOOL: Final = "portfolio-check-no-such-tool-17"


def gate_steps(pytest_command: list[str], report_path: Path) -> list[tuple[str, list[str], Path]]:
    return [
        ("pytest", pytest_command, REPO_ROOT),
        (
            "domain coverage",
            [sys.executable, "scripts/domain_coverage.py", str(report_path)],
            REPO_ROOT,
        ),
    ]


def test_the_floor_waits_on_pytest_and_nothing_else() -> None:
    check = load_script(CHECK_SCRIPT, "check_under_test_prerequisites")

    assert check.PREREQUISITES == {"domain coverage": "pytest"}


def test_a_skipped_pytest_skips_the_floor_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`uv` missing: pytest is skipped, and the floor is skipped rather than failed as unusable."""
    check = load_script(CHECK_SCRIPT, "check_under_test_skip")

    failures = check.run_steps(gate_steps([MISSING_TOOL, "run", "pytest"], tmp_path / "none.json"))
    printed = capsys.readouterr().out.splitlines()

    assert failures == []
    assert f"  SKIP  pytest: {MISSING_TOOL} is not installed" in printed
    assert (
        "  SKIP  domain coverage: pytest was skipped, so there is no coverage report to hold "
        "to the floor"
    ) in printed


def test_a_failed_pytest_with_no_report_fails_both(tmp_path: Path) -> None:
    """Only a skip propagates: pytest ran, so the floor runs, and fails closed on no report."""
    check = load_script(CHECK_SCRIPT, "check_under_test_failed")
    failing = [sys.executable, "-c", "raise SystemExit(1)"]

    failures = check.run_steps(gate_steps(failing, tmp_path / "never-written.json"))

    assert failures == ["pytest", "domain coverage"]


def test_a_passing_pytest_with_no_report_fails_the_floor(tmp_path: Path) -> None:
    check = load_script(CHECK_SCRIPT, "check_under_test_passed")
    passing = [sys.executable, "-c", "raise SystemExit(0)"]

    failures = check.run_steps(gate_steps(passing, tmp_path / "never-written.json"))

    assert failures == ["domain coverage"]


def test_a_passing_pytest_with_a_passing_report_passes(tmp_path: Path) -> None:
    check = load_script(CHECK_SCRIPT, "check_under_test_green")
    report_path = tmp_path / "coverage.json"
    report_path.write_text(
        json.dumps(report({DOMAIN_FILE: summary(100, 100, 100, 100)})), encoding="utf-8"
    )
    passing = [sys.executable, "-c", "raise SystemExit(0)"]

    assert check.run_steps(gate_steps(passing, report_path)) == []


def test_main_runs_the_gate_through_run_steps() -> None:
    """The helper is only worth pinning if `main` uses it, and not a copy of the old loop."""
    source = CHECK_SCRIPT.read_text(encoding="utf-8")
    main_body = source[source.index("def main(") :]

    assert "run_steps(steps)" in main_body
    assert "if not run(step)" not in source
