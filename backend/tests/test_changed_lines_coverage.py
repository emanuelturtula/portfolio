"""Spec 030 (#23), criterion 10: `diff-cover` holds the lines a pull request changes to 90%.

Two joins, as `tests/test_domain_coverage_floor.py` pins its floor's:

* **the workflow**, read as text: in both test jobs the checkout fetches the whole history,
  the coverage run writes the report the step reads, and the step runs on pull requests only,
  in the right directory, with exactly the comparison and the threshold the spec names. The
  tool comes from `backend/uv.lock`, and the frontend's LCOV names files from the repository
  root;
* **the tool**, run here with the workflow's own arguments over a throwaway repository: an
  uncovered changed line fails it, a covered one passes, from `backend/` with Cobertura XML
  and from `frontend/` with LCOV -- and an LCOV that names files from `frontend/` instead
  measures nothing and passes, which is the trap `projectRoot` in `vite.config.ts` avoids.

The local gate, `scripts/check.py`, has no diff to compare and does not run the tool.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
CI_WORKFLOW: Final = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT: Final = REPO_ROOT / "backend" / "pyproject.toml"
LOCKFILE: Final = REPO_ROOT / "backend" / "uv.lock"
VITE_CONFIG: Final = REPO_ROOT / "frontend" / "vite.config.ts"
CHECK_SCRIPT: Final = REPO_ROOT / "scripts" / "check.py"

PULL_REQUESTS_ONLY: Final = "github.event_name == 'pull_request'"
BACKEND_RUN: Final = (
    'uv run diff-cover "$RUNNER_TEMP/coverage.xml" --compare-branch=origin/main --fail-under=90'
)
FRONTEND_RUN: Final = (
    "uv run --locked --project ../backend diff-cover coverage/lcov.info "
    "--compare-branch=origin/main --fail-under=90"
)
STEP_NAME: Final = "Changed-lines coverage"


def job_steps(job: str) -> list[dict[str, str]]:
    """A job's steps, each as its single-line keys, and its `with:` keys as `with.<key>`.

    Read from the text, as `test_domain_coverage_floor.backend_test_steps` reads it, and for
    its reason: the steps are flat, and PyYAML is only a transitive dependency. A step opens
    with `- ` at six spaces; its own keys are at eight; a `with:` block's keys are at ten.
    """
    lines = CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  {job}:")
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
        if line.startswith("      - "):
            steps.append({})
            line = "        " + line.removeprefix("      - ")
        if not steps or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        field, separator, value = stripped.partition(":")
        if not separator:
            continue
        value = value.split(" #", 1)[0].strip()
        if line.startswith("          ") and not line.startswith("           "):
            steps[-1].setdefault(f"with.{field}", value)
        elif line.startswith("        ") and not line.startswith("         "):
            steps[-1].setdefault(field, value)
    return steps


def job_setting(job: str, key: str) -> str:
    lines = CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  {job}:")
    for line in lines[start + 1 :]:
        if line.startswith(f"    {key}:"):
            return line.split(":", 1)[1].split("#", 1)[0].strip()
        if line.startswith("  ") and not line.startswith("   "):
            break
    message = f"{job} has no {key}"
    raise AssertionError(message)


def named(steps: list[dict[str, str]], name: str) -> tuple[int, dict[str, str]]:
    [found] = [(index, step) for index, step in enumerate(steps) if step.get("name") == name]
    return found


def test_the_parser_reads_a_nested_with_key_and_ignores_comments() -> None:
    """The control for every pin below: a parser that read nothing would pass them vacuously."""
    steps = job_steps("test-backend")

    assert steps[0]["uses"].startswith("actions/checkout@")
    assert steps[0]["with.persist-credentials"] == "false"
    assert all(not key.startswith("#") for step in steps for key in step)


@pytest.mark.parametrize("job", ["test-backend", "test-frontend"])
def test_each_test_job_checks_out_the_whole_history(job: str) -> None:
    """`--compare-branch=origin/main` diffs from the merge base, which a shallow clone lacks."""
    checkout = job_steps(job)[0]

    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with.fetch-depth"] == "0"
    assert checkout["with.persist-credentials"] == "false"


def test_the_backend_step_reads_the_xml_the_coverage_run_writes() -> None:
    steps = job_steps("test-backend")
    coverage_index, coverage = named(steps, "pytest with coverage")
    floor_index, _floor = named(steps, "Domain coverage floor")
    changed_index, changed = named(steps, STEP_NAME)

    assert '--cov-report="xml:$RUNNER_TEMP/coverage.xml"' in coverage["run"]
    assert '--cov-report="json:$RUNNER_TEMP/coverage.json"' in coverage["run"]
    assert coverage_index < floor_index < changed_index
    assert changed == {
        "name": STEP_NAME,
        "if": PULL_REQUESTS_ONLY,
        "working-directory": "backend",
        "run": BACKEND_RUN,
    }


def test_the_frontend_step_installs_the_locked_tool_and_reads_the_lcov() -> None:
    steps = job_steps("test-frontend")
    vitest_index, vitest = named(steps, "vitest with coverage")
    install_index, install = named(steps, "Install diff-cover")
    changed_index, changed = named(steps, STEP_NAME)
    [(uv_index, setup_uv)] = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    ]

    assert vitest["run"] == "npm run test:coverage"
    assert vitest_index < uv_index < install_index < changed_index
    assert setup_uv["if"] == PULL_REQUESTS_ONLY
    assert setup_uv["with.working-directory"] == "backend"
    assert install == {
        "name": "Install diff-cover",
        "if": PULL_REQUESTS_ONLY,
        "working-directory": "backend",
        "run": "uv sync --locked",
    }
    assert changed == {
        "name": STEP_NAME,
        "if": PULL_REQUESTS_ONLY,
        "working-directory": "frontend",
        "run": FRONTEND_RUN,
    }


def test_the_frontend_job_has_room_for_the_new_steps() -> None:
    assert job_setting("test-frontend", "timeout-minutes") == "10"


def test_the_tool_is_a_locked_backend_dev_dependency() -> None:
    configuration = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    lock = tomllib.loads(LOCKFILE.read_text(encoding="utf-8"))

    assert "diff-cover" in configuration["dependency-groups"]["dev"]
    assert [package["version"] for package in lock["package"] if package["name"] == "diff-cover"]


def test_the_lcov_names_files_from_the_repository_root() -> None:
    config = VITE_CONFIG.read_text(encoding="utf-8")

    assert "['lcov', { projectRoot: fileURLToPath(new URL('..', import.meta.url)) }]" in config
    assert "'lcov'," not in config.replace("['lcov',", "")


def test_the_local_gate_does_not_run_the_tool() -> None:
    """There is no pull request to compare against on a laptop."""
    assert "diff-cover" not in CHECK_SCRIPT.read_text(encoding="utf-8")
    assert "diff_cover" not in CHECK_SCRIPT.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# The tool itself, with the workflow's arguments, over a throwaway repository
# --------------------------------------------------------------------------------------

GIT: Final = shutil.which("git")
COVERED: Final = 0
BELOW: Final = 1


def git(repository: Path, *arguments: str) -> None:
    assert GIT is not None
    subprocess.run(  # noqa: S603 - a fixed executable and arguments the test wrote
        [GIT, "-c", "user.name=tester", "-c", "user.email=tester@example.invalid", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """`main` with one file per side, then a branch that adds one line to each."""
    root = tmp_path / "r"
    (root / "backend" / "src").mkdir(parents=True)
    (root / "frontend" / "src").mkdir(parents=True)
    (root / "backend" / "src" / "m.py").write_text("a = 1\n", encoding="utf-8")
    (root / "frontend" / "src" / "m.ts").write_text("export const a = 1\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(root, "checkout", "-q", "-b", "feature")
    (root / "backend" / "src" / "m.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    (root / "frontend" / "src" / "m.ts").write_text(
        "export const a = 1\nexport const b = 2\n", encoding="utf-8"
    )
    git(root, "commit", "-q", "-am", "change")
    return root


def diff_cover(directory: Path, report: str) -> int:
    """The workflow's command, from the workflow's directory."""
    completed = subprocess.run(  # noqa: S603 - the venv's own interpreter and a fixed module
        [
            sys.executable,
            "-m",
            "diff_cover.diff_cover_tool",
            report,
            "--compare-branch=origin/main",
            "--fail-under=90",
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode


def cobertura(line_two_hits: int) -> str:
    return (
        '<?xml version="1.0" ?><coverage version="7" line-rate="0.5" branch-rate="0">'
        '<sources><source>src</source></sources><packages><package name="."><classes>'
        '<class name="m.py" filename="m.py" line-rate="0.5"><methods/><lines>'
        '<line number="1" hits="1"/>'
        f'<line number="2" hits="{line_two_hits}"/>'
        "</lines></class></classes></package></packages></coverage>"
    )


def lcov(path: str, line_two_hits: int) -> str:
    hit = 1 + (line_two_hits > 0)
    return f"TN:\nSF:{path}\nDA:1,1\nDA:2,{line_two_hits}\nLF:2\nLH:{hit}\nend_of_record\n"


@pytest.mark.skipif(GIT is None, reason="git is needed to build the throwaway repository")
@pytest.mark.parametrize(("hits", "expected"), [(0, BELOW), (3, COVERED)])
def test_the_backend_command_fails_an_uncovered_changed_line(
    repository: Path, hits: int, expected: int
) -> None:
    report = repository / "coverage.xml"
    report.write_text(cobertura(hits), encoding="utf-8")

    assert diff_cover(repository / "backend", str(report)) == expected


@pytest.mark.skipif(GIT is None, reason="git is needed to build the throwaway repository")
@pytest.mark.parametrize(("hits", "expected"), [(0, BELOW), (3, COVERED)])
def test_the_frontend_command_fails_an_uncovered_changed_line_named_from_the_root(
    repository: Path, hits: int, expected: int
) -> None:
    (repository / "frontend" / "coverage").mkdir()
    (repository / "frontend" / "coverage" / "lcov.info").write_text(
        lcov("frontend/src/m.ts", hits), encoding="utf-8"
    )

    assert diff_cover(repository / "frontend", "coverage/lcov.info") == expected


@pytest.mark.skipif(GIT is None, reason="git is needed to build the throwaway repository")
def test_an_lcov_named_from_the_frontend_measures_nothing_and_passes(repository: Path) -> None:
    """The trap `projectRoot` avoids: the same uncovered line, named `src/m.ts`, passes."""
    (repository / "frontend" / "coverage").mkdir()
    (repository / "frontend" / "coverage" / "lcov.info").write_text(
        lcov("src/m.ts", 0), encoding="utf-8"
    )

    assert diff_cover(repository / "frontend", "coverage/lcov.info") == COVERED
