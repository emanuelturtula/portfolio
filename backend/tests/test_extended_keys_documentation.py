"""Spec 031, criterion 9, and the documentation the spec's Docs section asks for.

A document that states a number the code does not use is worse than no document: the owner
plans a first sync around it, and a provider author copies it. So each figure here is read
from the code it describes, not restated: the gap limit, the cap, the host floor, the
version bytes, the prefixes the redaction covers, the log event's name, and the downgrade's
refusal, whose sentence sends the owner to a section by name.

`tests/providers/test_documentation.py` already keeps every mainnet prefix out of
`docs/providers.md` (spec 031: "the guard stays as strict as it is"); this module does not
repeat that check, it relies on it.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Final

import pytest

from portfolio.db.migrations.versions import v0011_extended_keys
from portfolio.domain import extended_keys
from portfolio.domain.addresses import REJECTION_MESSAGES, AddressRejection
from portfolio.domain.extended_keys import (
    EXTENDED_PUBLIC_KEY_VERSIONS,
    GAP_LIMIT,
    MAX_ADDRESSES_PER_BRANCH,
    MULTISIG_PUBLIC_PREFIXES,
    PRIVATE_KEY_RUN_PATTERN,
    NetworkFamily,
    ScriptType,
)
from portfolio.logging import EXTENDED_PRIVATE_KEY_PREFIXES, EXTENDED_PUBLIC_KEY_PREFIXES
from portfolio.providers import base
from portfolio.providers.chains import bitcoin
from portfolio.providers.http import DEFAULT_MIN_HOST_INTERVAL_MS
from portfolio.services import balance_sync

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
PROVIDERS: Final = REPO_ROOT / "docs" / "providers.md"
OPERATIONS: Final = REPO_ROOT / "docs" / "operations.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def section(text: str, heading: str) -> str:
    """From `heading` to the next heading of the same or a higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(heading + "\n")
    following = re.compile(rf"^#{{1,{level}}} ", re.MULTILINE)
    end = following.search(text, start + len(heading) + 1)
    return text[start : end.start() if end else len(text)]


def flat(text: str) -> str:
    """Line breaks folded to spaces, so a phrase that wraps is still one phrase."""
    return re.sub(r"\s+", " ", text)


@pytest.fixture(scope="module")
def providers_section() -> str:
    return section(read(PROVIDERS), "## Extended public keys")


@pytest.fixture(scope="module")
def operations_section() -> str:
    return section(
        read(OPERATIONS), "### Adding an extended public key instead of single addresses"
    )


# --------------------------------------------------------------------------------------
# docs/providers.md: criterion 9
# --------------------------------------------------------------------------------------


def test_the_providers_document_says_no_vendor_can_answer_for_a_key_and_why(
    providers_section: str,
) -> None:
    """Criterion 9: public-API extended-key lookup is unavailable, and why, with a date."""
    text = flat(providers_section)

    assert "### Why the vendors cannot answer it: confirmed on 2026-10-03" in providers_section
    assert "Blockstream" in text
    assert "mempool.space" in text
    assert "Neither documents an endpoint that takes an extended key or a descriptor." in text
    assert "So a key is derived locally" in text


def test_the_providers_table_gives_each_test_prefix_with_its_mainnet_version_bytes(
    providers_section: str,
) -> None:
    """The table is checked against the parser's own R2 table, row by row."""
    by_script = {
        (version.network_family, version.script_type): (number, version.prefix)
        for number, version in EXTENDED_PUBLIC_KEY_VERSIONS.items()
    }
    names = {
        ScriptType.P2PKH: "P2PKH (BIP44)",
        ScriptType.P2SH_P2WPKH: "P2SH-P2WPKH (BIP49)",
        ScriptType.P2WPKH: "P2WPKH (BIP84)",
    }
    for script, name in names.items():
        mainnet, _ = by_script[(NetworkFamily.MAIN, script)]
        _, test_prefix = by_script[(NetworkFamily.TEST, script)]
        row = f"| {name} | `{test_prefix}` | `0x{mainnet:08X}` | `/0/i` and `/1/i` |"
        assert row in providers_section, row


