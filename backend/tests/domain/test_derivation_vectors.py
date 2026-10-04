"""Spec 031, criterion 2: the right script type is derived per prefix, on every network.

Three layers, each against something this repository did not compute with the code under
test:

* **BIP-32's non-hardened public steps**, as `tpub`: a parent key and an index must produce
  exactly the published child's public key and chain code.
* **BIP-49's and BIP-84's published children**: an account key, a branch and an index must
  produce the published public key, and that key the published address -- BIP-49's as it was
  published, BIP-84's as `tb1` over the same witness program.
* **R3's encodings**, on testnet and regtest, from independently derived addresses.

Mainnet is proven on the encoding tables themselves (R11), and by substituting test-network
values into the mainnet row so that `address_of` is seen to read it. No mainnet address is
produced at any point.
"""

from __future__ import annotations

from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from portfolio.domain import extended_keys
from portfolio.domain.addresses import (
    BITCOIN_HRP_BY_NETWORK,
    P2PKH_VERSION_BYTE_BY_NETWORK,
    P2SH_VERSION_BYTE_BY_NETWORK,
    BitcoinNetwork,
    bitcoin_network_of,
    validate_bitcoin_address,
)
from portfolio.domain.extended_keys import (
    CHANGE_BRANCH,
    HARDENED_INDEX,
    NETWORK_FAMILY_BY_NETWORK,
    RECEIVE_BRANCH,
    DerivedKey,
    ExtendedPublicKey,
    NetworkFamily,
    ScriptType,
    address_of,
    derive_child,
    hash160,
    parse_extended_public_key,
)
from portfolio.domain.secp256k1 import CURVE_ORDER, GENERATOR, compress
from tests.extended_key_vectors import (
    BIP32_PUBLIC_STEPS,
    BIP32_TV1_M,
    BIP49_ACCOUNT_UPUB,
    BIP49_CHILDREN,
    BIP49_RECEIVE_0_KEY_HASH,
    BIP49_RECEIVE_0_PUBLIC_KEY,
    BIP84_ACCOUNT_VPUB,
    BIP84_CHILDREN,
    BIP84_ROOT_VPUB,
    ENCODINGS,
    TV1_MASTER_CHILDREN_P2PKH,
    ChildStep,
    DerivedAddress,
    Encodings,
)

ALL_CHILDREN: Final[tuple[tuple[str, DerivedAddress], ...]] = (
    *((BIP84_ACCOUNT_VPUB, child) for child in BIP84_CHILDREN),
    *((BIP49_ACCOUNT_UPUB, child) for child in BIP49_CHILDREN),
    *((BIP32_TV1_M, child) for child in TV1_MASTER_CHILDREN_P2PKH),
)


def derive(key: str, branch: int, index: int) -> DerivedKey:
    parent = parse_extended_public_key(key)
    branch_key = derive_child(parent, branch)
    assert branch_key is not None
    child = derive_child(branch_key, index)
    assert child is not None
    return child


# --------------------------------------------------------------------------------------
# BIP-32: every non-hardened public step
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("step", BIP32_PUBLIC_STEPS, ids=[step.id for step in BIP32_PUBLIC_STEPS])
def test_a_published_public_step_produces_the_published_child(step: ChildStep) -> None:
    parent = parse_extended_public_key(step.parent)
    published = parse_extended_public_key(step.child)

    child = derive_child(parent, step.child_index)

    assert child is not None
    assert child.index == step.child_index
    assert child.public_key == published.public_key
    assert child.chain_code == published.chain_code


def test_the_child_fingerprint_is_the_parents_hash160() -> None:
    """Not something derivation computes, but the vectors carry it: it pins `hash160`."""
    for step in BIP32_PUBLIC_STEPS:
        parent = parse_extended_public_key(step.parent)
        published = parse_extended_public_key(step.child)
        assert hash160(parent.public_key)[:4] == published.parent_fingerprint, step.id


def test_hash160_is_the_published_key_hash() -> None:
    assert hash160(bytes.fromhex(BIP49_RECEIVE_0_PUBLIC_KEY)).hex() == BIP49_RECEIVE_0_KEY_HASH


