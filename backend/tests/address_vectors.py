"""The address vectors the whole suite shares, and where each one came from.

Every string here is **testnet, signet, regtest or synthetic**. Rule 3 forbids a mainnet
address anywhere in the repository, so the published mainnet halves of these vector lists
are deliberately absent even though they are the better known ones;
`tests/security/test_address_logging.py::test_fixtures_contain_no_mainnet_address` scans
this file and every other test file to prove it mechanically rather than by review.

## Provenance, which is the whole point of this module

A codec test is only worth what its vectors are worth. A vector produced by the
implementation under test proves that the code agrees with itself and nothing more, so
every string below is either quoted from a published source or derived from one by an
**independent** implementation.

* `BIP173_*` / `BIP350_*` -- the "Valid/Invalid address" test vector tables of BIP-173 and
  BIP-350, quoted verbatim from `bitcoin/bips`, restricted to the `tb` and `tc` entries.
* `CORE_*` -- Bitcoin Core's `src/test/data/key_io_valid.json`, restricted to the rows
  whose `chain` is `signet`, `testnet4` or `regtest`. Legacy base58 addresses on those
  three chains carry the same version bytes as testnet3: `0x6F` (111) for P2PKH, which
  renders as a leading `m` or `n`, and `0xC4` (196) for P2SH, which renders as a leading
  `2`.
* `DERIVED_*` -- minted for this suite, because the published lists do not contain the
  case. The tool that minted them is an independent bech32/bech32m implementation that
  was first required to reproduce *every* published non-mainnet vector, valid and invalid,
  before it was trusted to emit anything; see the comment on each constant for exactly
  what was derived and from what.

## What a "single-character corruption" is for

`corruptions_of` is the test that separates a checksum from a shape. A validator that
checks the prefix, the length and the alphabet passes every happy-path test in this suite
and still accepts a typo -- which is the exact failure this issue exists to prevent,
because an address accepted with one wrong character is a wallet that reports a zero
balance forever and looks no different from an empty one.

Bech32 guarantees detection of up to four substitutions, so *every* single-character
substitution of a valid address must be rejected, not merely a hand-picked one. Base58's
four-byte checksum makes a surviving substitution a one-in-four-billion event rather than
an impossibility, so the same exhaustive sweep is run over the base58 vectors too and was
confirmed to have no survivors before these vectors were chosen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

# --------------------------------------------------------------------------------------
# Bitcoin, the addresses that must be accepted
# --------------------------------------------------------------------------------------


class Vector(NamedTuple):
    """A valid address, the two forms it must be stored as, and its own alphabet.

    The alphabet is carried rather than guessed from the prefix: `corruptions_of` needs the
    characters a *typo* could plausibly produce, and a helper that sniffed it from the
    string would eventually sniff wrong and silently weaken the sweep it feeds.
    """

    id: str
    address: str
    canonical: str
    display: str
    alphabet: str


#: BIP-173, "Examples": the testnet P2WPKH address. Witness version 0, bech32.
BIP173_TESTNET_P2WPKH: Final = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"

#: BIP-173 and BIP-350, "Valid addresses": testnet P2WSH. Witness version 0, bech32.
BIP173_TESTNET_P2WSH: Final = "tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7"

#: BIP-350, "Valid addresses": testnet witness version 1, bech32m.
BIP350_TESTNET_V1: Final = "tb1pqqqqp399et2xygdj5xreqhjjvcmzhxw4aywxecjdzew6hylgvsesf3hn0c"

#: Bitcoin Core `key_io_valid.json`, chain `testnet4`: witness version 1, bech32m.
CORE_TESTNET4_V1: Final = "tb1p35n52jy6xkm4wd905tdy8qtagrn73kqdz73xe4zxpvq9t3fp50aqk3s6gz"

#: Bitcoin Core `key_io_valid.json`, chain `regtest`: witness version 0, bech32.
CORE_REGTEST_P2WPKH: Final = "bcrt1qdavt4j2sd7dlhqsavtnfxvzppw6k7qy97tmnu9"

#: Bitcoin Core `key_io_valid.json`, chain `regtest`: witness version 1, bech32m.
CORE_REGTEST_V1: Final = "bcrt1pfwxjqvtt4tcxrtdluukfmy2dv7xd2qzdfy6kajv5nwn4yam3wxkq3553uh"

#: Bitcoin Core `key_io_valid.json`, chain `testnet4`: legacy P2PKH, version byte 0x6F.
CORE_TESTNET4_P2PKH: Final = "mwgS2HRbjyfYxFnR1nF9VKLvmdgMfFBmGq"

#: Bitcoin Core `key_io_valid.json`, chain `signet`: legacy P2PKH, version byte 0x6F.
#: Chosen over the other P2PKH rows because every character of its lowercase form is still
#: inside the base58 alphabet, so case-flipping it fails on the *checksum* rather than on
#: an illegal character -- which is what makes the case-sensitivity test say what it means.
CORE_SIGNET_P2PKH: Final = "mfnJ8tEkqKNFE5YaHTXFxyHk2mnDK2fvDh"

#: Bitcoin Core `key_io_valid.json`, chain `testnet4`: legacy P2SH, version byte 0xC4.
CORE_TESTNET4_P2SH: Final = "2MwBVrJQ76BdaGD76CTmou8cZzQYLpe4NqU"

#: Bitcoin Core `key_io_valid.json`, chain `regtest`: legacy P2SH, version byte 0xC4.
CORE_REGTEST_P2SH: Final = "2MxFajLApXpYk4VodBSZSt7rw8y4ryABkfA"

#: The same address as `BIP173_TESTNET_P2WPKH`, rendered the way a QR-code wallet shows
#: it. Bech32 is case insensitive, so this is the *same* address; it is here because the
#: canonical form must lowercase it while the display form must not.
BIP173_TESTNET_P2WPKH_UPPERCASE: Final = BIP173_TESTNET_P2WPKH.upper()

BECH32_CHARSET: Final = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BASE58_ALPHABET: Final = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

#: Bech32 canonicalises to lowercase and keeps the display form as typed.
BECH32_VECTORS: Final[tuple[Vector, ...]] = (
    *(
        Vector(name, address, address, address, BECH32_CHARSET)
        for name, address in (
            ("tb1 v0 p2wpkh", BIP173_TESTNET_P2WPKH),
            ("tb1 v0 p2wsh", BIP173_TESTNET_P2WSH),
            ("tb1 v1 bech32m", BIP350_TESTNET_V1),
            ("tb1 v1 core", CORE_TESTNET4_V1),
            ("bcrt1 v0", CORE_REGTEST_P2WPKH),
            ("bcrt1 v1", CORE_REGTEST_V1),
        )
    ),
    Vector(
        "tb1 v0 uppercase",
        BIP173_TESTNET_P2WPKH_UPPERCASE,
        BIP173_TESTNET_P2WPKH,
        BIP173_TESTNET_P2WPKH_UPPERCASE,
        BECH32_CHARSET.upper(),
    ),
)

#: Base58Check is case *sensitive*, so canonical and display are the same bytes. Storing a
#: lowercased copy would name a different address, which is why these two columns exist.
BASE58_VECTORS: Final[tuple[Vector, ...]] = tuple(
    Vector(name, address, address, address, BASE58_ALPHABET)
    for name, address in (
        ("testnet4 p2pkh", CORE_TESTNET4_P2PKH),
        ("signet p2pkh", CORE_SIGNET_P2PKH),
        ("testnet4 p2sh", CORE_TESTNET4_P2SH),
        ("regtest p2sh", CORE_REGTEST_P2SH),
    )
)

BITCOIN_VECTORS: Final[tuple[Vector, ...]] = BECH32_VECTORS + BASE58_VECTORS

# --------------------------------------------------------------------------------------
# Bitcoin: published counterexamples, each with the reason its own specification gives
# --------------------------------------------------------------------------------------

#: BIP-350, "Invalid addresses": *witness version 0* carrying a **bech32m** checksum.
#: Everything else about it is well formed, so a validator that accepts either constant
#: for any version accepts this -- which is the bug BIP-350 exists to prevent.
BIP350_V0_WITH_BECH32M: Final = "tb1q0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vq24jc47"

#: BIP-350, "Invalid addresses": witness version 2 carrying a **bech32** checksum.
BIP350_V2_WITH_BECH32: Final = "tb1z0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqglt7rf"

#: Derived, not published: witness version **1** carrying a **bech32** checksum. The
#: published lists have this case only for mainnet, and rule 3 forbids quoting it.
#:
#: Minted by re-encoding the *unchanged* data part of `BIP350_TESTNET_V1` -- same human
#: readable part, same witness version, same witness program -- with the bech32 generator
#: constant in place of the bech32m one. The only difference between this string and the
#: published valid vector is the six checksum characters. The tool that produced it had
#: first reproduced every published non-mainnet vector in BIP-173, BIP-350 and Bitcoin
#: Core's `key_io_valid.json` / `key_io_invalid.json`; it is not the implementation under
#: test, and no part of `portfolio.domain` was involved.
DERIVED_V1_WITH_BECH32: Final = "tb1pqqqqp399et2xygdj5xreqhjjvcmzhxw4aywxecjdzew6hylgvsesud8l26"

# The cases below have published counterexamples only on **mainnet**, which rule 3
# forbids quoting. Each is therefore minted for this suite from one fixed synthetic
# witness program -- `sha256(b"portfolio-test-witness-program")`, truncated or repeated as
# the case needs -- by the same independent bech32 implementation that produced
# `DERIVED_V1_WITH_BECH32`, and each carries a *correct* checksum so that the refusal has
# to come from the rule being tested rather than from the checksum failing first.

#: Witness version 17, which does not exist: BIP-141 defines 0 to 16.
DERIVED_BAD_WITNESS_VERSION: Final = (
    "tb13u53pd33t2wnuluaq8rfh0f990l7gxpnac8e2h9g6yvc3etnw5whqpg2p4x"
)

#: Witness version 0 with a 21-byte program. Version 0 is defined for 20 bytes (P2WPKH)
#: and 32 (P2WSH) and for nothing else, so this is well formed and still not an address.
DERIVED_V0_WRONG_PROGRAM_LENGTH: Final = "tb1qu53pd33t2wnuluaq8rfh0f990l7gxpnacyqz2cqr"

#: A one-byte witness program. BIP-141's floor is two.
DERIVED_PROGRAM_TOO_SHORT: Final = "tb1pu5w5z9hq"

#: A forty-one byte witness program. BIP-141's ceiling is forty.
DERIVED_PROGRAM_TOO_LONG: Final = (
    "tb1pu53pd33t2wnuluaq8rfh0f990l7gxpnac8e2h9g6yvc3etnw5whw2gskcc448f707vsneyj7"
)

#: A checksum and nothing else: BIP-173's "empty data section", which has no witness
#: version to read, let alone a program.
DERIVED_EMPTY_DATA_SECTION: Final = "tb1cy0q7p"

#: 106 characters. Under the registry's own 128-character ceiling and over BIP-173's
#: 90-character limit, so it is the only input that reaches the codec's own length check
#: rather than being turned away by the outer one.
DERIVED_OVER_BECH32_LENGTH_LIMIT: Final = (
    "tb1pu53pd33t2wnuluaq8rfh0f990l7gxpnac8e2h9g6yvc3etnw5whw2gskcc448f707wsr35mh5"
    "jjhllyrqe7uru4tj5dzxvguvyzd0e"
)

#: BIP-350, "Invalid addresses": mixed case.
BIP350_MIXED_CASE: Final = "tb1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vq47Zagq"

#: BIP-173, "Invalid addresses": mixed case, one capital `L` in an otherwise valid string.
BIP173_MIXED_CASE: Final = "tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sL5k7"

#: BIP-173 and BIP-350, "Invalid addresses": unknown human-readable part (`tc`, not `tb`).
BIP173_UNKNOWN_HRP: Final = "tc1qw508d6qejxtdg4y5r3zarvary0c5xw7kg3g4ty"
BIP350_UNKNOWN_HRP: Final = "tc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vq5zuyut"

#: BIP-350 and BIP-173, "Invalid addresses": non-zero padding in the 8-to-5 conversion.
BIP350_NON_ZERO_PADDING: Final = "tb1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vpggkg4j"
BIP173_NON_ZERO_PADDING: Final = "tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3pjxtptv"

#: Bitcoin Core `key_io_invalid.json`: a witness program far too short for its version.
CORE_INVALID_SHORT_PROGRAM: Final = "bcrt1r2qxpwuge"

#: Bitcoin Core `key_io_invalid.json`: a string whose Base58Check checksum **verifies**
#: and whose version byte is `0xD4`, which is not any of Bitcoin's four address version
#: bytes. A codec that stops once the checksum passes accepts this, so it is the vector
#: that proves the version byte is looked at rather than merely decoded.
CORE_UNKNOWN_VERSION_BYTE: Final = "2UVPFpGYnLHJezFzjUo42our6PMEoozzRdM"

#: Bitcoin Core `key_io_invalid.json`: base58 strings shaped like testnet addresses that
#: are not addresses -- two are the wrong decoded length, one fails its checksum. Used
#: only to assert rejection; which reason applies is the codec's business.
CORE_INVALID_BASE58: Final[tuple[str, ...]] = (
    "2MygHQjE1U33q3LSC53p69YqFjP8PihumJAF",
    "2NDNP7GY59tTJPZTpbkprhM9SR99Nn5rUs7",
    "2jDPrDfAKihCGPbPD9ztY8TswAia4V8Bc6vx",
)

#: Synthetic, and deliberately *well formed*: version bytes `0x043587CF`, depth 0, null
#: fingerprint and child number, a chain code and a key body taken from two fixed SHA-256
#: digests of ASCII sentences, and a genuine four-byte double-SHA-256 checksum. It
#: controls nothing -- the key body is a hash, not a curve point -- but it decodes cleanly
#: as Base58Check, which is the point: an extended key with a *broken* checksum would be
#: rejected by the base58 codec anyway, and a test built on one would prove nothing about
#: whether extended keys are refused on purpose. `tpub` rather than `xpub` because rule 3
#: permits exactly the testnet prefixes.
SYNTHETIC_TPUB: Final = (
    "tpubD6NzVbkrYhZ4YitbqiNSwc9J2wKHfwnQN65Y21sccUoPYdgz9VcGYAE542vHpm3cyyL7"
    "PP4E25mUyyJ3iLLewvynfFD74zQj1TFegyiQivN"
)

# --------------------------------------------------------------------------------------
# Kaspa
# --------------------------------------------------------------------------------------

# There is no Kaspa node in CI, no BIP to quote, and rule 3 forbids the mainnet vectors
# that most documentation uses. The three below are the **testnet** rows of the case table
# in `kaspanet/rusty-kaspa`, `crypto/addresses/src/lib.rs`, `mod tests::cases()` -- a
# third-party reference implementation's own published test data, not anything this
# repository produced. Each row there states the version and the public key the address
# encodes, which is what makes them checkable rather than merely quoted:
#
#   Version::PubKey (0)      + 32 zero bytes
#   Version::PubKeyECDSA (1) + 33 zero bytes
#   Version::PubKeyECDSA (1) + ba01fc5f...b0f60e
#
# They were re-derived from those three inputs by an independent CashAddr implementation
# written for this suite, which had first reproduced all fourteen non-mainnet rows of that
# table and all five of its documented error cases. The implementation under test was not
# involved in producing or checking any of them.

#: Version 0 (Schnorr public key), 32 zero bytes.
KASPA_TESTNET_V0: Final = "kaspatest:qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqhqrxplya"

#: Version 1 (ECDSA public key), 33 zero bytes. One character longer than version 0, which
#: is the pair that makes a length check per version mean something.
KASPA_TESTNET_V1_ZERO: Final = (
    "kaspatest:qyqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqhe837j2d"
)

#: Version 1 (ECDSA public key), a non-degenerate key. The zero-payload vectors above are
#: the ones a bug in the 8-to-5 regrouping is most likely to survive, so one vector with
#: bits set in every byte is here to catch it.
KASPA_TESTNET_V1_KEY: Final = (
    "kaspatest:qxaqrlzlf6wes72en3568khahq66wf27tuhfxn5nytkd8tcep2c0vrse6gdmpks"
)

#: Version 0, 32 bytes, and from a **different publisher**: the Aspectron "Integrating
#: with Kaspa" documentation, https://kaspa.aspectron.org/wallets/primitives/addresses.html
#: Worth having precisely because the other three come from one repository. Two independent
#: publishers agreeing is a much stronger statement about the encoding than one repository
#: agreeing with itself, which is the same objection the spec raises against a vector
#: generated by the implementation under test.
KASPA_TESTNET_V0_ASPECTRON: Final = (
    "kaspatest:qqnapngv3zxp305qf06w6hpzmyxtx2r99jjhs04lu980xdyd2ulwwmx9evrfz"
)

KASPA_VECTORS: Final[tuple[Vector, ...]] = tuple(
    Vector(name, address, address, address, BECH32_CHARSET)
    for name, address in (
        ("kaspatest v0", KASPA_TESTNET_V0),
        ("kaspatest v1 zero key", KASPA_TESTNET_V1_ZERO),
        ("kaspatest v1 real key", KASPA_TESTNET_V1_KEY),
        ("kaspatest v0 aspectron", KASPA_TESTNET_V0_ASPECTRON),
    )
)

#: rusty-kaspa's case table also carries twelve rows under the test-only prefixes `a:` and
#: `b:`. They are not addresses -- the payloads are ASCII strings, not public keys -- so
#: `kaspa_decode` refuses them on the prefix long before it reaches a checksum, and rule 3
#: does not touch them because there is no network and no value involved.
#:
#: What they are good for is the polymod on its own. A one-character prefix exercises the
#: prefix expansion differently from `kaspatest`, and a two-character payload exercises the
#: checksum with none of the version or length rules in the way, so a bug in the generator
#: constants shows up here with nothing else to blame.
KASPA_POLYMOD_VECTORS: Final[tuple[tuple[str, str], ...]] = (
    ("a", "qqeq69uvrh"),
    ("a", "pq99546ray"),
    ("b", "pqsqzsjd64fv"),
    ("b", "pqksmhczf8ud"),
    ("b", "pqcq53eqrk0e"),
    ("b", "pqcshg75y0vf"),
    ("b", "pqknzl4e9y0zy"),
    ("b", "pqcnzt888ytdg"),
    ("b", "ppskycc8txxxn2w"),
    ("b", "pqcnyve5x5unsdekxqeusxeyu2"),
    ("b", "ppskycmyv4nxw6rfdf4kcmtwdac8zunnw36hvamc09aqtpppz8lk"),
    ("b", "pqcrqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcrq7ag684l3"),
)

#: The data part of `KASPA_TESTNET_V0` under a *different, still valid* network prefix.
#: Kaspa folds the prefix into the checksum, so this must fail on the checksum rather than
#: on the prefix -- which is the only way to tell "the prefix is checked" from "the prefix
#: is hashed". A validator that checksummed the payload alone would accept it, and a
#: testnet address would then be storable as a mainnet one.
KASPA_WRONG_NETWORK_PREFIX: Final = (
    "kaspadev:qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqhqrxplya"
)

#: `kaspasim` is a real prefix in rusty-kaspa and is deliberately not one this application
#: accepts: a simulation-network address in a portfolio is a mistake, not a configuration.
KASPA_UNKNOWN_PREFIX: Final = (
    "kaspasim:qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqhqrxplya"
)

#: The payload with no prefix at all. Kaspa wallets show addresses with the prefix, and
#: without it the checksum cannot be computed -- so this is refused, not guessed at.
KASPA_NO_PREFIX: Final = KASPA_TESTNET_V0.split(":", 1)[1]

#: Derived from a vector: upper case from the middle of the payload onwards.
KASPA_MIXED_CASE: Final = KASPA_TESTNET_V1_KEY[:30] + KASPA_TESTNET_V1_KEY[30:].upper()

# Kaspa has no BIP and no published invalid-address table beyond the five cases in
# rusty-kaspa's `test_errors`, none of which covers a version byte or a payload length. The
# four below are therefore minted for this suite, each with a **correct** checksum over the
# prefix, by the same independent CashAddr implementation that re-derived the vectors above.
# A correct checksum is the point: it forces the refusal to come from the rule being tested.

#: A payload of one five-bit value: fewer bits than a single byte, so there is not even a
#: version byte to read. A decoder that indexed straight into the regrouped bytes raises
#: `IndexError` here and turns a mistyped address into a 500.
KASPA_PAYLOAD_TOO_SHORT: Final = "kaspatest:ql8trnqpt"

#: Nine characters of which eight are the checksum, so the payload is empty. Refused on
#: length before the checksum is even computed.
KASPA_DATA_TOO_SHORT: Final = "kaspatest:qq"

#: Version byte 2. Kaspa defines 0 (Schnorr), 1 (ECDSA) and 8 (P2SH) and nothing else.
KASPA_UNKNOWN_VERSION_BYTE: Final = (
    "kaspatest:q2uvlwh9v5cutpr58wsswy9p2a5zxt8qe5gvk79rxhhn0wrxy5vxvcu3hgkht"
)

#: Version 0 with a 31-byte payload. Version 0 is a 32-byte Schnorr key and nothing else.
KASPA_WRONG_PAYLOAD_LENGTH: Final = (
    "kaspatest:qzuvlwh9v5cutpr58wsswy9p2a5zxt8qe5gvk79rxhhn0wrxy5vq9pwrqcld"
)

#: `KASPA_TESTNET_V0` with **one extra five-bit character** appended to its payload and the
#: checksum recomputed. This is the subtle one, and it is the reason the length check
#: counts characters as well as bytes: 54 five-bit values regroup into the same 33 bytes as
#: 53 do, so the version byte and the payload length both come out *correct*. A decoder
#: that checked only the decoded bytes would accept this as the very same address as
#: `KASPA_TESTNET_V0` -- two different strings, one address, and therefore two rows that
#: the unique constraint cannot see are duplicates. Verified: the independent reference
#: used to mint it, which has no such check, does exactly that.
KASPA_OVERLONG_PAYLOAD: Final = (
    "kaspatest:qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqpnud95ak"
)

#: One named single-character corruption per Kaspa vector, in the payload.
KASPA_NAMED_CORRUPTIONS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "kaspatest v0",
        KASPA_TESTNET_V0,
        "kaspatest:qqqqqqqqqqqqqqqqqqqqqqqqqpqqqqqqqqqqqqqqqqqqqqqqqqqqqhqrxplya",
    ),
    (
        "kaspatest v1 zero key",
        KASPA_TESTNET_V1_ZERO,
        "kaspatest:qyqqqqqqqqqqqqqqqqqqqqqqqqpqqqqqqqqqqqqqqqqqqqqqqqqqqqqhe837j2d",
    ),
    (
        "kaspatest v1 real key",
        KASPA_TESTNET_V1_KEY,
        "kaspatest:qxaqrlzlf6wes72en3568khahqq6wf27tuhfxn5nytkd8tcep2c0vrse6gdmpks",
    ),
    (
        "kaspatest v0 aspectron",
        KASPA_TESTNET_V0_ASPECTRON,
        "kaspatest:qqnapngv3zxp305qf06w6hpzmqxtx2r99jjhs04lu980xdyd2ulwwmx9evrfz",
    ),
)

# --------------------------------------------------------------------------------------
# Shapes that are not chain specific
# --------------------------------------------------------------------------------------

#: The `address` field of a request body is bounded, so an input that is merely enormous
#: has to be refused before any codec is asked to think about it.
TWO_HUNDRED_CHARACTERS: Final = "a" * 200

#: Empty, and the several ways a copy-and-paste arrives with nothing useful in it.
BLANK_INPUTS: Final[tuple[str, ...]] = ("", " ", "\t", "\n", "   \t\n  ")

# --------------------------------------------------------------------------------------
# Corruption
# --------------------------------------------------------------------------------------


def corruptions_of(address: str, alphabet: str) -> Iterator[tuple[int, str]]:
    """Every single-character substitution of `address` drawn from `alphabet`.

    Yields `(position, corrupted)` so that a failure names the character that got through
    rather than only the string it produced. The replacement is always drawn from the
    address's own alphabet: substituting a character that is *outside* the alphabet is a
    weaker test, because a validator can reject it on the alphabet alone without ever
    computing a checksum.
    """
    for position, original in enumerate(address):
        for replacement in alphabet:
            if replacement == original:
                continue
            yield position, address[:position] + replacement + address[position + 1 :]


#: One named corruption per valid vector, so the exhaustive sweep has a readable companion
#: a reviewer can check by eye. Each differs from its vector in exactly one character, in
#: the middle of the **payload** rather than in the checksum tail -- a mistyped payload is
#: what a user actually produces, and a validator that recomputed nothing would have to
#: accept it. `test_the_named_corruptions_really_are_single_character` proves the pairs
#: here are what this comment claims, so a careless edit cannot quietly weaken them.
NAMED_CORRUPTIONS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "tb1 v0 p2wpkh",
        BIP173_TESTNET_P2WPKH,
        "tb1qw508d6qejxtdg4y5rqzarvary0c5xw7kxpjzsx",
    ),
    (
        "tb1 v0 p2wsh",
        BIP173_TESTNET_P2WSH,
        "tb1qrp33g0q5c5txsp9arysrx4k6zdkqs4nce4xj0gdcccefvpysxf3q0sl5k7",
    ),
    (
        "tb1 v1 bech32m",
        BIP350_TESTNET_V1,
        "tb1pqqqqp399et2xygdj5xreqhjjvcmqhxw4aywxecjdzew6hylgvsesf3hn0c",
    ),
    (
        "tb1 v1 core",
        CORE_TESTNET4_V1,
        "tb1p35n52jy6xkm4wd905tdy8qtagrnq3kqdz73xe4zxpvq9t3fp50aqk3s6gz",
    ),
    (
        "bcrt1 v0",
        CORE_REGTEST_P2WPKH,
        "bcrt1qdavt4j2sd7dlhqsaqtnfxvzppw6k7qy97tmnu9",
    ),
    (
        "bcrt1 v1",
        CORE_REGTEST_V1,
        "bcrt1pfwxjqvtt4tcxrtdluukfmy2dv7qd2qzdfy6kajv5nwn4yam3wxkq3553uh",
    ),
    (
        "testnet4 p2pkh",
        CORE_TESTNET4_P2PKH,
        "mwgS2HRbjyfYxFnR11F9VKLvmdgMfFBmGq",
    ),
    (
        "signet p2pkh",
        CORE_SIGNET_P2PKH,
        "mfnJ8tEkqKNFE5YaH1XFxyHk2mnDK2fvDh",
    ),
    (
        "testnet4 p2sh",
        CORE_TESTNET4_P2SH,
        "2MwBVrJQ76BdaGD761Tmou8cZzQYLpe4NqU",
    ),
    (
        "regtest p2sh",
        CORE_REGTEST_P2SH,
        "2MxFajLApXpYk4Vod1SZSt7rw8y4ryABkfA",
    ),
)