def test_the_providers_document_states_the_scan_s_numbers_as_the_code_has_them(
    providers_section: str,
) -> None:
    text = flat(providers_section)

    assert GAP_LIMIT == 20
    assert f"**Gap limit {GAP_LIMIT} per branch**" in text
    assert MAX_ADDRESSES_PER_BRANCH == 1000
    assert f"**Capped at {MAX_ADDRESSES_PER_BRANCH} addresses per branch**" in text
    assert "`MAX_ADDRESSES_PER_BRANCH`" in text
    assert DEFAULT_MIN_HOST_INTERVAL_MS == 1000
    assert f"at least {2 * GAP_LIMIT} requests -- {GAP_LIMIT} per branch" in text
    assert f"at least {2 * GAP_LIMIT} seconds" in text


def test_the_providers_document_says_used_comes_from_tx_count(providers_section: str) -> None:
    text = flat(providers_section)

    assert bitcoin.TX_COUNT == "tx_count"
    assert "**Used** means `chain_stats.tx_count > 0`, or `mempool_stats.tx_count > 0`" in text
    assert "An address once recorded as used stays used" in text


def test_the_providers_document_carries_the_caveats_the_spec_lists(providers_section: str) -> None:
    text = flat(providers_section)

    assert "**The P2PKH-version caveat.**" in text
    assert "**No multisig and no taproot.**" in text
    for prefix in MULTISIG_PUBLIC_PREFIXES:
        assert f"`{prefix}`" in text
    assert "**Private keys are refused by their prefix**" in text
    assert "`docs/operations.md`, section 8, gives them by name" in text


def test_the_providers_document_names_code_that_exists(providers_section: str) -> None:
    """A provider author follows these names into the code, so each has to be there."""
    assert "`EsploraProvider.scan_extended_key`" in providers_section
    assert callable(bitcoin.EsploraProvider.scan_extended_key)
    assert "`ExtendedKeyScanner`" in providers_section
    assert hasattr(base, "ExtendedKeyScanner")
    for module in ("secp256k1.py", "ripemd160.py", "extended_keys.py"):
        assert f"`{module}`" in providers_section
        assert (REPO_ROOT / "backend" / "src" / "portfolio" / "domain" / module).is_file()


def test_the_providers_document_describes_what_counts_as_a_private_key(
    providers_section: str,
) -> None:
    """R2b, as a provider author reads it: both tests, by the names the code uses."""
    text = flat(providers_section)
    floor = re.search(r"\{(\d+),\}$", PRIVATE_KEY_RUN_PATTERN.pattern)
    assert floor is not None

    assert "**Private keys are refused by their prefix**, wherever it stands" in text
    assert f"named `{AddressRejection.PRIVATE_KEY.value}` (`looks_like_private_key`)" in text
    assert "it starts with one of `PRIVATE_KEY_PREFIXES`" in text
    assert "(`PRIVATE_KEY_RUN_PATTERN`): a private prefix that does not continue" in text
    assert "does not continue a Base58 run" in text
    assert f"followed by {floor.group(1)} or more Base58 characters" in text
    assert "the left boundary keeps the run off a public key's own body" in text
    assert "A key glued directly onto other Base58 text is not such a run." in text
    for name in ("looks_like_private_key", "PRIVATE_KEY_PREFIXES", "PRIVATE_KEY_RUN_PATTERN"):
        assert name in extended_keys.__all__


# --------------------------------------------------------------------------------------
# docs/operations.md: the owner's half
# --------------------------------------------------------------------------------------


def test_the_operations_section_is_in_section_8(operations_section: str) -> None:
    """Both the providers document and the downgrade's refusal send the owner to section 8."""
    eight = section(read(OPERATIONS), "## 8. Where Bitcoin balances are read from")

    assert operations_section in eight
    assert "#### Deleting extended-key wallets before a downgrade" in eight


