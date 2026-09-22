"""Criteria 1 and 7: what the protocol requires, and that the check on it can fail.

Two halves, and the second is the one that matters. `tests/providers/fakes.py` carries
`_CONFORMS: ChainProvider = FakeChainProvider()` and `mypy --strict` in the gate decides
whether that assignment type checks. A guard nobody has ever seen fail is a guard nobody
knows the state of, so `test_mypy_rejects_a_provider_with_the_wrong_signature` plants a
deliberately broken provider and proves the same check rejects it -- alongside a correct
one, in the same run, so a failure for a reason unrelated to the protocol is visible
instead of being mistaken for success.
"""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from portfolio.domain.addresses import AddressInvalidError
from portfolio.domain.chains import ChainKey, validate_address
from portfolio.providers.base import ChainProvider
from tests.address_vectors import BIP173_TESTNET_P2WPKH, NAMED_CORRUPTIONS
from tests.providers.fakes import FakeChainProvider

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
BACKEND_ROOT: Final = REPO_ROOT / "backend"
SOURCE_ROOT: Final = BACKEND_ROOT / "src"
PROVIDERS_DIR: Final = SOURCE_ROOT / "portfolio" / "providers"

#: Pinned as a literal, not derived from the protocol. A set derived from the thing it
#: describes shrinks along with it: dropping `health` from the protocol would silently
#: drop it from the expectation too, and the criterion-1 assertion would keep passing
#: while the interface it names lost a member.
EXPECTED_PROTOCOL_MEMBERS: Final[frozenset[str]] = frozenset(
    {"capabilities", "validate_address", "fetch_balances", "health"}
)

#: Modules that would make an address check cost a network round trip. `validate_address`
#: being offline is the property that lets a caller tell a mistyped address from an
#: unreachable API without one, and it is structural rather than a matter of discipline.
BLOCKING_IMPORTS: Final[frozenset[str]] = frozenset(
    {"httpx", "httpcore", "requests", "socket", "urllib", "http", "asyncio", "anyio"}
)


