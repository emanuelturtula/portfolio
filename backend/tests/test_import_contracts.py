"""Criterion 2 of #9: no request path reaches a price provider, proven able to fail.

The criterion reads "a test asserts no request-path code reaches a price provider", and the
spec answers it with an `import-linter` contract rather than a unit test, because it is a
statement about the import graph. That leaves this module with a harder job than asserting
a rule: **a contract file that is present but misconfigured passes silently**, and
`import-linter` says nothing at all about a `forbidden_modules` entry that does not exist
yet. Until `portfolio.providers.prices` lands, the shipped contract is green because there
is nothing to find -- which is the exact failure shape this repository keeps rediscovering.

So there are two halves and the second is the one that matters.

**The shipped contract's text is pinned**, including the one thing that is invisible in a
passing run: `allow_indirect_imports` is *absent*, deliberately, unlike the thin-routers
contract one section above it which sets it. That single missing line is the difference
between catching `router -> service -> price provider` and not.

**And the contract is shown catching that chain.** A shadow `portfolio` package is planted
under `tmp_path` -- a router, a service, a price provider, three imports -- and the
**real, unmodified `backend/.importlinter`** is run against it with `--contract`. Nothing
is paraphrased: the bytes checked are the bytes that ship. The same tree is then checked
against a copy of that file with `allow_indirect_imports = True` inserted, which reports
nothing, so the test discriminates between the shipped contract and the one it is
deliberately not.

## Why a shadow package rather than a planted module in `src/`

`backend/tests/**` is this agent's to write and `backend/src/**` is not, but the reason is
better than ownership: a violating module committed into the real package would have to be
deleted again, and a test that is only true between two edits is not a test. The shadow
tree is built, checked and thrown away inside one `tmp_path`, and the subprocess's
`PYTHONPATH` is what makes `portfolio` resolve to it -- so nothing in this process ever
imports it and the rest of the suite cannot be affected by it.

The same shape as `tests/providers/test_protocol.py`, which proves the `mypy --strict`
conformance check can reject a provider, for the same reason: a guard nobody has ever seen
fail is a guard nobody knows the state of.
"""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
BACKEND_ROOT: Final = REPO_ROOT / "backend"
IMPORT_LINTER_CONFIG: Final = BACKEND_ROOT / ".importlinter"
CHECK_SCRIPT: Final = REPO_ROOT / "scripts" / "check.py"
CI_WORKFLOW: Final = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The section header of criterion 2's contract, and the identifier `--contract` takes.
#: Pinned as a literal rather than discovered by searching for "prices": a test that went
#: looking for whichever contract mentioned price providers would keep passing after the
#: contract was renamed to something that no longer means what this one means.
PRICES_CONTRACT_ID: Final = "prices-are-never-fetched-in-a-request"
PRICES_CONTRACT_SECTION: Final = f"importlinter:contract:{PRICES_CONTRACT_ID}"

#: The sibling contract that *does* set `allow_indirect_imports`, named here so that the
#: absence asserted below reads as a decision between two available spellings rather than
#: as a line somebody forgot. Both are in one file; only one of them allows the chain.
THIN_ROUTERS_SECTION: Final = "importlinter:contract:thin-routers"

ROUTERS_MODULE: Final = "portfolio.api.routers"
PRICES_MODULE: Final = "portfolio.providers.prices"

#: The three modules the shadow tree wires together. `dashboard` is the realistic router:
#: the spec's own example of how this violation arrives is somebody adding "just refresh it
#: if it is stale" to the valuation service, and every dashboard render becoming a vendor
#: call.
SHADOW_ROUTER: Final = "portfolio/api/routers/dashboard.py"
SHADOW_SERVICE: Final = "portfolio/services/valuation.py"
SHADOW_PROVIDER: Final = "portfolio/providers/prices/kraken.py"

#: Every package `__init__.py` the shadow tree needs for `import-linter` to walk it.
SHADOW_PACKAGES: Final[tuple[str, ...]] = (
    "portfolio",
    "portfolio/api",
    "portfolio/api/routers",
    "portfolio/services",
    "portfolio/providers",
    "portfolio/providers/prices",
)


