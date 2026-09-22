"""Criterion 6: every module in `providers/chains/` is wired in -- and the scan is not vacuous.

**This is the criterion that can pass by being empty.** `providers/chains/` ships with no
provider in it, so "every module in the package is registered" is satisfied by a directory
containing nothing, forever, without a single assertion ever looking at a real module. That
is the exact failure #5 catalogued: a verifier whose subject is missing reports success.

So the scan is built as a pure function over a directory path, and it is driven three ways:

1. against a `tmp_path` holding a module that is **not** wired in, asserting it is reported;
2. against a `tmp_path` holding one that **is**, asserting it is not;
3. against the real `providers/chains/` directory.

The third is paired with `EXPECTED_PROVIDER_MODULES`, a pinned literal that is
`frozenset()` today. The empty state is therefore *asserted* rather than assumed, and #7 has
to come here and change it -- which is the moment the assertion stops being vacuous and
starts being the thing that catches a file added without its import line.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from portfolio.providers.registry import CHAIN_PROVIDERS

if TYPE_CHECKING:
    from collections.abc import Collection

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
CHAINS_DIR: Final = REPO_ROOT / "backend" / "src" / "portfolio" / "providers" / "chains"

#: Every provider module `providers/chains/` is expected to hold, as a literal.
#:
#: `frozenset()` today, and that is the assertion rather than an oversight: #7 adds
#: `bitcoin` here and #8 adds `kaspa`. Deriving this from the directory listing would make
#: it agree with whatever it found, which is the defect `PURE_PACKAGES` had in
#: `tests/security/test_no_float.py` -- a guard that shrinks along with its subject cannot
#: fail.
EXPECTED_PROVIDER_MODULES: Final[frozenset[str]] = frozenset()

#: The chain keys expected to have a provider registered, as a literal, for the same
#: reason. A module that lands and imports correctly but never calls the decorator is
#: caught by this and by nothing else.
EXPECTED_REGISTERED_KEYS: Final[tuple[str, ...]] = ()


def chain_modules(package_dir: Path) -> set[str]:
    """Every provider module in the package, under the name an import would use.

    A `.py` file, or a directory with an `__init__.py` -- a provider large enough to be a
    subpackage is still a provider, and a scan that only looked at files would stop seeing
    it on the day somebody split one up.

    `__init__.py` itself is excluded because it is the wiring, not a provider, and
    `__pycache__` because it is not source. Nothing else is excluded: a helper module
    parked in this package would be reported, which is the intended answer. Shared code
    belongs beside the package, not inside the directory whose contract is "everything
    here is a registered provider".
    """
    if not package_dir.is_dir():
        return set()
    found: set[str] = set()
    for entry in sorted(package_dir.iterdir()):
        if entry.name == "__pycache__":
            continue
        if entry.is_file() and entry.suffix == ".py" and entry.stem != "__init__":
            found.add(entry.stem)
        elif entry.is_dir() and (entry / "__init__.py").is_file():
            found.add(entry.name)
    return found


def imported_submodules(package_init: Path) -> set[str]:
    """The submodule names `chains/__init__.py` imports, however the import is spelled.

    All four spellings, because a wiring line that the scan cannot see is a wiring line
    the scan will one day report as missing:

    * `from portfolio.providers.chains import bitcoin`
    * `from . import bitcoin`
    * `import portfolio.providers.chains.bitcoin`
    * `from portfolio.providers.chains.bitcoin import EsploraProvider`
    """
    if not package_init.is_file():
        return set()
    tree = ast.parse(package_init.read_text(encoding="utf-8"), filename=str(package_init))
    package = "portfolio.providers.chains"
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(f"{package}."):
                    found.add(alias.name[len(package) + 1 :].split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == package or (node.level > 0 and not module):
                found.update(alias.name for alias in node.names)
            elif module.startswith(f"{package}."):
                found.add(module[len(package) + 1 :].split(".")[0])
            elif node.level > 0 and module:
                found.add(module.split(".")[0])
    return found


def unregistered_chain_modules(package_dir: Path, registered: Collection[str]) -> set[str]:
    """Every module in the package that nothing in `registered` accounts for.

    Pure, and it takes both sides as arguments, so the two planted-directory tests below
    can drive it without touching the real package and without importing anything. A scan
    that read the real directory from inside itself could only ever be run against the one
    state it happens to be in -- which today is "empty", and would prove nothing.
    """
    return chain_modules(package_dir) - set(registered)


# --------------------------------------------------------------------------------------
# The scan, driven against directories that are deliberately wrong
# --------------------------------------------------------------------------------------


def test_a_module_that_is_not_registered_is_reported(tmp_path: Path) -> None:
    """The failure the criterion exists to produce: a provider file with no import line.

    Adding `bitcoin.py` without the matching line in `chains/__init__.py` means its
    decorator never runs, so the chain is simply absent -- and the symptom is a balance
    that reads zero or a 404 three layers up, noticed by whoever is looking at the
    portfolio rather than by CI.
    """
    package = tmp_path / "chains"
    package.mkdir()
    (package / "__init__.py").write_text('"""No imports here."""\n', encoding="utf-8")
    (package / "bitcoin.py").write_text("PROVIDER = object()\n", encoding="utf-8")

    reported = unregistered_chain_modules(package, imported_submodules(package / "__init__.py"))

    assert reported == {"bitcoin"}


def test_a_module_that_is_registered_is_not_reported(tmp_path: Path) -> None:
    """The control. A scan that reported everything would pass the test above too."""
    package = tmp_path / "chains"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from portfolio.providers.chains import bitcoin  # noqa: F401\n", encoding="utf-8"
    )
    (package / "bitcoin.py").write_text("PROVIDER = object()\n", encoding="utf-8")

    reported = unregistered_chain_modules(package, imported_submodules(package / "__init__.py"))

    assert reported == set()


@pytest.mark.parametrize(
    "wiring",
    [
        pytest.param(
            "from portfolio.providers.chains import bitcoin  # noqa: F401", id="absolute from"
        ),
        pytest.param("from . import bitcoin  # noqa: F401", id="relative from"),
        pytest.param("import portfolio.providers.chains.bitcoin  # noqa: F401", id="dotted import"),
        pytest.param(
            "from portfolio.providers.chains.bitcoin import Esplora  # noqa: F401",
            id="a name out of the module",
        ),
        pytest.param("from .bitcoin import Esplora  # noqa: F401", id="relative, a name"),
    ],
)
def test_every_spelling_of_the_wiring_line_counts_as_wiring(tmp_path: Path, wiring: str) -> None:
    """A wiring line the scan cannot see is a false report waiting to happen.

    This is the hole `tests/domain/test_chains.py` found in its own import scan: filtering
    on `node.level == 0` made relative imports invisible. A guard blind to a whole
    syntactic form is not a guard, and here it would fail the build over a file that is
    correctly wired -- which is the failure mode that gets a guard deleted.
    """
    package = tmp_path / "chains"
    package.mkdir()
    (package / "__init__.py").write_text(f"{wiring}\n", encoding="utf-8")
    (package / "bitcoin.py").write_text("PROVIDER = object()\n", encoding="utf-8")

    wired = imported_submodules(package / "__init__.py")

    assert wired == {"bitcoin"}
    assert unregistered_chain_modules(package, wired) == set()


def test_a_provider_that_is_a_subpackage_is_still_scanned(tmp_path: Path) -> None:
    """A provider split across several files is one provider and still needs its line."""
    package = tmp_path / "chains"
    (package / "kaspa").mkdir(parents=True)
    (package / "__init__.py").write_text('"""Nothing wired."""\n', encoding="utf-8")
    (package / "kaspa" / "__init__.py").write_text("PROVIDER = object()\n", encoding="utf-8")
    (package / "kaspa" / "parsing.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert unregistered_chain_modules(package, ()) == {"kaspa"}


def test_the_package_init_and_the_bytecode_cache_are_not_providers(tmp_path: Path) -> None:
    """`__init__.py` is the wiring and `__pycache__` is not source.

    Reporting either would make the scan fail on a correctly wired package, every time,
    which is how a build gets a blanket ignore added to it.
    """
    package = tmp_path / "chains"
    (package / "__pycache__").mkdir(parents=True)
    (package / "__init__.py").write_text('"""Empty."""\n', encoding="utf-8")
    (package / "__pycache__" / "bitcoin.cpython-312.pyc").write_bytes(b"\x00")

    assert chain_modules(package) == set()


def test_a_directory_that_is_not_a_package_is_not_a_provider(tmp_path: Path) -> None:
    """A folder of fixtures or data is not something that can be imported and registered."""
    package = tmp_path / "chains"
    (package / "fixtures").mkdir(parents=True)
    (package / "__init__.py").write_text('"""Empty."""\n', encoding="utf-8")
    (package / "fixtures" / "response.json").write_text("{}\n", encoding="utf-8")

    assert chain_modules(package) == set()


# --------------------------------------------------------------------------------------
# The real directory, and the literal that stops the assertion being vacuous
# --------------------------------------------------------------------------------------


def test_the_expected_provider_modules_match_the_pinned_literal() -> None:
    """The scan is not vacuous: what is in the package is asserted against a literal.

    Today both sides are empty, and stating that is the whole job. The moment #7 adds
    `bitcoin.py`, this fails until somebody adds `"bitcoin"` here -- so the empty state
    cannot drift into a populated one without a person looking at this file.
    """
    assert CHAINS_DIR.is_dir(), f"{CHAINS_DIR} does not exist"
    assert chain_modules(CHAINS_DIR) == EXPECTED_PROVIDER_MODULES
    assert not EXPECTED_PROVIDER_MODULES, (
        "a provider module landed: add its name above, and add its chain key to "
        "EXPECTED_REGISTERED_KEYS, so the registry assertion stays honest too"
    )


def test_every_module_in_the_chains_package_is_registered() -> None:
    """Criterion 6 over the real tree: no file here lacks its import line.

    Vacuous on its own today -- an empty directory has no unregistered module -- which is
    why it is stated alongside the pinned literal above rather than on its own, and why
    the registry count below is a third, independent way of saying the same thing.
    """
    wired = imported_submodules(CHAINS_DIR / "__init__.py")

    unregistered = unregistered_chain_modules(CHAINS_DIR, wired)

    assert unregistered == set(), (
        f"these modules in providers/chains/ are not imported in its __init__.py, so "
        f"their @register_chain_provider decorator never runs: {sorted(unregistered)}"
    )


def test_importing_the_package_registers_exactly_the_expected_chains() -> None:
    """The third leg: a module can be imported and still never call the decorator.

    The scan above compares files against import lines and the literal above pins the
    files. Neither can see a module that is wired in correctly and simply forgot its
    `@register_chain_provider` -- only the registry can, and only after the package has
    actually been imported.
    """
    import portfolio.providers.chains  # noqa: F401 - imported for its registration effect

    assert CHAIN_PROVIDERS.registered_keys() == EXPECTED_REGISTERED_KEYS
    assert len(CHAIN_PROVIDERS.registered_keys()) == len(EXPECTED_PROVIDER_MODULES)


def test_the_chains_package_documents_why_the_imports_are_explicit() -> None:
    """The reasoning lives next to the thing it explains, or the next person undoes it.

    `pkgutil.walk_packages` is the obvious improvement and it is the wrong one: it turns a
    provider that fails to import into a chain that is silently absent. A docstring saying
    so is what stops that being rediscovered the expensive way.
    """
    document = (CHAINS_DIR / "__init__.py").read_text(encoding="utf-8")

    assert "pkgutil" in document
    assert "register_chain_provider" in document