def test_derivation_continues_from_a_derived_key() -> None:
    """`derive_child` takes a `DerivedKey` as a parent, which is how `/branch/index` works."""
    parent = parse_extended_public_key(BIP84_ACCOUNT_VPUB)
    branch = derive_child(parent, RECEIVE_BRANCH)
    assert isinstance(branch, DerivedKey)
    assert branch.index == RECEIVE_BRANCH
    child = derive_child(branch, 1)
    assert child is not None
    assert child.public_key.hex() == BIP84_CHILDREN[1].public_key


def test_the_branch_constants() -> None:
    assert RECEIVE_BRANCH == 0
    assert CHANGE_BRANCH == 1
    assert HARDENED_INDEX == 2**31


@pytest.mark.parametrize("index", [-1, HARDENED_INDEX, HARDENED_INDEX + 1, 2**32])
def test_a_hardened_or_negative_index_is_refused(index: int) -> None:
    """Hardened derivation needs a private key, which this application never holds."""
    parent = parse_extended_public_key(BIP32_TV1_M)
    with pytest.raises(ValueError):  # noqa: PT011 - the type is the contract
        derive_child(parent, index)


def test_the_highest_non_hardened_index_is_derived() -> None:
    parent = parse_extended_public_key(BIP32_TV1_M)
    child = derive_child(parent, HARDENED_INDEX - 1)
    assert child is not None
    assert child.index == HARDENED_INDEX - 1


# --------------------------------------------------------------------------------------
# BIP-49 and BIP-84: published children, and the script type each prefix fixes
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("account", "child"), ALL_CHILDREN, ids=[child.id for _account, child in ALL_CHILDREN]
)
def test_a_published_child_has_the_published_key_and_address(
    account: str, child: DerivedAddress
) -> None:
    key = parse_extended_public_key(account)
    derived = derive(account, child.branch, child.child_index)

    assert derived.public_key.hex() == child.public_key
    assert address_of(derived.public_key, key.script_type, BitcoinNetwork.TESTNET) == child.address


@pytest.mark.parametrize(
    ("key", "script_type"),
    [
        (BIP32_TV1_M, ScriptType.P2PKH),
        (BIP49_ACCOUNT_UPUB, ScriptType.P2SH_P2WPKH),
        (BIP84_ACCOUNT_VPUB, ScriptType.P2WPKH),
        (BIP84_ROOT_VPUB, ScriptType.P2WPKH),
    ],
    ids=["tpub", "upub", "vpub account", "vpub root"],
)
def test_the_prefix_fixes_the_script_type(key: str, script_type: ScriptType) -> None:
    assert parse_extended_public_key(key).script_type is script_type


# --------------------------------------------------------------------------------------
# R3: every encoding, testnet and regtest from vectors, mainnet from the tables
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("row", ENCODINGS, ids=[row.id for row in ENCODINGS])
def test_address_of_on_testnet_and_regtest(row: Encodings) -> None:
    public_key = bytes.fromhex(row.public_key)
    cases = {
        (ScriptType.P2PKH, BitcoinNetwork.TESTNET): row.testnet_p2pkh,
        (ScriptType.P2SH_P2WPKH, BitcoinNetwork.TESTNET): row.testnet_p2sh_p2wpkh,
        (ScriptType.P2WPKH, BitcoinNetwork.TESTNET): row.testnet_p2wpkh,
        (ScriptType.P2PKH, BitcoinNetwork.REGTEST): row.regtest_p2pkh,
        (ScriptType.P2SH_P2WPKH, BitcoinNetwork.REGTEST): row.regtest_p2sh_p2wpkh,
        (ScriptType.P2WPKH, BitcoinNetwork.REGTEST): row.regtest_p2wpkh,
    }
    for (script_type, network), expected in cases.items():
        assert address_of(public_key, script_type, network) == expected, (script_type, network)