def configuration() -> configparser.ConfigParser:
    """The shipped `.importlinter`, parsed.

    `configparser` rather than a substring search over the text: `allow_indirect_imports`
    appears in this file twice in prose -- the thin-routers contract explains why it sets
    the flag, and the prices contract explains why it does not -- so any assertion made
    against the raw characters would be answered by a comment.
    """
    assert IMPORT_LINTER_CONFIG.is_file(), f"{IMPORT_LINTER_CONFIG} does not exist"
    parser = configparser.ConfigParser()
    parser.read(IMPORT_LINTER_CONFIG, encoding="utf-8")
    return parser


def module_list(parser: configparser.ConfigParser, section: str, option: str) -> list[str]:
    """One of `import-linter`'s newline-separated module lists, as a list of names."""
    return parser.get(section, option).split()


def lint_imports_executable() -> str:
    """The `lint-imports` console script from the environment running these tests.

    Resolved beside `sys.executable` rather than off `PATH`, for the reason
    `tests/providers/test_protocol.py` gives about `mypy`: this has to be the same
    `import-linter` the gate runs (`uv run lint-imports`), out of the same environment,
    without depending on `uv` being on `PATH` inside the pytest process.

    Failing rather than skipping is deliberate. A skipped control is a control nobody
    knows the state of, which is the condition this whole module exists to remove.
    """
    resolved = shutil.which("lint-imports", path=str(Path(sys.executable).parent))
    assert resolved is not None, "lint-imports is not installed beside the interpreter"
    return resolved


def plant_shadow_package(root: Path, *, service_reaches_prices: bool, router_reaches: str) -> None:
    """Write a throwaway `portfolio` package under `root`, with a chosen import graph.

    `router_reaches` is the module the router imports -- the service, for the indirect
    chain, or the price provider itself for the direct one. `service_reaches_prices`
    decides whether the chain completes.

    Every module is a plain import and a `__all__`, because `import-linter` reads the
    import graph and nothing else: what the modules would *do* is irrelevant, and anything
    they did would only be another thing that could break the harness.
    """
    for package in SHADOW_PACKAGES:
        directory = root / package
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("", encoding="utf-8")

    (root / SHADOW_PROVIDER).write_text('QUOTE = "1"\n', encoding="utf-8")

    service_body = (
        "from portfolio.providers.prices import kraken\n\n__all__ = ['kraken']\n"
        if service_reaches_prices
        else "__all__: list[str] = []\n"
    )
    (root / SHADOW_SERVICE).write_text(service_body, encoding="utf-8")

    package, _, name = router_reaches.rpartition(".")
    router_body = f"from {package} import {name}\n\n__all__ = ['{name}']\n"
    (root / SHADOW_ROUTER).write_text(router_body, encoding="utf-8")


def relaxed_configuration(destination: Path) -> Path:
    """The shipped config with `allow_indirect_imports = True` added to the prices contract.

    Built by inserting one line into the real file rather than by writing a second config
    by hand, so the two runs this module compares differ in exactly that line and in
    nothing else. If they differed in anything more, a passing "relaxed" run would not be
    evidence about the flag.
    """
    lines = IMPORT_LINTER_CONFIG.read_text(encoding="utf-8").splitlines()
    header = f"[{PRICES_CONTRACT_SECTION}]"
    assert header in lines, f"{header} is not in {IMPORT_LINTER_CONFIG}"
    index = lines.index(header)
    relaxed = [*lines[: index + 1], "allow_indirect_imports = True", *lines[index + 1 :]]
    destination.write_text("\n".join(relaxed) + "\n", encoding="utf-8")
    return destination


