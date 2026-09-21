"""The chain registry: the narrow interface, and the purity that justifies its address.

`validate_address` lives in `domain` rather than in `providers` on purpose. A provider is
an I/O boundary, and a pure function parked behind one makes "validation never costs a
network round-trip" a matter of discipline instead of a matter of layering. The last test
here is what turns that sentence into something a build can fail on.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

from portfolio.domain.addresses import AddressInvalidError, AddressRejection
from portfolio.domain.chains import CHAIN_VALIDATORS, ChainKey, validate_address
from tests.address_vectors import BIP173_TESTNET_P2WPKH, CORE_SIGNET_P2PKH

#: Pinned against a literal. Adding a chain is a deliberate act that also has to touch the
#: `CHECK` constraint, the migration and the frontend, so it should not be possible to do
#: it in one file and have every test stay green.
EXPECTED_CHAIN_KEYS: Final = {"bitcoin", "kaspa"}

DOMAIN_DIR: Final = Path(__file__).resolve().parents[3] / "backend" / "src" / "portfolio" / "domain"

#: What a pure validator is allowed to reach for. `hashlib` is here because Base58Check is
#: defined as a double SHA-256 and there is no way to compute one without it; everything
#: on this list is arithmetic over values the caller already handed in.
PURE_IMPORTS_ALLOWED: Final = frozenset(
    {
        "__future__",
        "collections",
        "collections.abc",
        "dataclasses",
        "enum",
        "functools",
        "hashlib",
        "itertools",
        "operator",
        "re",
        "string",
        "types",
        "typing",
        "portfolio.domain.addresses",
        "portfolio.domain.chains",
    }
)

#: Reaching for any of these from `domain` means validation can block, can depend on the
#: wall clock, or can differ between two runs -- and none of those may be true of a rule
#: the user is told about the instant they paste an address.
IMPURE_IMPORTS: Final = frozenset(
    {
        "asyncio",
        "datetime",
        "httpx",
        "os",
        "pathlib",
        "random",
        "requests",
        "secrets",
        "socket",
        "sqlalchemy",
        "subprocess",
        "time",
        "urllib",
    }
)


def imported_modules(path: Path) -> set[str]:
    """Every top-level module name a file imports, however it spells the import."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            found.add(node.module)
    return found


def test_the_registry_has_a_validator_for_every_chain_key() -> None:
    """A key with no validator is a `KeyError` at runtime on a path the user can reach."""
    assert set(CHAIN_VALIDATORS) == set(ChainKey)
    assert {key.value for key in ChainKey} == EXPECTED_CHAIN_KEYS


def test_chain_key_values_are_the_strings_the_api_speaks() -> None:
    """The enum is what a request body carries, so its values are part of the contract."""
    assert ChainKey.BITCOIN.value == "bitcoin"
    assert ChainKey.KASPA.value == "kaspa"
    assert str(ChainKey.BITCOIN) == "bitcoin"


@pytest.mark.parametrize(
    "chain_key",
    ["", "BITCOIN", "bitcoin ", "ethereum", "btc", "kaspatest", "bitcoin;drop"],
    ids=["empty", "upper", "trailing space", "unsupported", "ticker", "prefix", "injection"],
)
def test_an_unknown_chain_key_is_refused_by_the_registry(chain_key: str) -> None:
    """The caller passes a plain string, so the registry is where an unknown one stops.

    Case matters and whitespace matters: the value goes into a column with a `CHECK`
    constraint on exactly these two strings, and a key that got past here but failed the
    constraint would be a 500 where a 422 belongs.
    """
    with pytest.raises(AddressInvalidError) as caught:
        validate_address(chain_key, BIP173_TESTNET_P2WPKH)

    assert caught.value.reason is AddressRejection.UNKNOWN_CHAIN


def test_the_unknown_chain_refusal_does_not_quote_the_address() -> None:
    """The address is still user data even when it was the chain key that was wrong."""
    with pytest.raises(AddressInvalidError) as caught:
        validate_address("ethereum", BIP173_TESTNET_P2WPKH)

    assert BIP173_TESTNET_P2WPKH not in caught.value.message
    assert BIP173_TESTNET_P2WPKH not in str(caught.value)


@pytest.mark.parametrize(
    "address",
    [BIP173_TESTNET_P2WPKH, CORE_SIGNET_P2PKH],
    ids=["bech32", "base58"],
)
def test_a_bitcoin_address_is_not_a_kaspa_address(address: str) -> None:
    """The registry has to dispatch, not merely look up.

    A validator chain that tried every codec until one succeeded would accept a Bitcoin
    address under `chain_key="kaspa"`, store it, and then hand it to a Kaspa provider that
    can only answer "no such address" -- which is indistinguishable from an empty wallet.
    """
    # Accepted under Bitcoin, display form untouched. The canonical form is not asserted
    # here: it is lower-cased for bech32 and left alone for Base58Check, and
    # `tests/domain/test_addresses.py` is where that asymmetry is pinned down.
    assert validate_address(ChainKey.BITCOIN, address).display == address

    with pytest.raises(AddressInvalidError):
        validate_address(ChainKey.KASPA, address)


def test_the_validator_accepts_the_enum_and_the_bare_string_alike() -> None:
    """Services hold a `ChainKey`; a request body carries a `str`. Both must work."""
    from_enum = validate_address(ChainKey.BITCOIN, BIP173_TESTNET_P2WPKH)
    from_string = validate_address("bitcoin", BIP173_TESTNET_P2WPKH)

    assert from_enum == from_string


@pytest.mark.parametrize("module", ["addresses.py", "chains.py"])
def test_the_address_modules_import_nothing_that_could_block_or_drift(module: str) -> None:
    """The property that puts these modules in `domain` instead of in `providers`.

    `import-linter` enforces the direction of the dependency; nothing enforces that the
    things `domain` is allowed to import are themselves pure. An `httpx` call here would
    make an address check cost a round-trip, and a `datetime.now()` would make the same
    input answer differently on two runs -- and both would still satisfy every layering
    contract in the repository, because neither is an upward import.
    """
    path = DOMAIN_DIR / module
    assert path.is_file(), f"{path} does not exist; the module was renamed or never landed"

    imported = imported_modules(path)
    roots = {name.split(".")[0] for name in imported}

    assert roots & IMPURE_IMPORTS == set()
    assert imported <= PURE_IMPORTS_ALLOWED, imported - PURE_IMPORTS_ALLOWED


def test_the_purity_scan_can_actually_fail() -> None:
    """A scan that found no imports would pass the test above without checking anything."""
    assert imported_modules(Path(__file__)) & {"ast", "pathlib"} == {"ast", "pathlib"}
    assert IMPURE_IMPORTS.isdisjoint(PURE_IMPORTS_ALLOWED)