def test_the_operations_section_names_each_prefix_and_what_it_derives(
    operations_section: str,
) -> None:
    rows = {
        "zpub": "| `zpub` | native segwit, P2WPKH (BIP84) | mainnet |",
        "ypub": "| `ypub` | nested segwit, P2SH-P2WPKH (BIP49) | mainnet |",
        "xpub": "| `xpub` | legacy, P2PKH (BIP44) | mainnet |",
    }
    by_prefix = {version.prefix: version for version in EXTENDED_PUBLIC_KEY_VERSIONS.values()}
    for prefix, row in rows.items():
        assert row in operations_section
        assert by_prefix[prefix].network_family is NetworkFamily.MAIN
    assert "| `vpub` / `upub` / `tpub` | the same three, in that order |" in operations_section
    assert [by_prefix[p].script_type for p in ("vpub", "upub", "tpub")] == [
        by_prefix[p].script_type for p in ("zpub", "ypub", "xpub")
    ]


def test_the_operations_section_states_the_first_scan_and_its_cost(
    operations_section: str,
) -> None:
    text = flat(operations_section)

    assert "**The first sync after adding one takes about a minute.**" in text
    assert f"the standard gap limit of {GAP_LIMIT}" in text
    assert f"at least {2 * GAP_LIMIT} reads" in text
    assert f"more than {MAX_ADDRESSES_PER_BRANCH} addresses" in text


def test_the_operations_section_warns_about_the_overlap(operations_section: str) -> None:
    text = flat(operations_section)

    assert "**Do not also register an address the key derives.**" in text
    assert "is counted twice, and nothing detects the overlap" in text


def test_the_operations_section_says_where_a_private_key_is_refused(
    operations_section: str,
) -> None:
    """R2b, as the owner reads it, with the API's own sentence quoted."""
    text = flat(operations_section)

    assert f'"{REJECTION_MESSAGES[AddressRejection.PRIVATE_KEY]}"' in text
    assert "the value starts with a private prefix, ignoring surrounding spaces and" in text
    assert "a whole private key appears anywhere in it: after other text and a space" in text
    assert "The form applies the same test as you enter the value, clears the field" in text
    assert "and never sends it" in text
    assert "it is still never stored" in text


def test_the_logged_event_the_owner_is_told_about_is_the_one_the_sync_logs(
    operations_section: str,
) -> None:
    assert "`balance_sync_extended_key_scanned`" in operations_section
    assert '"balance_sync_extended_key_scanned"' in inspect.getsource(balance_sync)


def test_the_downgrade_refusal_quoted_to_the_owner_is_the_one_the_migration_raises() -> None:
    eight = flat(section(read(OPERATIONS), "## 8. Where Bitcoin balances are read from"))
    refusal = v0011_extended_keys.DOWNGRADE_REFUSED

    head, _count, tail = refusal.partition("{count}")
    first_sentence = head + "..." + tail.split(". ")[0] + "."
    assert first_sentence.endswith(
        "wallet(s), archived ones included, hold an extended public key."
    )
    assert f"`{first_sentence}`" in eight
    assert "section 8, 'Deleting extended-key wallets before a downgrade'" in refusal


def test_the_redaction_prefixes_listed_for_the_owner_are_the_ones_the_pattern_uses() -> None:
    text = flat(read(OPERATIONS))
    public = ", ".join(f"`{prefix}`" for prefix in EXTENDED_PUBLIC_KEY_PREFIXES[:-1])
    private = ", ".join(f"`{prefix}`" for prefix in EXTENDED_PRIVATE_KEY_PREFIXES[:-1])

    assert (
        f"the public {public} and `{EXTENDED_PUBLIC_KEY_PREFIXES[-1]}`, and the private "
        f"{private} and `{EXTENDED_PRIVATE_KEY_PREFIXES[-1]}`, followed by 100 or more Base58 "
        "characters"
    ) in text