def run_lint_imports(
    *,
    package_root: Path,
    config: Path,
    contracts: Sequence[str] = (PRICES_CONTRACT_ID,),
) -> subprocess.CompletedProcess[str]:
    """Run `lint-imports` over `package_root`, against `config`, for `contracts` only.

    Three details, each of which has its own way of producing a meaningless verdict:

    * **`PYTHONPATH` at `package_root`, and the real `src` removed from it.** The shadow
      tree is named `portfolio` so that the shipped contract -- which names
      `portfolio.api.routers` and `portfolio.providers.prices` literally -- applies to it
      unaltered. That only works if the shadow is what `import-linter` imports, so the real
      package must not be reachable. `PYTHONPATH` entries precede the editable install's,
      and `cwd` is the shadow root, but an inherited `PYTHONPATH` naming `backend/src`
      would still win a race nobody wants to think about, so it is dropped outright.
    * **`--no-cache`.** A cache keyed on a path would be shared between the two runs this
      module compares, and the second one would report the first one's graph.
    * **`--contract`.** The shipped file holds four contracts, three of which describe the
      real package and are all vacuously true of a three-module shadow. Limiting the run
      means a non-zero exit is about *this* contract and not about one of those.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(package_root)
    environment.pop("PYTHONSAFEPATH", None)
    command = [
        lint_imports_executable(),
        "--config",
        str(config),
        "--no-cache",
        "--no-logo",
        *(argument for contract in contracts for argument in ("--contract", contract)),
    ]
    return subprocess.run(  # noqa: S603 - a fixed argument list, no shell, no user input
        command,
        cwd=package_root,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


# --------------------------------------------------------------------------------------
# Criterion 2, half one: the contract that ships says what it has to say
# --------------------------------------------------------------------------------------


def test_a_request_path_may_not_reach_a_price_provider() -> None:
    """Criterion 2's contract exists, is a `forbidden` contract, and names both ends.

    Asserted field by field rather than as "a contract mentioning prices exists". The
    interesting failure is not a missing contract -- somebody would notice -- but one that
    survives a rename or a reordering with a `source_modules` entry quietly dropped, at
    which point it still appears in the file, still runs in CI, and forbids nothing.
    """
    parser = configuration()

    assert parser.has_section(PRICES_CONTRACT_SECTION), (
        f"{PRICES_CONTRACT_SECTION} is not in {IMPORT_LINTER_CONFIG}"
    )
    assert parser.get(PRICES_CONTRACT_SECTION, "type") == "forbidden"
    assert module_list(parser, PRICES_CONTRACT_SECTION, "source_modules") == [ROUTERS_MODULE]
    assert module_list(parser, PRICES_CONTRACT_SECTION, "forbidden_modules") == [PRICES_MODULE]
    assert parser.get(PRICES_CONTRACT_SECTION, "name").strip() != ""


def test_the_contract_is_not_direct_imports_only() -> None:
    """`allow_indirect_imports` is absent here and present one contract above it.

    This is the whole contract in one line, and it is the line that is invisible when
    everything passes. With the flag, `router -> service -> price provider` is reported as
    the layering working correctly; without it, that chain is the violation. The spec picks
    the second reading deliberately, because the realistic way this rule gets broken is a
    well-meaning "just refresh it if it is stale" in the valuation service.

    Both contracts are asserted in one test on purpose. Asserting only the absence would
    pass for a file in which nobody had ever heard of the option; asserting it against the
    sibling that sets it shows that the absence is a choice between two spellings the
    author had in front of them.
    """
    parser = configuration()

    assert not parser.has_option(PRICES_CONTRACT_SECTION, "allow_indirect_imports"), (
        "the prices contract allows indirect imports, so router -> service -> provider passes"
    )
    assert parser.getboolean(THIN_ROUTERS_SECTION, "allow_indirect_imports") is True
    # And the thin-routers contract still forbids a price provider *directly*, through
    # `portfolio.providers`, so the two contracts overlap rather than leaving a gap
    # between "direct" and "indirect" that neither covers.
    assert "portfolio.providers" in module_list(parser, THIN_ROUTERS_SECTION, "forbidden_modules")


def test_the_layering_check_is_actually_run_by_the_gate_and_by_ci() -> None:
    """A contract nothing executes is a comment with an ini syntax.

    The two halves of this module prove the contract is right and that it can fail. Neither
    proves anybody runs it, and that is the join: delete the `lint-imports` step from
    `scripts/check.py` and from the workflow, and every other assertion here stays green
    while the rule stops being enforced anywhere.
    """
    assert CHECK_SCRIPT.is_file(), CHECK_SCRIPT
    assert CI_WORKFLOW.is_file(), CI_WORKFLOW

    assert "lint-imports" in CHECK_SCRIPT.read_text(encoding="utf-8")
    assert "lint-imports" in CI_WORKFLOW.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# Criterion 2, half two: the contract is shown catching the chain it exists for
# --------------------------------------------------------------------------------------


def test_the_shipped_contract_reports_a_router_reaching_prices_through_a_service(
    tmp_path: Path,
) -> None:
    """The real `.importlinter`, unmodified, run against a planted two-hop chain.

    The bytes checked here are the bytes that ship -- the file is passed to `--config` as
    it stands, not paraphrased into a fixture -- so this cannot go green against a contract
    that was edited after the test was written.

    The chain is reported in full in the output, which is asserted as well: an operator
    reading a broken build needs to see *which* service made the router reach a vendor, and
    a verdict with no path in it sends them to read four modules by hand.
    """
    plant_shadow_package(
        tmp_path,
        service_reaches_prices=True,
        router_reaches="portfolio.services.valuation",
    )

    result = run_lint_imports(package_root=tmp_path, config=IMPORT_LINTER_CONFIG)
    output = result.stdout + result.stderr

    assert result.returncode != 0, f"the contract accepted router -> service -> prices:\n{output}"
    assert "BROKEN" in output, output
    assert "portfolio.api.routers.dashboard" in output, output
    assert "portfolio.services.valuation" in output, output
    assert "portfolio.providers.prices.kraken" in output, output


def test_the_same_tree_without_the_chain_is_accepted(tmp_path: Path) -> None:
    """The control. Without it, the test above passes for a harness that always fails.

    Identical tree, identical config, identical invocation: the only difference is the one
    import inside the service. If planting a package were enough to break the contract --
    a mis-set `PYTHONPATH` reaching the real `portfolio`, a config path typo producing a
    usage error, a shadow package `import-linter` cannot walk -- this goes red and says so
    before the assertion above is believed.
    """
    plant_shadow_package(
        tmp_path,
        service_reaches_prices=False,
        router_reaches="portfolio.services.valuation",
    )

    result = run_lint_imports(package_root=tmp_path, config=IMPORT_LINTER_CONFIG)
    output = result.stdout + result.stderr

    assert result.returncode == 0, f"a router reaching no price provider was reported:\n{output}"
    assert "KEPT" in output, output


def test_allowing_indirect_imports_would_hide_the_chain_this_contract_exists_for(
    tmp_path: Path,
) -> None:
    """The discriminator, and the reason `test_the_contract_is_not_direct_imports_only` matters.

    The same planted chain, checked against the shipped file with exactly one line added.
    It is reported as **kept**: with `allow_indirect_imports = True` the violation the spec
    names is invisible, which is why the shipped contract does not set it.

    Without this, the absence asserted above is a claim about an ini file. With it, the
    absence is a claim about what the build would and would not catch, which is the claim
    criterion 2 is actually making.
    """
    plant_shadow_package(
        tmp_path,
        service_reaches_prices=True,
        router_reaches="portfolio.services.valuation",
    )
    relaxed = relaxed_configuration(tmp_path / "relaxed.importlinter")

    result = run_lint_imports(package_root=tmp_path, config=relaxed)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "KEPT" in output, output


def test_the_relaxed_configuration_still_catches_a_direct_import(tmp_path: Path) -> None:
    """And the control on the discriminator: the relaxed config is not simply broken.

    A `lint-imports` run that exited zero because the config was unparseable, or because
    the inserted line landed in the wrong section and disabled the contract, would satisfy
    the test above for a reason that has nothing to do with indirection. Here the same
    relaxed config is handed a router that imports a price provider *directly*, and it
    reports it -- so the difference between the two runs is the chain and only the chain.
    """
    plant_shadow_package(
        tmp_path,
        service_reaches_prices=False,
        router_reaches="portfolio.providers.prices.kraken",
    )
    relaxed = relaxed_configuration(tmp_path / "relaxed.importlinter")

    result = run_lint_imports(package_root=tmp_path, config=relaxed)
    output = result.stdout + result.stderr

    assert result.returncode != 0, f"a direct router -> price import was allowed:\n{output}"
    assert "BROKEN" in output, output


def test_the_file_holds_exactly_these_five_contracts() -> None:
    """The contract set, pinned against a literal, so a deletion is a red test.

    Exact rather than `>=`, for the reason `APPLICATION_TABLES` in `tests/db/` gives: a
    superset check is satisfied by a file that has lost a contract and gained two, and the
    loss is the thing worth knowing about. `PRICES_CONTRACT_ID` appears here as the literal
    string as well as through the constant, because the constant is also what `--contract`
    is given above -- if the two ever disagreed, every subprocess run in this module would
    fail with a usage error rather than a verdict.
    """
    parser = configuration()

    contracts = {
        section.removeprefix("importlinter:contract:")
        for section in parser.sections()
        if section.startswith("importlinter:contract:")
    }

    assert contracts == {
        "portfolio",
        "thin-routers",
        "framework-free-services",
        "prices-are-never-fetched-in-a-request",
        "api-never-reaches-an-exchange-provider",
    }
    assert PRICES_CONTRACT_ID in contracts
    assert EXCHANGES_CONTRACT_ID in contracts
    assert parser.get("importlinter", "root_package") == "portfolio"


# --------------------------------------------------------------------------------------
# #15 criterion 7: no request path reaches an exchange provider, proven able to fail
# --------------------------------------------------------------------------------------
#
# The same two halves as criterion 2 of #9 above, for a contract with a wider source: the
# whole of `portfolio.api` -- routers, schemas and `dependencies.py` -- may not import
# `portfolio.providers.exchanges`, the package holding `Credentials`, directly or through a
# service. The one sanctioned path from a request to a venue is the coordinator's runner,
# which `portfolio.main` builds, and `main` is outside `portfolio.api`.

#: The section header of #15's contract, and the identifier `--contract` takes.
EXCHANGES_CONTRACT_ID: Final = "api-never-reaches-an-exchange-provider"
EXCHANGES_CONTRACT_SECTION: Final = f"importlinter:contract:{EXCHANGES_CONTRACT_ID}"

API_MODULE: Final = "portfolio.api"
EXCHANGE_PROVIDERS_MODULE: Final = "portfolio.providers.exchanges"

#: The shadow tree's packages. `credentials` is the module the contract exists to wall off.
SHADOW_EXCHANGE_PACKAGES: Final[tuple[str, ...]] = (
    "portfolio",
    "portfolio/api",
    "portfolio/api/routers",
    "portfolio/api/schemas",
    "portfolio/services",
    "portfolio/providers",
    "portfolio/providers/exchanges",
)
SHADOW_CREDENTIALS: Final = "portfolio/providers/exchanges/credentials.py"
SHADOW_READ_SERVICE: Final = "portfolio/services/exchanges.py"


def import_line(module: str) -> str:
    """`from a.b import c` for `a.b.c`, with an `__all__` so nothing is flagged as unused."""
    package, _, name = module.rpartition(".")
    return f"from {package} import {name}\n\n__all__ = ['{name}']\n"


def plant_exchange_shadow(
    root: Path,
    *,
    importer: str,
    reaches: str | None,
    service_reaches_provider: bool,
) -> None:
    """A throwaway `portfolio` whose `importer` module imports `reaches`, if anything.

    `importer` is a path under the shadow root, `reaches` a dotted module name. The read
    service imports the credentials module when `service_reaches_provider` is set, which is
    what makes `router -> service -> provider` a chain rather than two unrelated imports.
    """
    for package in SHADOW_EXCHANGE_PACKAGES:
        directory = root / package
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (root / SHADOW_CREDENTIALS).write_text('FIELD = "api_key"\n', encoding="utf-8")
    service_body = (
        import_line("portfolio.providers.exchanges.credentials")
        if service_reaches_provider
        else "__all__: list[str] = []\n"
    )
    (root / SHADOW_READ_SERVICE).write_text(service_body, encoding="utf-8")
    importer_path = root / importer
    importer_path.parent.mkdir(parents=True, exist_ok=True)
    body = "__all__: list[str] = []\n" if reaches is None else import_line(reaches)
    importer_path.write_text(body, encoding="utf-8")


def relaxed_exchange_configuration(destination: Path) -> Path:
    """The shipped config with `allow_indirect_imports = True` added to #15's contract only."""
    lines = IMPORT_LINTER_CONFIG.read_text(encoding="utf-8").splitlines()
    header = f"[{EXCHANGES_CONTRACT_SECTION}]"
    assert header in lines, f"{header} is not in {IMPORT_LINTER_CONFIG}"
    index = lines.index(header)
    relaxed = [*lines[: index + 1], "allow_indirect_imports = True", *lines[index + 1 :]]
    destination.write_text("\n".join(relaxed) + "\n", encoding="utf-8")
    return destination