def imported_roots(path: Path) -> set[str]:
    """The top-level name of every module a file imports, relative imports included.

    A relative import is reported `.`-prefixed rather than skipped, for the reason
    `tests/domain/test_chains.py` gives: filtering on `node.level == 0` makes a whole
    syntactic form invisible to the scan, and a guard that cannot see a form is not a
    guard.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add("." * node.level + (node.module or "").split(".")[0])
    return roots


# --------------------------------------------------------------------------------------
# Criterion 1: the members, and the shape of each one
# --------------------------------------------------------------------------------------


def test_the_protocol_members_are_the_pinned_set() -> None:
    """Criterion 1's interface, pinned against a literal.

    `__protocol_attrs__` is what `typing` itself computes for a `Protocol`, so this reads
    the same set a structural check would, rather than a hand-maintained list of names
    that happens to sit next to the class.
    """
    # `getattr` rather than attribute access: `__protocol_attrs__` is real at runtime --
    # `typing` computes it for every `Protocol` -- but typeshed does not declare it, so
    # the gate's `mypy --strict` over `tests` would reject the direct spelling.
    attrs: object = getattr(ChainProvider, "__protocol_attrs__", None)
    assert isinstance(attrs, set), "ChainProvider is not a Protocol, or typing changed"
    members = frozenset(str(name) for name in attrs)

    assert members == EXPECTED_PROTOCOL_MEMBERS
    assert getattr(ChainProvider, "_is_protocol", False) is True


def test_the_two_members_that_talk_to_a_chain_are_coroutines_and_the_others_are_not() -> None:
    """`validate_address` is synchronous, and that is criterion 1's "pure, offline".

    If it were `async def`, every caller would have to await it and nobody reading a call
    site could tell whether an address check costs a round trip. The distinction is the
    interface, so it is asserted rather than assumed.
    """
    assert inspect.iscoroutinefunction(ChainProvider.fetch_balances)
    assert inspect.iscoroutinefunction(ChainProvider.health)
    assert not inspect.iscoroutinefunction(ChainProvider.validate_address)
    assert isinstance(ChainProvider.capabilities, property)


def test_the_protocol_is_not_runtime_checkable() -> None:
    """`isinstance` against a protocol compares attribute names and nothing else.

    A class whose `fetch_balances` takes the wrong arguments, or is a plain `def` where
    the protocol says `async def`, passes `isinstance` against a `@runtime_checkable`
    protocol. Making the protocol runtime-checkable would therefore offer a check that
    looks like verification and is not, and somebody would reach for it instead of the
    static one. The decorator's absence is load-bearing, so its absence is a test.
    """
    assert getattr(ChainProvider, "_is_runtime_protocol", False) is False

    with pytest.raises(TypeError, match=r"(?i)runtime"):
        isinstance(FakeChainProvider(), ChainProvider)  # type: ignore[misc]


def test_validate_address_delegates_to_the_domain_registry() -> None:
    """The same answer as the domain, for the valid case and for the refusal.

    A provider that carried its own copy of the codec could accept an address the
    registry's `CHECK` constraint rejects, or canonicalise it differently and so store the
    same wallet twice. Delegation is the only way there is one answer.
    """
    provider = FakeChainProvider()

    assert provider.validate_address(BIP173_TESTNET_P2WPKH) == validate_address(
        ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH
    )

    _, _, corrupted = NAMED_CORRUPTIONS[0]
    with pytest.raises(AddressInvalidError):
        provider.validate_address(corrupted)


def test_the_provider_seam_imports_nothing_that_could_make_validation_block() -> None:
    """`providers/base.py` holds the protocol and the alignment rule, and neither does I/O.

    `import-linter` enforces which layers may import which; nothing enforces that the
    module defining a *synchronous* interface member cannot reach for a socket. An `httpx`
    import here would not break a single layering contract and would still turn an offline
    guarantee into a matter of discipline.
    """
    base = PROVIDERS_DIR / "base.py"
    assert base.is_file(), f"{base} does not exist; the module was renamed or never landed"

    offending = imported_roots(base) & BLOCKING_IMPORTS

    assert offending == set(), f"providers/base.py imports {sorted(offending)}"


def test_the_import_scan_can_actually_fail(tmp_path: Path) -> None:
    """The control. A scan that found no imports would pass the test above vacuously."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import httpx\nfrom . import sibling\nfrom portfolio.domain import money\n",
        encoding="utf-8",
    )

    roots = imported_roots(planted)

    assert roots == {"httpx", ".", "portfolio"}
    assert roots & BLOCKING_IMPORTS == {"httpx"}
    # And the real module is genuinely being read, not an empty file.
    assert "portfolio" in imported_roots(PROVIDERS_DIR / "base.py")


# --------------------------------------------------------------------------------------
# Criterion 7: the static check, proven able to fail
# --------------------------------------------------------------------------------------


CONFORMING_PROVIDER: Final = '''\
"""A provider that satisfies the protocol. The control for the broken one beside it."""

from __future__ import annotations

from collections.abc import Sequence

from portfolio.domain.chains import ChainKey, ValidatedAddress, validate_address
from portfolio.providers.base import (
    AddressBalance,
    ChainCapabilities,
    ChainProvider,
    ProviderHealth,
)


class ConformingProvider:
    @property
    def capabilities(self) -> ChainCapabilities:
        return ChainCapabilities(
            chain_key=ChainKey.BITCOIN, decimals=8, max_addresses_per_call=1
        )

    def validate_address(self, raw: str) -> ValidatedAddress:
        return validate_address(ChainKey.BITCOIN, raw)

    async def fetch_balances(self, addresses: Sequence[str]) -> Sequence[AddressBalance]:
        return [
            AddressBalance(address=address, confirmed=0, decimals=8) for address in addresses
        ]

    async def health(self) -> ProviderHealth:
        return ProviderHealth(chain_key=ChainKey.BITCOIN, healthy=True, detail=None)


_CONFORMS: ChainProvider = ConformingProvider()
'''