def test_the_encoding_tables_are_r3() -> None:
    """Mainnet support, proven where R11 says it is proven: on the tables, as values."""
    assert dict(BITCOIN_HRP_BY_NETWORK) == {
        BitcoinNetwork.MAINNET: "bc",
        BitcoinNetwork.TESTNET: "tb",
        BitcoinNetwork.REGTEST: "bcrt",
    }
    assert dict(P2PKH_VERSION_BYTE_BY_NETWORK) == {
        BitcoinNetwork.MAINNET: 0x00,
        BitcoinNetwork.TESTNET: 0x6F,
        BitcoinNetwork.REGTEST: 0x6F,
    }
    assert dict(P2SH_VERSION_BYTE_BY_NETWORK) == {
        BitcoinNetwork.MAINNET: 0x05,
        BitcoinNetwork.TESTNET: 0xC4,
        BitcoinNetwork.REGTEST: 0xC4,
    }
    assert dict(NETWORK_FAMILY_BY_NETWORK) == {
        BitcoinNetwork.MAINNET: NetworkFamily.MAIN,
        BitcoinNetwork.TESTNET: NetworkFamily.TEST,
        BitcoinNetwork.REGTEST: NetworkFamily.TEST,
    }


@pytest.mark.parametrize("script_type", list(ScriptType))
def test_the_mainnet_row_is_the_one_address_of_reads(
    script_type: ScriptType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`address_of(..., MAINNET)` reads the mainnet row of the tables, and nothing else.

    The mainnet row is replaced, for this test only, by testnet's values. If `address_of`
    reads the row it is handed, the result is the testnet address; if it hard-coded `bc` or
    `0x00` anywhere, the result would differ. Nothing mainnet is produced either way, because
    the only values reachable are the ones substituted in.
    """
    for table in (
        BITCOIN_HRP_BY_NETWORK,
        P2PKH_VERSION_BYTE_BY_NETWORK,
        P2SH_VERSION_BYTE_BY_NETWORK,
    ):
        monkeypatch.setitem(
            table,
            BitcoinNetwork.MAINNET,
            table[BitcoinNetwork.TESTNET],
        )
    row = ENCODINGS[0]
    public_key = bytes.fromhex(row.public_key)
    expected = {
        ScriptType.P2PKH: row.testnet_p2pkh,
        ScriptType.P2SH_P2WPKH: row.testnet_p2sh_p2wpkh,
        ScriptType.P2WPKH: row.testnet_p2wpkh,
    }[script_type]

    assert address_of(public_key, script_type, BitcoinNetwork.MAINNET) == expected


# --------------------------------------------------------------------------------------
# Every derived address validates and is its own canonical form
# --------------------------------------------------------------------------------------

DERIVATION_KEYS: Final = (BIP32_TV1_M, BIP49_ACCOUNT_UPUB, BIP84_ACCOUNT_VPUB, BIP84_ROOT_VPUB)
TEST_NETWORKS: Final = (BitcoinNetwork.TESTNET, BitcoinNetwork.REGTEST)


@settings(max_examples=60, deadline=None)
@given(
    key=st.sampled_from(DERIVATION_KEYS),
    branch=st.sampled_from((RECEIVE_BRANCH, CHANGE_BRANCH)),
    index=st.integers(min_value=0, max_value=HARDENED_INDEX - 1),
    network=st.sampled_from(TEST_NETWORKS),
)
def test_every_derived_address_validates_and_is_canonical(
    key: str, branch: int, index: int, network: BitcoinNetwork
) -> None:
    """The spec's own requirement: `validate_bitcoin_address` accepts it, unchanged."""
    parent = parse_extended_public_key(key)
    branch_key = derive_child(parent, branch)
    assert branch_key is not None
    child = derive_child(branch_key, index)
    if child is None:  # pragma: no cover - probability below 2^-127
        return

    address = address_of(child.public_key, parent.script_type, network)
    validated = validate_bitcoin_address(address)

    assert validated.canonical == address
    assert validated.display == address
    if parent.script_type is ScriptType.P2WPKH:
        assert bitcoin_network_of(address) is network
    else:
        # Base58 cannot tell regtest from testnet (`bitcoin_network_of`'s docstring).
        assert bitcoin_network_of(address) is BitcoinNetwork.TESTNET


#: Derived by the independent script: BIP-32 test vector 1's master `tpub`, child /0/1000001,
#: as test-network P2PKH. **The property test above found it.** Its last `1` follows only
#: letters and precedes only bech32-alphabet characters, so the bech32 discriminator took it
#: for a segwit address and `validate_bitcoin_address` refused it as `mixed_case` -- a
#: derived address the provider would then have refused to read, failing the whole chain.
SHAPED_LIKE_BECH32_P2PKH: Final = "moKbLCYdi1dEKVpscE9FnNVsGTpV8mk7DM"
SHAPED_LIKE_BECH32_PUBLIC_KEY: Final = (
    "0274f17232f1cca3af98cf618bf2e6ff969136f451769e36e98fd01d80a7ef503f"
)


def test_a_derived_p2pkh_address_shaped_like_bech32_validates() -> None:
    """The regression the property test found, pinned so it cannot need finding again."""
    child = derive(BIP32_TV1_M, RECEIVE_BRANCH, 1_000_001)
    assert child.public_key.hex() == SHAPED_LIKE_BECH32_PUBLIC_KEY

    address = address_of(child.public_key, ScriptType.P2PKH, BitcoinNetwork.TESTNET)

    assert address == SHAPED_LIKE_BECH32_P2PKH
    assert validate_bitcoin_address(address).canonical == address
    assert bitcoin_network_of(address) is BitcoinNetwork.TESTNET


# --------------------------------------------------------------------------------------
# R5's invalid index, by injection through the HMAC seam
# --------------------------------------------------------------------------------------


def generator_parent() -> ExtendedPublicKey:
    """A parent whose public key is G, so that IL = n - 1 lands exactly on infinity."""
    base = parse_extended_public_key(BIP32_TV1_M)
    return ExtendedPublicKey(
        network_family=base.network_family,
        script_type=base.script_type,
        depth=base.depth,
        parent_fingerprint=base.parent_fingerprint,
        child_number=base.child_number,
        chain_code=base.chain_code,
        public_key=compress(GENERATOR),
    )


def inject(monkeypatch: pytest.MonkeyPatch, il: int) -> list[tuple[bytes, bytes]]:
    calls: list[tuple[bytes, bytes]] = []

    def fake(key: bytes, data: bytes) -> bytes:
        calls.append((key, data))
        return il.to_bytes(32, "big") + bytes(range(32))

    monkeypatch.setattr(extended_keys, "hmac_sha512", fake)
    return calls


@pytest.mark.parametrize("il", [CURVE_ORDER, CURVE_ORDER + 1, 2**256 - 1], ids=["n", "n+1", "max"])
def test_il_at_or_above_the_order_has_no_key(il: int, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = inject(monkeypatch, il)
    parent = parse_extended_public_key(BIP32_TV1_M)

    assert derive_child(parent, 7) is None
    assert len(calls) == 1


def test_a_child_at_infinity_has_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = inject(monkeypatch, CURVE_ORDER - 1)

    assert derive_child(generator_parent(), 3) is None
    assert len(calls) == 1


def test_il_just_below_the_order_is_a_key_for_any_other_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the two tests above: the same IL is fine when it does not cancel."""
    inject(monkeypatch, CURVE_ORDER - 1)
    parent = parse_extended_public_key(BIP32_TV1_M)

    child = derive_child(parent, 3)

    assert child is not None
    assert child.chain_code == bytes(range(32))
    assert child.index == 3


def test_the_hmac_input_is_bip32s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Key = chain code, data = serP(K_par) || ser32(i): the CKDpub definition."""
    calls = inject(monkeypatch, 5)
    parent = parse_extended_public_key(BIP32_TV1_M)

    derive_child(parent, 0x01020304)

    assert calls == [(parent.chain_code, parent.public_key + bytes([1, 2, 3, 4]))]


def test_the_real_hmac_is_sha512() -> None:
    import hashlib
    import hmac

    key, data = b"chain-code", b"data"
    assert extended_keys.hmac_sha512(key, data) == hmac.new(key, data, hashlib.sha512).digest()