def run_exchange_contract(package_root: Path, config: Path) -> tuple[int, str]:
    result = run_lint_imports(
        package_root=package_root, config=config, contracts=(EXCHANGES_CONTRACT_ID,)
    )
    return result.returncode, result.stdout + result.stderr


def test_the_exchange_contract_walls_off_the_whole_api_package() -> None:
    """Forbidden, `portfolio.api` entire as the source, the exchange providers as the target.

    Field by field, and `allow_indirect_imports` absent, for the reasons the prices contract's
    tests give: the dangerous failure is a contract that still exists and forbids nothing.
    """
    parser = configuration()

    assert parser.has_section(EXCHANGES_CONTRACT_SECTION)
    assert parser.get(EXCHANGES_CONTRACT_SECTION, "type") == "forbidden"
    assert module_list(parser, EXCHANGES_CONTRACT_SECTION, "source_modules") == [API_MODULE]
    assert module_list(parser, EXCHANGES_CONTRACT_SECTION, "forbidden_modules") == [
        EXCHANGE_PROVIDERS_MODULE
    ]
    assert not parser.has_option(EXCHANGES_CONTRACT_SECTION, "allow_indirect_imports")
    assert parser.get(EXCHANGES_CONTRACT_SECTION, "name").strip() != ""


def test_the_shipped_exchange_contract_reports_a_router_reaching_a_provider_through_a_service(
    tmp_path: Path,
) -> None:
    """The planted violation: a router, the read service, the credentials module."""
    plant_exchange_shadow(
        tmp_path,
        importer="portfolio/api/routers/exchanges.py",
        reaches="portfolio.services.exchanges",
        service_reaches_provider=True,
    )

    code, output = run_exchange_contract(tmp_path, IMPORT_LINTER_CONFIG)

    assert code != 0, f"the contract accepted router -> service -> exchange provider:\n{output}"
    assert "BROKEN" in output, output
    assert "portfolio.api.routers.exchanges" in output, output
    assert "portfolio.services.exchanges" in output, output
    assert "portfolio.providers.exchanges.credentials" in output, output