BROKEN_PROVIDER: Final = '''\
"""A provider whose `fetch_balances` takes one address instead of a sequence.

This is the exact mistake `@runtime_checkable` + `isinstance` cannot see: the attribute is
present, it is a coroutine function, and it is spelled correctly. Only a signature check
rejects it.
"""

from __future__ import annotations

from portfolio.domain.chains import ChainKey, ValidatedAddress, validate_address
from portfolio.providers.base import (
    AddressBalance,
    ChainCapabilities,
    ChainProvider,
    ProviderHealth,
)


class BrokenProvider:
    @property
    def capabilities(self) -> ChainCapabilities:
        return ChainCapabilities(
            chain_key=ChainKey.BITCOIN, decimals=8, max_addresses_per_call=1
        )

    def validate_address(self, raw: str) -> ValidatedAddress:
        return validate_address(ChainKey.BITCOIN, raw)

    async def fetch_balances(self, address: str) -> AddressBalance:
        return AddressBalance(address=address, confirmed=0, decimals=8)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(chain_key=ChainKey.BITCOIN, healthy=True, detail=None)


_BROKEN: ChainProvider = BrokenProvider()
'''


def run_mypy(paths: Sequence[Path], *, cache_dir: Path) -> subprocess.CompletedProcess[str]:
    """`mypy --strict --no-incremental` over exactly these files.

    Three details, each of which has its own way of producing a meaningless verdict:

    * **`--no-incremental` and a throwaway `--cache-dir`.** A warm cache can report a file
      it did not re-analyse, so a planted file would "pass" having never been checked.
    * **`MYPYPATH` at `src`.** Without it the planted file's `portfolio.providers.base`
      import is unresolved, mypy reports *that* instead, and a test asserting a non-zero
      exit would go green for a reason with nothing to do with the protocol. The control
      file is what actually catches this: if the import were broken, it would fail too.
    * **`sys.executable -m mypy` rather than `uv run mypy`.** The same mypy, out of the
      same environment the gate uses, without depending on `uv` being on `PATH` inside the
      pytest process -- and without a second process between the assertion and the answer.
    """
    command = [
        sys.executable,
        "-m",
        "mypy",
        "--strict",
        "--no-incremental",
        "--no-error-summary",
        f"--cache-dir={cache_dir}",
        *(str(path) for path in paths),
    ]
    environment = dict(os.environ, MYPYPATH=str(SOURCE_ROOT))
    return subprocess.run(  # noqa: S603 - a fixed argument list, no shell, no user input
        command,
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def test_mypy_rejects_a_provider_with_the_wrong_signature(tmp_path: Path) -> None:
    """Criterion 7's guard, shown failing -- and shown passing on a correct provider.

    Both files go through one invocation on purpose. A test that only ran the broken file
    would report success for any mypy failure at all: a typo in the planted source, an
    unresolved import, a missing plugin. Checking a conforming provider in the same run
    turns those into a visible failure of the control instead of a false green.
    """
    conforming = tmp_path / "conforming_provider.py"
    broken = tmp_path / "broken_provider.py"
    conforming.write_text(CONFORMING_PROVIDER, encoding="utf-8")
    broken.write_text(BROKEN_PROVIDER, encoding="utf-8")

    result = run_mypy([conforming, broken], cache_dir=tmp_path / "mypy-cache")
    output = result.stdout + result.stderr

    assert result.returncode != 0, f"mypy accepted a provider with the wrong signature:\n{output}"
    # The control: nothing the run complained about is in the conforming file, so the
    # non-zero exit above is about the protocol and not about the harness.
    offending = [line for line in output.splitlines() if conforming.name in line]
    assert offending == [], f"the control file failed to type check:\n{chr(10).join(offending)}"
    # And the complaint is about the member that was broken, named in the output.
    assert broken.name in output, output
    assert "fetch_balances" in output, output


def test_the_fake_that_ships_with_the_suite_is_the_one_mypy_checks() -> None:
    """`fakes.py` really does carry the assignment the gate's mypy run decides on.

    Without this, someone deleting `_CONFORMS` would remove criterion 7's entire static
    check and no test would notice: the file would still import, the suite would still
    pass, and `mypy --strict` would have nothing left to decide.
    """
    source = (Path(__file__).parent / "fakes.py").read_text(encoding="utf-8")

    assert "_CONFORMS: ChainProvider = FakeChainProvider()" in source