@pytest.mark.parametrize(
    "importer",
    ["portfolio/api/dependencies.py", "portfolio/api/schemas/exchanges.py"],
    ids=["dependencies", "a schema"],
)
def test_the_exchange_contract_covers_api_code_that_is_not_a_router(
    tmp_path: Path, importer: str
) -> None:
    """A schema importing a provider's type for an annotation is a request path too."""
    plant_exchange_shadow(
        tmp_path,
        importer=importer,
        reaches="portfolio.providers.exchanges.credentials",
        service_reaches_provider=False,
    )

    code, output = run_exchange_contract(tmp_path, IMPORT_LINTER_CONFIG)

    assert code != 0, f"{importer} reached an exchange provider unreported:\n{output}"
    assert "BROKEN" in output, output


def test_the_composition_root_may_reach_an_exchange_provider(tmp_path: Path) -> None:
    """The control: the same tree with the chain cut, and `main` building the providers.

    `portfolio.main` is where the coordinator's runner is built over the provider mapping,
    and it is outside `portfolio.api`. A contract that refused it would refuse the one
    sanctioned path; a harness broken enough to pass this tree for the wrong reason would
    also pass the violation above, which is why both run the same way.
    """
    plant_exchange_shadow(
        tmp_path,
        importer="portfolio/api/routers/exchanges.py",
        reaches="portfolio.services.exchanges",
        service_reaches_provider=False,
    )
    (tmp_path / "portfolio" / "main.py").write_text(
        import_line("portfolio.providers.exchanges.credentials"), encoding="utf-8"
    )

    code, output = run_exchange_contract(tmp_path, IMPORT_LINTER_CONFIG)

    assert code == 0, f"the composition root or a clean router was reported:\n{output}"
    assert "KEPT" in output, output


def test_allowing_indirect_imports_would_hide_the_exchange_chain(tmp_path: Path) -> None:
    """The discriminator: the planted chain passes a config with the flag added."""
    plant_exchange_shadow(
        tmp_path,
        importer="portfolio/api/routers/exchanges.py",
        reaches="portfolio.services.exchanges",
        service_reaches_provider=True,
    )
    relaxed = relaxed_exchange_configuration(tmp_path / "relaxed.importlinter")

    code, output = run_exchange_contract(tmp_path, relaxed)

    assert code == 0, output
    assert "KEPT" in output, output
